"""الواجهة العربية (من اليمين لليسار) — FastAPI + Jinja2."""
from __future__ import annotations

import hashlib
import os
import json
import logging
import threading
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from .. import jobs
from ..auth import ROLE_LABELS, authenticate, ensure_admin, hash_password
from ..companies import OPERATOR_LABELS, SOURCE_LABELS, log_activity
from ..config import settings
from ..db import SessionLocal, init_db, session_scope
from ..discovery.sources import DirectoryImportSource
from ..models import (LoginCode, Attachment, Call, Campaign, Category, Company, Consent, DiscoveryRun, ExistingCustomer,
                      InboundMessage, MailAccount, Meeting, Notification, Offer, OutboundMessage, Setting,
                      Suppression, Task, User)
from ..normalize import domain_of, format_phone_display, normalize_email, normalize_name, normalize_phone
from ..reports import (STAGE_LABELS, STATUS_LABELS, OfferFilters, company_timeline, daily_report, dashboard,
                       export_offers_xlsx, needs_followup_today, offers_report, scope_companies)
from ..replyparse import INTENT_LABELS
from ..segments import REGIONS
from ..targeting import CITIES, SECTORS, parse_request
from ..timeutil import fmt, fmt_date, now_utc, riyadh_to_utc, riyadh_today, to_riyadh

log = logging.getLogger(__name__)
HERE = Path(__file__).parent

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app):
    _startup()
    yield


app = FastAPI(title="المندوب الذكي لسيارة اب", docs_url=None, redoc_url=None, lifespan=_lifespan)


class LangMiddleware:
    """لغة الطلب (الجلسة ← ملف تعريف الارتباط ← العربية) + ترجمة صفحات HTML إلى الإنجليزية عند اختيارها."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from http.cookies import SimpleCookie
        from ..i18n import LANGS, reset_lang, set_lang, translate_html
        lang = (scope.get("session") or {}).get("lang")
        if lang not in LANGS:
            ck = SimpleCookie()
            for k, v in scope.get("headers") or []:
                if k == b"cookie":
                    ck.load(v.decode("latin-1"))
            lang = ck["lang"].value if "lang" in ck and ck["lang"].value in LANGS else "ar"
        token = set_lang(lang)
        if lang == "ar":
            try:
                return await self.app(scope, receive, send)
            finally:
                reset_lang(token)
        start, chunks = {}, []

        async def _send(msg):
            if msg["type"] == "http.response.start":
                start.update(msg)
                ctype = dict(msg.get("headers") or []).get(b"content-type", b"")
                start["_html"] = ctype.startswith(b"text/html")
                if not start["_html"]:
                    await send(msg)
                return
            if msg["type"] == "http.response.body" and start.get("_html"):
                chunks.append(msg.get("body", b""))
                if msg.get("more_body"):
                    return
                body = translate_html(b"".join(chunks).decode("utf-8")).encode("utf-8")
                hdrs = [(k, v) for k, v in start.get("headers", []) if k != b"content-length"]
                hdrs.append((b"content-length", str(len(body)).encode()))
                await send({"type": "http.response.start", "status": start["status"], "headers": hdrs})
                await send({"type": "http.response.body", "body": body})
                return
            await send(msg)
        try:
            await self.app(scope, receive, _send)
        finally:
            reset_lang(token)


app.add_middleware(LangMiddleware)  # داخل الجلسة: يقرأ لغة المستخدم المحفوظة فيها
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 12, same_site="lax",
                   https_only=settings.public_base_url.startswith("https"))
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(fmt=fmt, fmt_date=fmt_date, STATUS_LABELS=STATUS_LABELS, STAGE_LABELS=STAGE_LABELS,
                             SECTORS=SECTORS, CITIES=list(CITIES), REGIONS=REGIONS, OPERATOR_LABELS=OPERATOR_LABELS, ROLE_LABELS=ROLE_LABELS,
                             INTENT_LABELS=INTENT_LABELS, SOURCE_LABELS=SOURCE_LABELS, phone=format_phone_display)
from ..activities import ENTITY_TYPES, VEHICLE_INDICATOR  # noqa: E402
templates.env.globals.update(ENTITY_TYPES=ENTITY_TYPES, VEHICLE_INDICATOR=VEHICLE_INDICATOR)
from ..i18n import current_lang as _cur_lang  # noqa: E402
templates.env.globals.update(cur_lang=_cur_lang)
from ..companies import owned_phones as _owned_phones  # noqa: E402
templates.env.globals.update(owned_phones=_owned_phones)


def _startup() -> None:
    init_db()
    ensure_admin(settings.admin_email, settings.admin_password)
    from ..permissions import backfill_default_perms
    with session_scope() as s:
        backfill_default_perms(s)
    if settings.run_scheduler_in_web:
        from ..worker import start_background
        start_background()


# ---------------------------------------------------------------- أدوات

class LoginRequired(Exception):
    def __init__(self, to: str = "/admin/login"):
        self.to = to


@app.exception_handler(LoginRequired)
async def _login_redirect(request, exc):
    return RedirectResponse(exc.to, status_code=303)


def get_db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _session_user(request: Request, s, portal: str) -> User | None:
    """يعيد المستخدم فقط إن كانت جلسته لهذه البوابة، وحسابه نشط، ولم تُغلق جلساته (إيقاف/تغيير)."""
    uid, sv, p = request.session.get("uid"), request.session.get("sv"), request.session.get("portal")
    if not uid or p != portal:
        return None
    user = s.get(User, uid)
    if not user or not user.active or user.session_version != sv:
        request.session.clear()
        return None
    from ..auth import portal_of
    if portal_of(user) != portal:
        request.session.clear()
        return None
    return user


def current_user(request: Request, s=Depends(get_db)) -> User:
    """بوابة الإدارة."""
    user = _session_user(request, s, "admin")
    if user is None:
        raise LoginRequired("/portal" if request.session.get("portal") == "staff" else "/admin/login")
    return user


def staff_user(request: Request, s=Depends(get_db)) -> User:
    """بوابة الموظفين."""
    user = _session_user(request, s, "staff")
    if user is None:
        raise LoginRequired("/portal/login")
    return user


def require_manager(user: User = Depends(current_user)) -> User:
    if user.role not in ("admin", "manager"):
        raise HTTPException(403, "هذه الصفحة لمدير المبيعات فقط")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "هذه الصفحة لمدير المنصة فقط")
    return user


def render(request: Request, name: str, user: User | None, s=None, **ctx):
    unread = 0
    if user is not None and s is not None:
        unread = s.scalar(select(func.count(Notification.id)).where(Notification.user_id == user.id,
                                                                    Notification.read.is_(False))) or 0
    flash = request.session.pop("flash", None)
    demo = False
    if user is not None and s is not None:
        from ..reports import demo_view
        demo = demo_view(s)
    return templates.TemplateResponse(request, name, {"user": user, "unread": unread, "flash": flash, "demo_on": demo,
                                                      "voice_on": settings.voice_calls_enabled,
                                                      "outreach_on": settings.auto_outreach_enabled,
                                                      "path": request.url.path, **ctx})


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session["flash"] = {"msg": msg, "kind": kind}


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _company_visible(user: User, c: Company) -> bool:
    return user.role != "rep" or c.owner_id == user.id


def _bg(fn, *args, **kwargs) -> None:
    threading.Thread(target=lambda: _safe(fn, *args, **kwargs), daemon=True).start()


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        log.exception("background job failed")


# ---------------------------------------------------------------- الدخول

@app.get("/login")
def login_legacy():
    return back("/admin/login")


def _ip(request: Request) -> str:
    from ..leads import hash_ip
    return hash_ip(request.client.host if request.client else None, settings.secret_key)


def _login_page(request: Request, portal: str, s):
    import time as _t
    from ..auth import RESEND_AFTER_SECONDS, mail_ready
    sent = int(request.session.get(f"otp_{portal}_at") or 0)
    wait = max(0, RESEND_AFTER_SECONDS - (int(_t.time()) - sent)) if sent else 0
    return render(request, "otp_login.html", None, portal=portal, step=request.session.get(f"otp_{portal}"),
                  wait=wait, bootstrap=(portal == "admin" and not mail_ready(s)))


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request, s=Depends(get_db)):
    return _login_page(request, "admin", s)


@app.get("/portal/login", response_class=HTMLResponse)
def staff_login_page(request: Request, s=Depends(get_db)):
    return _login_page(request, "staff", s)


def _request_code(request: Request, portal: str, email: str, s):
    from ..auth import OTPError, account_problem, request_code, send_code_email
    problem = account_problem(s, email, portal)
    if problem:
        flash(request, problem, "err")
        return
    try:
        user, code = request_code(s, email, portal, _ip(request))
    except OTPError as e:
        flash(request, str(e), "err")
        return
    if user is not None and not send_code_email(s, user, code, portal):
        log.error("login code email failed for user %s", user.id)
        s.rollback()
        flash(request, "تعذر إرسال الكود لأن بريد الشركة غير مربوط بالمنصة بعد. تواصل مع الإدارة.", "err")
        return
    import time as _t
    request.session[f"otp_{portal}"] = email.strip().lower()
    request.session[f"otp_{portal}_at"] = int(_t.time())
    flash(request, "أُرسل كود الدخول إلى بريدك من بريد الشركة. صالح لمدة ٥ دقائق ولمرة واحدة.")


def _verify_code(request: Request, portal: str, code: str, s):
    from ..auth import OTPError, verify_code
    email = request.session.get(f"otp_{portal}")
    try:
        user = verify_code(s, email or "", code, portal)
    except OTPError as e:
        s.commit()  # حفظ عدّاد المحاولات
        flash(request, str(e), "err")
        return None
    request.session.clear()
    request.session.update({"uid": user.id, "sv": user.session_version, "portal": portal,
                            "lang": _remember_lang(request, user)})
    return user


@app.post("/admin/login/request")
def admin_request_code(request: Request, email: str = Form(...), s=Depends(get_db)):
    _request_code(request, "admin", email, s)
    return back("/admin/login")


@app.post("/portal/login/request")
def staff_request_code(request: Request, email: str = Form(...), s=Depends(get_db)):
    _request_code(request, "staff", email, s)
    return back("/portal/login")


@app.post("/admin/login/verify")
def admin_verify(request: Request, code: str = Form(...), s=Depends(get_db)):
    return back("/" if _verify_code(request, "admin", code, s) else "/admin/login")


@app.post("/portal/login/verify")
def staff_verify(request: Request, code: str = Form(...), s=Depends(get_db)):
    return back("/portal" if _verify_code(request, "staff", code, s) else "/portal/login")


@app.post("/admin/login/restart")
def admin_restart(request: Request):
    request.session.pop("otp_admin", None)
    return back("/admin/login")


@app.post("/portal/login/restart")
def staff_restart(request: Request):
    request.session.pop("otp_staff", None)
    return back("/portal/login")


@app.post("/admin/login/password")
def admin_bootstrap_password(request: Request, email: str = Form(...), password: str = Form(...), s=Depends(get_db)):
    """دخول تأسيسي بكلمة المرور لحساب ADMIN_EMAIL فقط، ويُعطَّل تلقائيًا بعد ربط بريد الشركة."""
    from ..auth import audit, mail_ready
    if mail_ready(s) or email.strip().lower() != settings.admin_email.lower():
        flash(request, "الدخول بكلمة المرور غير متاح. استخدم رمز البريد.", "err")
        return back("/admin/login")
    u = authenticate(email, password)
    if not u:
        flash(request, "البريد أو كلمة المرور غير صحيحة", "err")
        return back("/admin/login")
    audit(s, u, "login", u, portal="admin", method="bootstrap_password")
    request.session.clear()
    request.session.update({"uid": u.id, "sv": u.session_version, "portal": "admin", "lang": _remember_lang(request, u)})
    return back("/")


def _remember_lang(request: Request, user: User) -> str:
    """لغة المستخدم المحفوظة في حسابه تتقدّم؛ وإن لم يختر بعد فاختيار شاشة الدخول يُحفظ له."""
    from ..i18n import LANGS, current_lang
    if user.lang not in LANGS:
        user.lang = current_lang()
    return user.lang


@app.get("/lang")
def switch_lang(request: Request, to: str = "ar", next: str = "/", s=Depends(get_db)):
    """«العربية | English»: يحفظ الاختيار (الحساب + الجلسة + المتصفح) ويعيد الصفحة نفسها بفلاترها."""
    from ..i18n import LANGS
    to = to if to in LANGS else "ar"
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    if request.session.get("uid"):
        request.session["lang"] = to
        u = s.get(User, request.session["uid"])
        if u is not None:
            u.lang = to
    else:
        request.session.pop("lang", None)
    r = back(next)
    r.set_cookie("lang", to, max_age=60 * 60 * 24 * 365, samesite="lax", httponly=True,
                 secure=settings.public_base_url.startswith("https"))
    return r


@app.get("/logout")
def logout(request: Request):
    portal = request.session.get("portal")
    request.session.clear()
    return back("/portal/login" if portal == "staff" else "/admin/login")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- الرئيسية

@app.get("/", response_class=HTMLResponse)
def home(request: Request, period: str = "7d", user: User = Depends(current_user), s=Depends(get_db)):
    from urllib.parse import parse_qsl
    from ..health import search_status
    from ..reports import (PERIODS, card_links, daily_target, period_bounds, recent_customers,
                           sales_performance)
    if user.role not in ("admin", "manager"):
        return back("/portal")
    links = card_links()

    def count(link):  # الرقم = عدد القائمة نفسها التي تفتحها البطاقة
        if link == "/followups":
            return len(needs_followup_today(s, user))
        p = dict(parse_qsl(link.split("?", 1)[1]))
        qq = _companies_query(user, p.get("status", "complete"), "", "", "", "", "", "", p.get("kind", ""),
                              p.get("owner", ""), "", p.get("found", ""))
        return s.scalar(select(func.count()).select_from(qq.subquery())) or 0
    cards = {k: count(v) for k, v in links.items()}
    period = period if period in PERIODS else "7d"
    t0, t1 = period_bounds("today")
    p0, p1 = period_bounds(period)
    perf_today = sales_performance(s, user, t0, t1)
    perf = sales_performance(s, user, p0, p1)
    from ..models import AdminList, AdminListContact
    lists_ = list(s.execute(select(AdminList.id, AdminList.rows)))
    adm = {"lists": len(lists_), "rows": sum(len(r or []) for _, r in lists_),
           "done": s.scalar(select(func.count(AdminListContact.id))) or 0,
           "replied": s.scalar(select(func.count(AdminListContact.id)).where(AdminListContact.status == "replied")) or 0,
           "first": lists_[-1][0] if lists_ else None}
    adm["none"] = max(adm["rows"] - adm["done"], 0)
    return render(request, "dashboard.html", user, s, adm=adm, cards=cards, links=links, target=daily_target(s),
                  today=riyadh_today(), recent=recent_customers(s, user), follow=needs_followup_today(s, user)[:8],
                  health=search_status(s), perf=perf, perf_today=perf_today, period=period, PERIODS=PERIODS)


# ---------------------------------------------------------------- العملاء المحتملون

def _prospect_args(day: str):
    try:
        return date.fromisoformat(day) if day else riyadh_today()
    except ValueError:
        return riyadh_today()


@app.get("/prospects", response_class=HTMLResponse)
def prospects_page(request: Request, day: str = "", city: str = "", sector: str = "", level: str = "", state: str = "ready",
                   q: str = "", owner: str = "", all: int = 0, user: User = Depends(current_user), s=Depends(get_db)):
    from .. import prospects as P
    from ..activities import SECTORS as ASECTORS
    from ..models import ProspectRun
    d = _prospect_args(day)
    qq = P.query(s, d, city, sector, level, state, q, owner)
    if user.role == "rep":
        qq = qq.where(Company.owner_id == user.id)
    total = s.scalar(select(func.count()).select_from(qq.subquery())) or 0
    rows = list(s.scalars(qq.limit(500 if all else 50)))
    cities = [c for (c,) in s.execute(select(Company.city).where(Company.prospect_on == d).distinct()) if c]
    last = s.scalar(select(ProspectRun).order_by(ProspectRun.id.desc()))
    parents = {c.id: s.get(Company, c.parent_id) for c in rows if c.parent_id}
    from ..models import ProspectPool
    pool = {st_: s.scalar(select(func.count(ProspectPool.id)).where(ProspectPool.status == st_)) or 0 for st_ in ("queued", "done")}
    with_key = bool(settings.google_places_api_key)
    return render(request, "prospects.html", user, s, sm=P.summary(s, d), rows=rows, parents=parents, pool=pool,
                  gkey=with_key, gspent=round(P.google_spent_today(s), 2), auto=P.get_auto(s), SOURCE_LABEL=P.SOURCE_LABEL, total=total, d=d, city=city,
                  sector=sector, level=level, state=state, q=q, owner=owner, all=all, cities=sorted(cities),
                  ASECTORS=ASECTORS, LEVELS=P.LEVELS, CONTACT_LEVELS=P.CONTACT_LEVELS, last=last,
                  csum=_csum(s, rows), qs=str(request.query_params),
                  staff=list(s.scalars(select(User).where(User.role == "rep", User.active.is_(True)).order_by(User.name))))


@app.get("/prospects.xlsx")
def prospects_xlsx(request: Request, day: str = "", city: str = "", sector: str = "", level: str = "", state: str = "ready",
                   q: str = "", owner: str = "", user: User = Depends(current_user), s=Depends(get_db)):
    from .. import prospects as P
    from ..activities import SECTORS as ASECTORS
    from ..xlsx import CONTACT_HEADERS, contact_cells, response, workbook
    d = _prospect_args(day)
    qq = P.query(s, d, city, sector, level, state, q, owner)
    if user.role == "rep":
        qq = qq.where(Company.owner_id == user.id)
    rows = []
    for c in s.scalars(qq):
        ph, em = c.primary("phone"), c.primary("email")
        rows.append([c.priority, P.LEVELS.get(c.priority_level or "", ""), c.name, c.name_en or "",
                     ASECTORS.get(c.sector_key or "", {}).get("label", ""), c.activity, c.city, c.website or "",
                     format_phone_display(ph.value) if ph else "", ph.source_url if ph else "",
                     em.value if em else "", em.source_url if em else "",
                     P.CONTACT_LEVELS.get(c.contact_level or "", "لا توجد وسيلة بعد"), fmt(c.channels_verified_at),
                     c.priority_reason or "", c.priority_url or "", c.vehicles_estimate or "", c.unified_number or "",
                     (s.get(Company, c.parent_id).name if c.parent_id else ""),
                     next((x.url for x in c.sources if x.source_type == "directory"), "")] + contact_cells(s, c))
    head = ["الأولوية (٠–١٠٠)", "مستوى الأولوية", "الاسم التجاري", "الاسم بالإنجليزية", "القطاع", "النشاط", "المدينة",
            "الموقع الإلكتروني", "هاتف الأعمال", "مصدر الهاتف", "البريد الرسمي", "مصدر البريد", "مستوى التحقق",
            "تاريخ التحقق", "سبب الأولوية", "رابط سبب الأولوية", "عدد المركبات المنشور", "الرقم الوطني الموحد",
            "الشركة الأم", "رابط المصدر"] + CONTACT_HEADERS
    return response(workbook([("العملاء المحتملون", head, rows)],
                             {"العملاء المحتملون": [[f"العملاء المحتملون — {d.isoformat()} — {len(rows)} منشأة"]]}),
                    f"prospects-{d.isoformat()}.xlsx")


@app.post("/prospects/run")
def prospects_run(request: Request, target: int = Form(100), user: User = Depends(require_manager), s=Depends(get_db)):
    from .. import prospects as P
    r = P.request_run(s, user, target, P.get_auto(s)["sources"])
    flash(request, f"بدأت دفعة «العملاء المحتملون» #{r.id} على الخادم — الهدف {r.target} منشأة قابلة للتواصل. "
                   "تتحدث الصفحة تلقائيًا.")
    return back("/prospects")


@app.post("/prospects/import")
async def prospects_import(request: Request, file: UploadFile = File(...), license_note: str = Form(""),
                           src_kind: str = Form("device"), src_url: str = Form(""),
                           user: User = Depends(require_manager), s=Depends(get_db)):
    """ملف مزوّد بيانات مرخّص (CSV أو Excel): يُضاف إلى طابور المعالجة اليومية."""
    from .. import prospects as P
    from ..auth import audit
    license_note = _source_note(user, src_kind, src_url, license_note)
    data = await file.read()
    if len(data) > 30 * 1024 * 1024:
        flash(request, "الملف أكبر من ٣٠ ميغابايت — قسّمه إلى ملفات أصغر.", "err")
        return back("/prospects")
    n, cols = P.queue_upload(s, user, file.filename or "upload.csv", data, license_note.strip())
    audit(s, user, "import", user, kind="prospects", rows=n, file=(file.filename or "")[:100])
    if not n:
        flash(request, "لم أجد عمود «الاسم» في الصف الأول. الأعمدة المقبولة: الاسم، النشاط، المدينة، الموقع، الهاتف، البريد، "
                       "الرقم الموحد، السجل التجاري، رابط المصدر (بالعربية أو الإنجليزية).", "err")
    else:
        flash(request, f"أُضيف {n} سجلًا إلى طابور المعالجة. الأعمدة المعروفة: {', '.join(cols)}. "
                       "تُعالج ضمن الدفعة اليومية أو عند «تشغيل دفعة».")
    return back("/prospects")


# ---------------------------------------------------------------- قائمة عملاء إدارية (ملف Excel كما هو)

def _split_table(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """صف العناوين = أول صف فيه ٢ خلايا أو أكثر غير فارغة (بعد أسطر العنوان)، وما بعده البيانات."""
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    for i, r in enumerate(rows[:15]):
        if sum(1 for c in r if str(c).strip()) >= 2:
            width = max(len(x) for x in rows)
            head = [str(c).strip() or f"عمود {j + 1}" for j, c in enumerate(list(r) + [""] * (width - len(r)))]
            body = [[str(c).strip() for c in list(x) + [""] * (width - len(x))] for x in rows[i + 1:]]
            return head, body
    return [], []


def _cell(v):
    """تنظيف عرض الخلية: 567775573.0 ← 567775573، و2018-08-25 00:00:00 ← 2018-08-25."""
    v = str(v or "").strip()
    if re.fullmatch(r"\d+\.0", v):
        v = v[:-2]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} 00:00:00", v):
        v = v[:10]
    return v


def _col_kinds(headers, rows) -> dict[int, str]:
    """نوع كل عمود للعرض: phone / email / url / date / num / long (نص طويل) / text."""
    from ..prospects import normalize_arabic_digits
    out = {}
    width = len(headers)
    sample = rows[:300]
    for j in range(width):
        vals = [_cell(r[j]) for r in sample if j < len(r) and _cell(r[j])]
        h = str(headers[j])
        if not vals:
            out[j] = "text"
            continue
        def share(fn):
            return sum(1 for v in vals if fn(v)) / len(vals)
        digits = lambda v: re.sub(r"[\s\-+()]", "", normalize_arabic_digits(v))  # noqa: E731
        if share(lambda v: "@" in v and "." in v) > .5:
            out[j] = "email"
        elif share(lambda v: v.lower().startswith(("http://", "https://", "www."))) > .5:
            out[j] = "url"
        elif any(w in h for w in ("جوال", "هاتف", "تلفون", "phone", "mobile", "واتس")) or \
                share(lambda v: re.fullmatch(r"(?:00966|966|0)?5\d{8}|(?:00966|966|0)?1\d{8}|9200\d{5}", digits(v)) is not None) > .6:
            out[j] = "phone"
        elif share(lambda v: re.fullmatch(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}", v) is not None) > .6:
            out[j] = "date"
        elif share(lambda v: re.fullmatch(r"[\d.,]+", v) is not None) > .8:
            out[j] = "num"
        elif sum(len(v) for v in vals) / len(vals) > 28:
            out[j] = "long"
        else:
            out[j] = "text"
    return out


def _phone_full(v: str) -> str:
    """الرقم كاملًا برمز الدولة: 567775573 ← +966567775573، و0551234567 ← +966551234567."""
    from ..normalize import normalize_phone
    from ..prospects import normalize_arabic_digits
    d = re.sub(r"[\s\-()]", "", normalize_arabic_digits(_cell(v)))
    if re.fullmatch(r"[15]\d{8}", d):
        d = "0" + d
    return normalize_phone(d) or _cell(v)


templates.env.globals.update(phone_full=_phone_full)


LIST_STATUS = {"replied": "تم التواصل — رد", "no_answer": "تم التواصل — لم يرد", "wrong_number": "رقم خاطئ",
               "do_not_contact": "طلب عدم التواصل"}


def _admin_lists_view(request, user, s, lid, q, page, cstate, base):
    from ..models import AdminList, AdminListContact
    lists = list(s.execute(select(AdminList.id, AdminList.name, AdminList.sheet, AdminList.created_at, AdminList.note)
                           .order_by(AdminList.id.desc())))
    cur = s.get(AdminList, lid) if lid else (s.get(AdminList, lists[0][0]) if lists else None)
    rows, total, kinds, cmap, counts = [], 0, {}, {}, {"all": 0, "none": 0, "done": 0, "replied": 0}
    if cur is not None:
        kinds = _col_kinds(cur.headers or [], cur.rows or [])
        cmap = {x.row_idx: x for x in s.scalars(select(AdminListContact).where(AdminListContact.list_id == cur.id))}
        data = list(enumerate(cur.rows or []))
        counts = {"all": len(data), "done": len(cmap), "none": len(data) - len(cmap),
                  "replied": sum(1 for x in cmap.values() if x.status == "replied")}
        if q:
            ql = q.strip().lower()
            data = [(i, r) for i, r in data if any(ql in str(c).lower() for c in r)]
        if cstate == "none":
            data = [(i, r) for i, r in data if i not in cmap]
        elif cstate == "done":
            data = [(i, r) for i, r in data if i in cmap]
        elif cstate in LIST_STATUS:
            data = [(i, r) for i, r in data if i in cmap and cmap[i].status == cstate]
        total = len(data)
        rows = data[(page - 1) * 200: page * 200]
    sizes = {i: len(s.get(AdminList, i).rows or []) for i, *_ in lists}
    skip_first = bool(cur and cur.headers and str(cur.headers[0]).strip() in ("م", "#", "No", "no", "رقم", "ت", "مسلسل"))
    return render(request, "admin_lists.html", user, s, lists=lists, cur=cur, rows=rows, total=total, q=q, page=page,
                  sizes=sizes, kinds=kinds, cell=_cell, skip_first=skip_first, cmap=cmap, cstate=cstate, counts=counts,
                  LIST_STATUS=LIST_STATUS, base=base, can_manage=user.role in ("admin", "manager"))


@app.get("/admin-lists", response_class=HTMLResponse)
def admin_lists(request: Request, lid: Optional[int] = None, q: str = "", page: int = 1, cstate: str = "",
                user: User = Depends(require_manager), s=Depends(get_db)):
    return _admin_lists_view(request, user, s, lid, q, page, cstate, "/admin-lists")


@app.get("/portal/lists", response_class=HTMLResponse)
def portal_lists(request: Request, lid: Optional[int] = None, q: str = "", page: int = 1, cstate: str = "",
                 user: User = Depends(staff_user), s=Depends(get_db)):
    """بوابة الموظفين: نفس القائمة الإدارية مع تسجيل التواصل (بلا رفع أو حذف)."""
    return _admin_lists_view(request, user, s, lid, q, page, cstate, "/portal/lists")


def _list_contact(request, s, user, lid, idx, status, note, confirm, base):
    from ..models import AdminList, AdminListContact
    al = s.get(AdminList, lid)
    if al is None or not (0 <= idx < len(al.rows or [])):
        raise HTTPException(404)
    ret = _safe_back(request, f"{base}?lid={lid}")
    x = s.scalar(select(AdminListContact).where(AdminListContact.list_id == lid, AdminListContact.row_idx == idx))
    name_col = next((j for j, k in enumerate(al.headers or []) if "اسم" in str(k) or "name" in str(k).lower()), 1 if len(al.rows[idx]) > 1 else 0)
    who = _cell(al.rows[idx][name_col]) if name_col < len(al.rows[idx]) else f"صف {idx + 1}"
    if status == "reset":
        if x is not None:
            s.delete(x)
            flash(request, f"أُلغيت حالة التواصل لـ «{who}».")
        return back(ret)
    if status not in LIST_STATUS:
        flash(request, "اختر نتيجة التواصل.", "err")
        return back(ret)
    if x is not None and x.by_id != user.id and not confirm:
        flash(request, f"تم التواصل مع «{who}» بواسطة {x.by_name} في {fmt(x.at)} ({LIST_STATUS.get(x.status)}). "
                       "لا تكرر الاتصال — وإن كان لديك تحديث جديد فاضغط «حفظ» مرة أخرى مع تأكيد.", "err")
        return back(ret)
    if x is None:
        x = AdminListContact(list_id=lid, row_idx=idx, history=[])
        s.add(x)
    hist = list(x.history or [])
    if x.status:
        hist.append({"status": x.status, "note": x.note, "by": x.by_name, "at": x.at.isoformat(timespec="minutes") if x.at else ""})
    x.status, x.note, x.by_id, x.by_name, x.at, x.history = status, (note or "").strip()[:500], user.id, user.name, now_utc(), hist[-20:]
    flash(request, f"سُجّل «{LIST_STATUS[status]}» لـ «{who}» — يظهر لكل الموظفين الآن.")
    return back(ret)


@app.post("/admin-lists/{lid}/row/{idx}/contact")
def admin_list_contact(lid: int, idx: int, request: Request, status: str = Form(""), note: str = Form(""),
                       confirm: str = Form(""), user: User = Depends(require_manager), s=Depends(get_db)):
    return _list_contact(request, s, user, lid, idx, status, note, bool(confirm), "/admin-lists")


@app.post("/portal/lists/{lid}/row/{idx}/contact")
def portal_list_contact(lid: int, idx: int, request: Request, status: str = Form(""), note: str = Form(""),
                        confirm: str = Form(""), user: User = Depends(staff_user), s=Depends(get_db)):
    return _list_contact(request, s, user, lid, idx, status, note, bool(confirm), "/portal/lists")


@app.post("/admin-lists/upload")
async def admin_lists_upload(request: Request, file: UploadFile = File(...), note: str = Form(""),
                             user: User = Depends(require_manager), s=Depends(get_db)):
    from .. import prospects as P
    from ..auth import audit
    from ..models import AdminList
    fname = file.filename or "list.xlsx"
    data = await file.read()
    if len(data) > 40 * 1024 * 1024:
        flash(request, "الملف أكبر من ٤٠ ميغابايت — قسّمه.", "err")
        return back("/admin-lists")
    try:
        sheets = P._sheets(fname, data)
    except P.UploadError as e:
        flash(request, f"«{fname}»: {e}", "err")
        return back("/admin-lists")
    except Exception as e:  # noqa: BLE001
        flash(request, f"«{fname}»: تعذّرت قراءة الملف ({type(e).__name__}). احفظه بصيغة xlsx وأعد رفعه.", "err")
        return back("/admin-lists")
    made, first = 0, None
    names = []
    if fname.lower().endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        import io
        from openpyxl import load_workbook
        names = [ws.title for ws in load_workbook(io.BytesIO(data), read_only=True).worksheets]
    for i, rows in enumerate(sheets):
        head, body = _split_table(rows)
        if not body:
            continue
        al = AdminList(name=fname[:200], sheet=(names[i] if i < len(names) else "")[:100], headers=head, rows=body,
                       note=(note or "").strip()[:500], uploaded_by=user.id)
        s.add(al)
        s.flush()
        first = first or al.id
        made += 1
    audit(s, user, "import", user, kind="admin_list", file=fname[:100], sheets=made)
    if not made:
        flash(request, f"«{fname}»: الملف فارغ أو لم أجد فيه جدولًا.", "err")
        return back("/admin-lists")
    flash(request, f"«{fname}»: رُفع {made} {'ورقة' if made == 1 else 'أوراق'} — تظهر القائمة كما هي بكل أعمدتها.")
    return back(f"/admin-lists?lid={first}")


@app.post("/admin-lists/{lid}/delete")
def admin_lists_delete(lid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from ..models import AdminList
    al = s.get(AdminList, lid)
    if al:
        s.delete(al)
        flash(request, f"حُذفت القائمة «{al.name}».")
    return back("/admin-lists")


@app.post("/admin-lists/{lid}/verify")
def admin_lists_verify(lid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    """اختياري: إرسال صفوف القائمة إلى «عملاء عشوائيون» ثم التحقق — القائمة الأصلية تبقى كما هي."""
    from .. import prospects as P
    from ..models import AdminList, ProspectPool
    al = s.get(AdminList, lid)
    if al is None:
        raise HTTPException(404)
    m = P._map_headers(al.headers or [])
    if "name" not in m:
        m = P._guess_columns(al.rows or [])
    n = 0
    for r in al.rows or []:
        get = lambda k: (r[m[k]] if k in m and m[k] < len(r) else "")  # noqa: E731
        if not get("name"):
            continue
        s.add(ProspectPool(file_name=al.name[:200], license_note=f"قائمة عملاء إدارية #{al.id}", kind="random",
                           status="queued", row={k: get(k) for k in P.COLS}, uploaded_by=user.id))
        n += 1
    al.sent_to_verify_at = now_utc()
    flash(request, f"أُرسل {n} صفًا للتحقق — تظهر نتيجتها في «العملاء المحتملون». القائمة الإدارية بقيت كما هي."
          if n else "لم أتعرف على عمود الاسم في هذه القائمة.", "ok" if n else "err")
    return back(f"/admin-lists?lid={lid}")


@app.get("/portal/lists/{lid}.xlsx")
def portal_lists_xlsx(lid: int, user: User = Depends(staff_user), s=Depends(get_db)):
    _require(user, "export")
    return admin_lists_xlsx(lid, user, s)


@app.get("/admin-lists/{lid}.xlsx")
def admin_lists_xlsx(lid: int, user: User = Depends(require_manager), s=Depends(get_db)):
    from ..models import AdminList
    from ..xlsx import response, workbook
    from ..models import AdminListContact
    al = s.get(AdminList, lid)
    if al is None:
        raise HTTPException(404)
    cmap = {x.row_idx: x for x in s.scalars(select(AdminListContact).where(AdminListContact.list_id == lid))}
    rows = []
    for i, r in enumerate(al.rows or []):
        x = cmap.get(i)
        rows.append([_cell(c) for c in r] + (["نعم", "رد" if x.status == "replied" else ("لم يرد" if x.status == "no_answer" else LIST_STATUS.get(x.status, "")),
                                              x.by_name, fmt(x.at), x.note] if x else ["لا", "", "", "", ""]))
    head = list(al.headers or []) + ["تم التواصل؟", "هل رد؟", "الموظف", "وقت التواصل", "ملاحظة"]
    return response(workbook([((al.sheet or "القائمة")[:31], head, rows)]), f"admin-list-{lid}.xlsx")


# ---------------------------------------------------------------- عملاء عشوائيون (قوائم غير متحقق منها)

def _source_note(user, kind: str, url: str, note: str) -> str:
    """مصدر الملف اختياري: «من جهازي» افتراضيًا، أو رابط، مع ملاحظة إن كُتبت."""
    url, note = (url or "").strip(), (note or "").strip()
    if kind == "url" and url:
        base = url
    else:
        base = f"ملف من جهاز {user.name}"
    return (base + (f" — {note}" if note else ""))[:500]

def _random_q(q: str, status: str):
    from ..models import ProspectPool
    qq = select(ProspectPool).where(ProspectPool.kind == "random")
    if status:
        qq = qq.where(ProspectPool.status == status)
    if q:
        from sqlalchemy import cast, String as _S
        qq = qq.where(cast(ProspectPool.row, _S).like(f"%{q}%") | ProspectPool.file_name.like(f"%{q}%"))
    return qq


@app.get("/random-leads", response_class=HTMLResponse)
def random_leads(request: Request, q: str = "", status: str = "random", page: int = 1,
                 user: User = Depends(require_manager), s=Depends(get_db)):
    from ..models import ProspectPool
    qq = _random_q(q, status)
    total = s.scalar(select(func.count()).select_from(qq.subquery())) or 0
    rows = list(s.scalars(qq.order_by(ProspectPool.id).offset((page - 1) * 100).limit(100)))
    counts = {k: s.scalar(select(func.count(ProspectPool.id)).where(ProspectPool.kind == "random", ProspectPool.status == k)) or 0
              for k in ("random", "queued", "done")}
    files = list(s.execute(select(ProspectPool.file_name, ProspectPool.license_note, func.count(ProspectPool.id))
                           .where(ProspectPool.kind == "random").group_by(ProspectPool.file_name, ProspectPool.license_note)))
    return render(request, "random_leads.html", user, s, rows=rows, total=total, q=q, status=status, page=page,
                  counts=counts, files=files, qs=str(request.query_params))


@app.post("/random-leads/upload")
async def random_leads_upload(request: Request, file: UploadFile = File(...), source_note: str = Form(""),
                              src_kind: str = Form("device"), src_url: str = Form(""),
                              user: User = Depends(require_manager), s=Depends(get_db)):
    from .. import prospects as P
    from ..auth import audit
    data = await file.read()
    if len(data) > 40 * 1024 * 1024:
        flash(request, "الملف أكبر من ٤٠ ميغابايت — قسّمه.", "err")
        return back("/random-leads")
    fname = file.filename or "list.csv"
    try:
        n, cols = P.queue_upload(s, user, fname, data, _source_note(user, src_kind, src_url, source_note), kind="random")
    except P.UploadError as e:
        flash(request, f"«{fname}»: {e}", "err")
        return back("/random-leads")
    except Exception as e:  # noqa: BLE001
        log.warning("random upload failed: %s", type(e).__name__)
        flash(request, f"«{fname}»: تعذّرت قراءة الملف ({type(e).__name__}). احفظه بصيغة xlsx أو CSV وأعد رفعه.", "err")
        return back("/random-leads")
    if not n:
        flash(request, f"«{fname}»: قُرئ الملف لكن لم أجد أسطرًا فيها اسم منشأة مع هاتف أو بريد. تأكد أن فيه عمودًا للاسم "
                       "(مثل «الاسم» أو «اسم الشركة») وعمودًا للهاتف أو البريد.", "err")
        return back("/random-leads")
    audit(s, user, "import", user, kind="random_leads", rows=n, file=(file.filename or "")[:100])
    flash(request, f"«{fname}»: أُضيف {n} سجلًا إلى «عملاء عشوائيون». لم يُتحقق منها بعد — انقلها للتحقق لتدخل «العملاء المحتملون».")
    return back("/random-leads")


@app.post("/random-leads/verify")
async def random_leads_verify(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    """نقل السجلات المحددة (أو كل نتائج البحث) إلى طابور التحقق: تكرار ← موقع المنشأة ← أولوية."""
    from ..models import ProspectPool
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    if form.get("all"):
        ids = [p.id for p in s.scalars(_random_q(form.get("q") or "", "random"))]
    n = 0
    for p in s.scalars(select(ProspectPool).where(ProspectPool.id.in_(ids or [0]), ProspectPool.status == "random")):
        p.status, n = "queued", n + 1
    flash(request, f"نُقل {n} سجلًا للتحقق. تُعالج في الدفعة التالية من «العملاء المحتملون» (أو اضغط «تشغيل دفعة» هناك).")
    return back("/random-leads")


@app.get("/random-leads.xlsx")
def random_leads_xlsx(request: Request, q: str = "", status: str = "", user: User = Depends(require_manager), s=Depends(get_db)):
    from ..xlsx import response, workbook
    st = {"random": "لم يُتحقق منها", "queued": "في طابور التحقق", "done": "عولجت"}
    rows = [[p.row.get("name", ""), p.row.get("phone", ""), p.row.get("email", ""), p.row.get("city", ""), p.row.get("activity", ""),
             p.row.get("website", ""), p.file_name, p.license_note, st.get(p.status, p.status), p.result or ""]
            for p in s.scalars(_random_q(q, status))]
    head = ["الاسم", "الهاتف", "البريد", "المدينة", "النشاط", "الموقع", "الملف", "مصدر القائمة", "الحالة", "نتيجة التحقق"]
    return response(workbook([("عملاء عشوائيون", head, rows)]), "random-leads.xlsx")


@app.post("/prospects/auto")
def prospects_auto(request: Request, enabled: str = Form(""), time_: str = Form("05:30", alias="time"),
                   target: int = Form(1000), google_budget_usd: float = Form(10.0),
                   user: User = Depends(require_manager), s=Depends(get_db)):
    from .. import prospects as P
    if not re.fullmatch(r"\d{2}:\d{2}", time_ or ""):
        time_ = "05:30"
    cfg = P.set_auto(s, enabled=bool(enabled), time=time_, target=max(10, min(int(target), 5000)),
                     google_budget_usd=max(0.0, min(float(google_budget_usd), 500.0)))
    flash(request, (f"التشغيل اليومي مفعّل الساعة {cfg['time']} بتوقيت الرياض — الهدف {cfg['target']} منشأة، "
                    f"وميزانية خرائط Google ${cfg['google_budget_usd']:.2f} يوميًا.") if cfg["enabled"] else "التشغيل اليومي متوقف.")
    return back("/prospects")


@app.post("/prospects/stop")
def prospects_stop(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from ..models import ProspectRun
    for r in s.scalars(select(ProspectRun).where(ProspectRun.status.in_(["pending", "running"]))):
        r.status = "stopped"
    flash(request, "أُوقفت الدفعة. ما جُمع يبقى في القائمة.")
    return back("/prospects")


@app.post("/companies/{cid}/enrich")
def company_enrich_now(cid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    """يقدّم العميل في طابور إعادة البحث عن الهاتف/البريد (ينفذه العامل خلال ١٠ دقائق)."""
    c = s.get(Company, cid)
    if c is None:
        raise HTTPException(404)
    if c.data_status == "incomplete":
        c.retry_enrich_at, c.enrich_attempts = None, 0
        flash(request, "سيبحث الخادم عن الهاتف والبريد لهذا العميل خلال ١٠ دقائق؛ حدّث الصفحة بعدها.")
    return back(f"/companies/{cid}")


# ---------------------------------------------------------------- الفئات المستهدفة

@app.get("/targets", response_class=HTMLResponse)
def targets(request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    camps = list(s.scalars(select(Campaign).order_by(Campaign.id.desc())))
    today = riyadh_today()
    progress = {}
    for c in camps:
        for cat in c.categories:
            progress[cat.id] = s.scalar(select(func.count(Company.id)).where(
                Company.category_id == cat.id, Company.counted_on == today, Company.data_status == "complete")) or 0
    return render(request, "targets.html", user, s, camps=camps, progress=progress,
                  sched=jobs.get_setting("schedule", jobs.DEFAULT_SCHEDULE))


@app.post("/targets/parse", response_class=HTMLResponse)
def targets_parse(request: Request, text: str = Form(...), daily_target: int = Form(400),
                  user: User = Depends(require_manager), s=Depends(get_db)):
    parsed = parse_request(text, daily_target)
    offers = list(s.scalars(select(Offer).order_by(Offer.id)))
    return render(request, "target_review.html", user, s, parsed=parsed, text=text, offers=offers, camp=None)


def _split(v: str) -> list[str]:
    return [x.strip() for x in (v or "").replace("،", ",").replace("\n", ",").split(",") if x.strip()]


def _save_categories(s, camp: Campaign, form) -> None:
    n = int(form.get("cat_count", 0))
    keep_ids = set()
    for i in range(n):
        if form.get(f"cat_{i}_delete"):
            continue
        if not form.get(f"cat_{i}_id") and not (form.get(f"cat_{i}_label") or "").strip():
            continue  # خانة «إضافة فئة جديدة» فارغة
        cid = form.get(f"cat_{i}_id")
        cat = s.get(Category, int(cid)) if cid else None
        if cat is None:
            cat = Category(campaign_id=camp.id, sector_key=form.get(f"cat_{i}_sector_key") or "custom")
            s.add(cat)
        cat.label = form.get(f"cat_{i}_label") or cat.sector_key
        cat.sector_key = form.get(f"cat_{i}_sector_key") or cat.sector_key
        cat.cities = _split(form.get(f"cat_{i}_cities", ""))
        cat.keywords_ar = _split(form.get(f"cat_{i}_keywords_ar", ""))
        cat.keywords_en = _split(form.get(f"cat_{i}_keywords_en", ""))
        cat.priority_signals = _split(form.get(f"cat_{i}_signals", ""))
        cat.priority = int(form.get(f"cat_{i}_priority") or i + 1)
        cat.quota = max(int(form.get(f"cat_{i}_quota") or 0), 0)
        cat.active = form.get(f"cat_{i}_active") == "on"
        cat.is_experiment = form.get(f"cat_{i}_experiment") == "on"
        oid = form.get(f"cat_{i}_offer_id")
        cat.offer_id = int(oid) if oid else None
        s.flush()
        keep_ids.add(cat.id)
    for cat in list(camp.categories):
        if cat.id not in keep_ids:
            s.delete(cat)
    s.flush()
    s.refresh(camp)
    camp.daily_target = sum(c.quota for c in camp.categories if c.active)


def _save_campaign_fields(camp: Campaign, form) -> None:
    camp.name = form.get("name") or camp.name
    camp.exclusions = _split(form.get("exclusions", ""))
    camp.exclude_existing_customers = form.get("exclude_existing_customers") == "on"
    camp.allow_backfill = form.get("allow_backfill") == "on"
    camp.send_enabled = form.get("send_enabled") == "on"
    camp.send_days = [int(x) for x in form.getlist("send_days")] if hasattr(form, "getlist") else camp.send_days
    camp.send_start_hour = int(form.get("send_start_hour") or 9)
    camp.send_end_hour = int(form.get("send_end_hour") or 16)
    camp.daily_send_cap = int(form.get("daily_send_cap") or 150)
    camp.followup_days = int(form.get("followup_days") or 0)
    camp.test_recipient = normalize_email(form.get("test_recipient")) or None


@app.post("/targets/save")
async def targets_save(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    form = await request.form()
    camp = Campaign(name=form.get("name") or "حملة جديدة", request_text=form.get("request_text", ""),
                    created_by=user.id, status="draft")
    s.add(camp)
    s.flush()
    _save_campaign_fields(camp, form)
    _save_categories(s, camp, form)
    flash(request, "حُفظت الحملة كمسودة. راجع العروض ثم اعتمدها لتبدأ العمل اليومي.")
    return back(f"/targets/{camp.id}")


@app.get("/targets/{cid}", response_class=HTMLResponse)
def target_edit(cid: int, request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    camp = s.get(Campaign, cid) or _404()
    offers = list(s.scalars(select(Offer).order_by(Offer.id)))
    parsed = {"name": camp.name, "exclusions": camp.exclusions, "categories": [
        {"id": c.id, "sector_key": c.sector_key, "label": c.label, "cities": c.cities, "keywords_ar": c.keywords_ar,
         "keywords_en": c.keywords_en, "priority": c.priority, "priority_signals": c.priority_signals,
         "quota": c.quota, "active": c.active, "offer_id": c.offer_id, "is_experiment": c.is_experiment}
        for c in camp.categories],
        "exclude_existing_customers": camp.exclude_existing_customers}
    runs = list(s.scalars(select(DiscoveryRun).where(DiscoveryRun.campaign_id == cid)
                          .order_by(DiscoveryRun.id.desc()).limit(10)))
    return render(request, "target_review.html", user, s, parsed=parsed, text=camp.request_text, offers=offers,
                  camp=camp, runs=runs)


@app.post("/targets/{cid}")
async def target_update(cid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    camp = s.get(Campaign, cid) or _404()
    form = await request.form()
    _save_campaign_fields(camp, form)
    _save_categories(s, camp, form)
    flash(request, "حُفظت التعديلات")
    return back(f"/targets/{cid}")


@app.post("/targets/{cid}/status")
def target_status(cid: int, request: Request, action: str = Form(...), user: User = Depends(require_manager),
                  s=Depends(get_db)):
    camp = s.get(Campaign, cid) or _404()
    if action == "activate":
        missing = [c.label for c in camp.categories if c.active and c.offer_id and not c.offer.approved] \
            if settings.auto_outreach_enabled else []
        if missing:
            flash(request, "اعتمد العروض المرتبطة أولًا: " + "، ".join(missing), "err")
            return back(f"/targets/{cid}")
        camp.status, camp.approved_at = "active", now_utc()
        flash(request, "اعتُمدت الحملة وأصبحت نشطة. ستعمل يوميًا في الوقت المحدد.")
    elif action == "pause":
        camp.status = "paused"
        flash(request, "أُوقفت الحملة مؤقتًا")
    return back(f"/targets/{cid}")


@app.post("/targets/{cid}/run")
def target_run(cid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    camp = s.get(Campaign, cid) or _404()
    if camp.status != "active":
        flash(request, "فعّل الحملة أولًا", "err")
        return back(f"/targets/{cid}")
    from ..discovery.runner import run_campaign_discovery
    _bg(run_campaign_discovery, cid)
    flash(request, "بدأ التشغيل في الخلفية على الخادم. تابع التقدم من سجل التشغيل.")
    return back("/runs")


@app.post("/schedule")
def save_schedule(request: Request, discovery_time: str = Form(...), daily_report_time: str = Form(...),
                  user: User = Depends(require_manager)):
    jobs.put_setting("schedule", {"discovery_time": discovery_time, "daily_report_time": daily_report_time})
    flash(request, f"سيعمل الاكتشاف يوميًا الساعة {discovery_time} بتوقيت الرياض")
    return back("/targets")


# ---------------------------------------------------------------- البحث عن العملاء (الأداة الرئيسية للمرحلة الحالية)

@app.get("/prospecting", response_class=HTMLResponse)
def prospecting_page(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from .. import prospecting as P
    st = P.status(s)
    from .. import geo
    from ..health import search_status
    return render(request, "prospecting.html", user, s, st=st, ACTIVITIES=P.ACTIVITIES, DIR_SECTORS=P.SECTORS,
                  health=search_status(s),
                  GEO=geo, GEO_REGIONS=geo.REGIONS, GEO_PLACES=geo.PLACES,
                  MAIN_CITIES=P.MAIN_CITIES,
                  REGIONS=list(P.REGIONS.keys()), ai_ready=settings.ai_enabled,
                  sources={"وكيل البحث (Claude: بحث الويب وقراءة الصفحات)": settings.ai_enabled,
                           "خرائط Google (GOOGLE_PLACES_API_KEY)": bool(settings.google_places_api_key),
                           "بحث Brave (BRAVE_SEARCH_API_KEY)": bool(settings.brave_search_api_key)})


def _scope_from_form(form) -> dict:
    """النطاق من النموذج: «جميع مناطق المملكة»، أو مناطق كاملة (مع استثناءات) + مدن محددة + أسماء مكتوبة من الدليل."""
    from .. import geo
    from ..prospecting import ScopeError
    if form.get("scope_mode") == "all":
        return {"all": True, "regions": [], "places": [], "exclude": []}
    regions = [r for r in form.getlist("regions") if r in geo.REGIONS]
    checked = {p for p in form.getlist("places") if p in geo.PLACES}
    # منطقة محددة كاملة؛ وإن أُرسلت بعض مدنها فقط فالبقية استثناء («اختيار جزئي»)
    exclude = [p for r in regions if any(x in checked for x in geo.REGIONS[r]["places"])
               for p in geo.REGIONS[r]["places"] if p not in checked]
    places = [p for p in checked if geo.PLACES[p]["region"] not in regions]
    for raw in (form.get("extra_cities") or "").replace("،", ",").split(","):
        raw = raw.strip()
        if not raw:
            continue
        name, _, reg = raw.partition(" — ")
        hits = geo.matches(name)
        if reg:
            hits = [h for h in hits if geo.REGIONS[geo.PLACES[h]["region"]]["ar"] == reg.strip()]
        if not hits:
            raise ScopeError(f"«{raw}» غير موجود في دليل الأماكن — اختره من القائمة المقترحة")
        if len(set(hits)) > 1:
            raise ScopeError(f"«{name}» يطابق أكثر من مكان: " + "، ".join(geo.label(h, True) for h in hits) +
                             " — اختر المقصود من القائمة")
        if hits[0] not in places and geo.PLACES[hits[0]]["region"] not in regions:
            places.append(hits[0])
        if hits[0] in exclude:
            exclude.remove(hits[0])
    return {"all": False, "regions": regions, "places": places, "exclude": exclude}


@app.post("/prospecting")
async def prospecting_save(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from .. import prospecting as P
    from ..auth import audit
    form = await request.form()
    try:
        scope = _scope_from_form(form)
        camp = P.save_config(s, scope=scope, activities=list(form.getlist("activities")),
                             all_activities=bool(form.get("all_activities")),
                             daily_target=int(form.get("daily_target") or 400), scheduled=bool(form.get("scheduled")),
                             time=(form.get("time") or "06:00")[:5], user_id=user.id)
    except P.ScopeError as e:
        flash(request, str(e), "err")
        return back("/prospecting")
    audit(s, user, "prospecting_config", user, after=P.get_config(s))
    msg = f"حُفظ الإعداد: الهدف {camp.daily_target} منشأة يوميًا. "
    msg += (f"التشغيل اليومي مفعّل الساعة {P.get_config(s)['time']} بتوقيت الرياض ويعمل على الخادم ولو أُغلقت اللوحة."
            if camp.status == "active" else "التشغيل اليومي متوقف؛ يمكنك البحث الآن يدويًا.")
    if form.get("run_now"):
        s.flush()
        n = int(form.get("batch") or 0) or None
        P.request_run(s, camp.id, n, user.id)
        msg += " وبدأ البحث الآن على الخادم (يلتقطه خلال دقيقة)."
    flash(request, msg)
    return back("/prospecting")


@app.post("/prospecting/run")
def prospecting_run(request: Request, batch: int = Form(0), user: User = Depends(require_manager), s=Depends(get_db)):
    from .. import prospecting as P
    cfg = P.get_config(s)
    if not cfg.get("campaign_id"):
        flash(request, "احفظ الإعداد أولًا", "err")
        return back("/prospecting")
    req = P.request_run(s, cfg["campaign_id"], batch or None, user.id)
    flash(request, "بدأ البحث على الخادم؛ تظهر النتائج تباعًا في «عملاء اليوم»." if req.get("by") == user.id and
          req.get("state") == "pending" else "يوجد بحث قيد التشغيل بالفعل.")
    return back("/prospecting")


# ---------------------------------------------------------------- الشركات

def _companies_query(user, status, q, city, sector, source, operator, signals, kind, owner, contact="", found=""):
    """استعلام قائمة الشركات بالفلاتر — نفسه للصفحة ولملف Excel ولأرقام بطاقات لوحة التحكم."""
    from ..models import CompanySignal
    qq = scope_companies(select(Company), user).where(Company.data_status == status)
    if found == "today":  # عملاء جدد اليوم: المكتملون المحتسبون في هدف اليوم
        qq = qq.where(Company.counted_on == riyadh_today())
    if contact:
        qq = qq.where(Company.contact_status == contact)
    if kind == "test":
        qq = qq.where(Company.is_test.is_(True))
    elif kind == "real":
        qq = qq.where(Company.is_test.is_(False))
    if owner == "none":
        qq = qq.where(Company.owner_id.is_(None))
    elif owner.isdigit():
        qq = qq.where(Company.owner_id == int(owner))
    if source:
        qq = qq.where(Company.lead_source.like(f"{source}%"))
    if operator:
        qq = qq.where(Company.operator_status == operator)
    if signals:
        qq = qq.where(Company.id.in_(select(CompanySignal.company_id)))
    if q:
        from ..models import Contact as _Ct
        qq = qq.where(Company.name.like(f"%{q}%") | Company.domain.like(f"%{q}%") | Company.activity.like(f"%{q}%") |
                      Company.id.in_(select(_Ct.company_id).where(_Ct.value.like(f"%{q.strip().lower()}%"))))
    if city:
        qq = qq.where(Company.city == city)
    if sector:
        qq = qq.where(Company.sector_key == sector)
    return qq


@app.get("/companies", response_class=HTMLResponse)
def companies(request: Request, status: str = "complete", q: str = "", city: str = "", sector: str = "",
              page: int = 1, source: str = "", operator: str = "", signals: str = "", kind: str = "",
              owner: str = "", contact: str = "", found: str = "", user: User = Depends(current_user), s=Depends(get_db)):
    from ..models import CompanySignal
    qq = _companies_query(user, status, q, city, sector, source, operator, signals, kind, owner, contact, found)
    total = s.scalar(select(func.count()).select_from(qq.subquery())) or 0
    rows = list(s.scalars(qq.order_by(Company.first_found_at.desc()).offset((page - 1) * 50).limit(50)))
    counts = {st: s.scalar(scope_companies(select(func.count(Company.id)), user).where(Company.data_status == st)) or 0
              for st in ("complete", "incomplete")}
    from ..companies import missing_fields
    from ..leads import source_label
    sig = {cid: n for cid, n in s.execute(select(CompanySignal.company_id, func.count(CompanySignal.id))
                                          .where(CompanySignal.company_id.in_([r.id for r in rows]))
                                          .group_by(CompanySignal.company_id))}
    return render(request, "companies.html", user, s, rows=rows, status=status, q=q, city=city, sector=sector,
                  total=total, page=page, counts=counts, missing_fields=missing_fields, source=source,
                  operator=operator, signals=signals, sig=sig, source_label=source_label, kind=kind, owner=owner,
                  contact=contact, found=found, qs=str(request.query_params), csum=_csum(s, rows),
                  staff=list(s.scalars(select(User).where(User.role == "rep").order_by(User.name))),
                  test_total=s.scalar(select(func.count(Company.id)).where(Company.is_test.is_(True))) or 0)


def _contact_ctx(s, c):
    from ..contact import STATUSES, UPDATE_CHOICES, history, summary
    return {"cs": summary(s, c), "chist": history(s, c.id), "STATUSES": STATUSES, "UPDATE_CHOICES": UPDATE_CHOICES,
            "owners": list(s.scalars(select(User).where(User.active.is_(True)).order_by(User.name))),
            "base": f"/companies/{c.id}"}


def _csum(s, companies_):
    from ..contact import summary
    return {c.id: summary(s, c) for c in companies_}


def _customer_row(s, c):
    """أعمدة العميل الأساسية + أعمدة التواصل الموحدة (لكل تقارير العملاء)."""
    from ..reports import STAGE_LABELS as _SL
    from ..xlsx import contact_cells
    ph, em = c.primary("phone"), c.primary("email")
    return ([c.name, c.activity, c.city, format_phone_display(ph.value) if ph else "", ph.source_url if ph else "",
             em.value if em else "", em.source_url if em else "", c.decision_unit or "", c.website or "",
             "مكتمل" if c.data_status == "complete" else "قيد الاستكمال", fmt(c.first_found_at)]
            + contact_cells(s, c))


CUSTOMER_HEADERS = ["المنشأة", "النشاط", "المدينة", "الهاتف", "مصدر الهاتف", "البريد", "مصدر البريد",
                    "جهة القرار (مسؤول الشركة)", "الموقع", "اكتمال البيانات", "تاريخ الجمع"]


def _contact_start(request, s, user, c, back_to):
    from ..contact import ContactBlocked, start_contact
    try:
        start_contact(s, c, user)
        flash(request, f"بدأت التواصل مع {c.name}. يظهر للزملاء «جارٍ التواصل بواسطة {user.name}». حدّث المتابعة بعد الاتصال.")
    except ContactBlocked as e:
        flash(request, str(e), "err")
    return back(back_to)


def _contact_update(request, s, user, c, form, back_to, owner_choices):
    from ..contact import ContactBlocked, update_followup
    status = form.get("status") or ""
    nxt = None
    if form.get("next_at"):
        try:
            nxt = riyadh_to_utc(datetime.fromisoformat(form.get("next_at")))
        except ValueError:
            flash(request, "موعد المتابعة غير صحيح", "err")
            return back(back_to)
    owner_id = int(form.get("next_owner") or 0) or None
    if owner_id and owner_id not in owner_choices:
        raise HTTPException(403, "لا يمكنك إسناد المتابعة لهذا الموظف")
    try:
        update_followup(s, c, user, status, form.get("note") or "", nxt, owner_id, bool(form.get("reached_dm")))
        flash(request, "حُفظت المتابعة وظهرت لكل الزملاء فورًا.")
    except (ContactBlocked, ValueError) as e:
        flash(request, str(e), "err")
    return back(back_to)


def _safe_back(request, default):
    from urllib.parse import urlparse as _up
    ref = request.headers.get("referer") or ""
    p = _up(ref)
    return (p.path + ("?" + p.query if p.query else "")) if p.path.startswith("/") and not p.path.startswith("//") else default


@app.post("/companies/{cid}/contact/start")
def company_contact_start(cid: int, request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    c = s.get(Company, cid) or _404()
    return _contact_start(request, s, user, c, _safe_back(request, f"/companies/{cid}"))


@app.post("/companies/{cid}/contact/update")
async def company_contact_update(cid: int, request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    c = s.get(Company, cid) or _404()
    form = await request.form()
    owners = {u.id for u in s.scalars(select(User).where(User.active.is_(True)))}
    return _contact_update(request, s, user, c, form, _safe_back(request, f"/companies/{cid}"), owners)


# ---------------------------------------------------------------- اكتشاف عملاء جدد (أدلة النقل والمقاولين والمصانع)

DISCOVER_DEFAULT = ["transport.freight", "transport.parcels", "transport.cold_chain", "construction.contractors",
                    "industry.food_factories", "industry.water_factories", "industry.building_material_factories"]


@app.get("/discover", response_class=HTMLResponse)
def discover_page(request: Request, run: Optional[int] = None, user: User = Depends(require_manager), s=Depends(get_db)):
    from .. import directory as D, geo
    from ..activities import ACTIVITIES, SECTORS
    from ..models import DirectoryRun, LeadStub
    runs = list(s.scalars(select(DirectoryRun).order_by(DirectoryRun.id.desc()).limit(10)))
    cur = s.get(DirectoryRun, run) if run else (runs[0] if runs else None)
    stubs = list(s.scalars(select(LeadStub).where(LeadStub.run_id == cur.id).order_by(LeadStub.id))) if cur else []
    busy = any(r.status in ("pending", "running") for r in runs)
    rv = {}
    for r in runs:
        st = {k: v for k, v in (r.stats or {}).items() if not k.startswith("_") and isinstance(v, dict)}
        rv[r.id] = {"labels": "، ".join(D.SOURCES.get(x, {}).get("label", x) for x in (r.sources or [])),
                    **{f: sum(v.get(f, 0) for v in st.values()) for f in ("discovered", "complete", "incomplete",
                                                                        "duplicate", "existing_customer")},
                    "spent": (r.stats or {}).get("_spent", 0), "minutes": (r.stats or {}).get("_minutes")}
        rv[r.id]["new"] = rv[r.id]["discovered"] - rv[r.id]["duplicate"] - rv[r.id]["existing_customer"]
    from datetime import date as _date
    today_rows = D.daily_by_source(s)
    auto = D.get_auto(s)
    return render(request, "discover.html", user, s, sources=D.source_status(s), runs=runs, cur=cur, stubs=stubs, rv=rv,
                  today_rows=today_rows, auto=auto,
                  today_new_complete=sum(x["complete"] for x in today_rows if x["key"] != "prospecting"),
                  busy=busy, ACTIVITIES=ACTIVITIES, DIR_SECTORS=SECTORS, GEO_REGIONS=geo.REGIONS,
                  defaults=DISCOVER_DEFAULT, KIND={"works": ("ok", "يعمل"), "needs_key": ("warn", "يحتاج ربطًا (مفتاح)"),
                                                   "needs_file": ("warn", "ينتظر ملفًا"),
                                                   "blocked": ("err", "غير متاح آليًا من الخادم")})


@app.post("/discover/auto")
def discover_auto(request: Request, enabled: str = Form(""), time: str = Form("05:30"), target: int = Form(400),
                  budget: float = Form(20), user: User = Depends(require_manager), s=Depends(get_db)):
    from .. import directory as D
    from ..auth import audit
    v = D.set_auto(s, enabled=bool(enabled), time=(time or "05:30")[:5], target_complete=max(1, min(target, 5000)),
                   budget_usd=max(1.0, min(budget, 2000.0)))
    audit(s, user, "directory_auto", None, **{k: v[k] for k in ("enabled", "time", "target_complete", "budget_usd")})
    flash(request, f"البحث اليومي التلقائي {'مفعّل' if v['enabled'] else 'متوقف'}: {v['time']} — هدف {v['target_complete']} "
                   f"— ميزانية ${v['budget_usd']:.0f}")
    return back("/discover")


@app.post("/discover/probe")
def discover_probe(request: Request, user: User = Depends(require_manager)):
    from .. import directory as D
    _bg(D.probe_sources)
    flash(request, "بدأ فحص الوصول للمصادر من الخادم — حدّث الصفحة بعد نصف دقيقة.")
    return back("/discover")


def _scope_regions(form) -> dict:
    from .. import geo
    regs = [r for r in form.getlist("regions") if r in geo.REGIONS]
    if form.get("all_regions") or not regs:
        return {"all": True} if form.get("all_regions") else {"places": ["SA-01/riyadh", "SA-02/jeddah", "SA-04/dammam"]}
    return {"regions": regs}


@app.post("/discover/run")
async def discover_run(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from .. import directory as D
    from ..auth import audit
    form = await request.form()
    halt_reason = getattr(__import__("mandoob.prospecting", fromlist=["x"]), "halt_reason", lambda: None)
    if halt_reason():
        flash(request, "البحث موقوف أو بلغ سقف الإنفاق اليومي: " + halt_reason(), "err")
        return back("/discover")
    sources = [x for x in form.getlist("sources") if x in D.SOURCES] or list(D.AUTO_SOURCES)
    budget = max(0.5, min(float(form.get("budget") or 5), 500))
    params = {"activities": list(form.getlist("activities")) or DISCOVER_DEFAULT, "scope": _scope_regions(form),
              "stage1_limit": max(5, min(int(form.get("limit") or 30), 2000))}
    r = D.request(s, sources=sources, params=params, budget=budget, user_id=user.id)
    audit(s, user, "directory_run", None, run=r.id, sources=sources, budget=budget, limit=params["stage1_limit"])
    flash(request, f"بدأت الدفعة #{r.id} على الخادم (يلتقطها العامل خلال دقيقة) — الميزانية ${budget:.2f}.")
    return back(f"/discover?run={r.id}")


@app.post("/discover/import")
async def discover_import(request: Request, file: UploadFile = File(...), label: str = Form("ملف"),
                          user: User = Depends(require_manager), s=Depends(get_db)):
    """المرحلة ١ من ملف رسمي: تُحفظ المنشآت وتُطابق مع القاعدة فورًا (بلا تكلفة)، ثم تُستكمل في دفعة «ملف بيانات»."""
    from .. import directory as D
    from ..auth import audit
    from ..models import LeadStub
    raw = (await file.read())[:10_000_000].decode("utf-8-sig", errors="replace")
    rows = D.parse_file(raw)
    counts = {"new": 0, "duplicate": 0, "existing_customer": 0}
    key = "file:" + re.sub(r"[^\w\u0600-\u06FF-]+", "-", label.strip())[:30]
    for it in rows:
        st = LeadStub(source=key, name=it["name"][:300], name_norm=normalize_name(it["name"]), city=it["city"][:120],
                      activity=it["activity"][:200], website=it["website"] or None, phone_hint=it["phone"] or None,
                      source_url=it["listing_url"][:500], vehicle_hint=it["vehicle_hint"] or None)
        s.add(st)
        s.flush()
        st.status, st.duplicate_of = D.dedupe(s, st)
        counts[st.status] = counts.get(st.status, 0) + 1
    audit(s, user, "directory_import", None, label=label, rows=len(rows), **counts)
    flash(request, f"استُورد {len(rows)} سجلًا: جديد {counts['new']}، مكرر {counts['duplicate']}، "
                   f"من عملائنا {counts['existing_customer']}. شغّل دفعة بمصدر «ملف بيانات» لاستكمال الجديد.")
    return back("/discover")


@app.post("/companies/{cid}/similar")
async def company_similar(cid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    """«ابحث عن شركات مشابهة»: النشاط وطريقة استخدام المركبات، في المناطق المختارة — دفعة بميزانية محددة."""
    from .. import directory as D
    from ..auth import audit
    c = s.get(Company, cid) or _404()
    form = await request.form()
    how = (c.fit_phrase or c.fit_reason or "")[:120]
    queries = [c.activity or c.name]
    if how:
        queries.append(f"{c.activity} — {how}")
    budget = max(0.5, min(float(form.get("budget") or 3), 100))
    params = {"queries": queries, "sector": c.sector_key or "vehicle_users", "sector_label": f"مشابهة لـ {c.name}"[:60],
              "scope": _scope_regions(form), "stage1_limit": max(5, min(int(form.get("limit") or 15), 300)),
              "similar_to": cid}
    r = D.request(s, sources=["claude_directory"], params=params, budget=budget, user_id=user.id)
    audit(s, user, "directory_similar", None, company=cid, run=r.id)
    flash(request, f"بدأ البحث عن شركات مشابهة لـ {c.name} (الدفعة #{r.id}، الميزانية ${budget:.2f}).")
    return back(f"/discover?run={r.id}")


@app.post("/companies/{cid}/reasons/find")
def company_reasons_find(cid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from .. import directory as D
    halt_reason = getattr(__import__("mandoob.prospecting", fromlist=["x"]), "halt_reason", lambda: None)
    s.get(Company, cid) or _404()
    if halt_reason():
        flash(request, halt_reason(), "err")
        return back(f"/companies/{cid}")
    _bg(D.find_reasons, cid)
    flash(request, "يبحث الآن عن دليل محدد (توظيف، افتتاح، عقد…) — حدّث الصفحة بعد دقيقة.")
    return back(f"/companies/{cid}#reasons")


@app.post("/companies/{cid}/reasons/add")
def company_reasons_add(cid: int, request: Request, kind: str = Form("other"), title: str = Form(...),
                        when: str = Form(""), url: str = Form(""), user: User = Depends(current_user),
                        s=Depends(get_db)):
    from .. import directory as D
    from ..models import CompanySignal
    c = s.get(Company, cid) or _404()
    if not _company_visible(user, c):
        raise HTTPException(403)
    if url and not url.startswith(("http://", "https://")):
        flash(request, "الرابط يجب أن يبدأ بـ https://", "err")
        return back(f"/companies/{cid}#reasons")
    try:
        d = date.fromisoformat(when) if when else None
    except ValueError:
        d = None
    s.add(CompanySignal(company_id=cid, kind=kind if kind in D.REASON_KINDS else "other",
                        label=D.REASON_KINDS.get(kind, D.REASON_KINDS["other"]), title=f"{title[:300]} — أضافه {user.name}",
                        announced_on=d, url=url[:500]))
    flash(request, "أُضيف سبب الترشيح.")
    return back(f"/companies/{cid}#reasons")


@app.get("/companies.xlsx")
def companies_xlsx(request: Request, status: str = "complete", q: str = "", city: str = "", sector: str = "",
                   source: str = "", operator: str = "", signals: str = "", kind: str = "", owner: str = "",
                   contact: str = "", found: str = "", user: User = Depends(current_user), s=Depends(get_db)):
    from ..auth import audit
    from ..xlsx import CONTACT_HEADERS, response, workbook
    qq = _companies_query(user, status, q, city, sector, source, operator, signals, kind, owner, contact, found)
    rows = [_customer_row(s, c) for c in s.scalars(qq.order_by(Company.first_found_at.desc()))]
    audit(s, user, "export_download", user, kind="companies", rows=len(rows))
    return response(workbook([("الشركات", CUSTOMER_HEADERS + CONTACT_HEADERS, rows)],
                             {"الشركات": [["الشركات المسندة وغير المسندة حسب الفلاتر", f"{len(rows)} سجل"]]}),
                    "companies.xlsx")


@app.get("/followups.xlsx")
def followups_xlsx(request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    from ..xlsx import CONTACT_HEADERS, response, workbook
    items = needs_followup_today(s, user)
    rows = [[it["task"].title, fmt(it["task"].due_at), "متأخرة" if it["overdue"] else "اليوم", it["owner"]]
            + _customer_row(s, it["company"]) for it in items]
    return response(workbook([("تحتاج متابعة", ["المهمة", "موعدها", "الحالة", "مسؤول المهمة"] + CUSTOMER_HEADERS
                               + CONTACT_HEADERS, rows)]), "followups.xlsx")


@app.get("/daily-report.xlsx")
def daily_report_xlsx(request: Request, day: str = "", user: User = Depends(require_manager), s=Depends(get_db)):
    from ..xlsx import response, workbook
    d = date.fromisoformat(day) if day else riyadh_today()
    r = daily_report(s, d)
    summary_rows = [["اليوم", str(r["day"])], ["شركات مكتملة جديدة", r["complete"]],
                    ["التكلفة التقديرية ($)", r["total_cost"]], ["تكلفة العميل المكتمل ($)", r["cost_per_lead"] or ""]]
    sectors = [[k, v["contacted"], v["replied"], v["meetings"], v["won"]] for k, v in r["by_sector"].items()]
    offers = [[k, v["sent"], v["replies"], v["meetings"], v["won"]] for k, v in r["by_offer"].items()]
    actions = [[t.company.name if t.company else "", t.title, fmt(t.due_at), t.owner.name if t.owner else ""]
               for t in r["actions"]]
    sources = [[k, v["candidates"], v["complete_count"], v["incomplete_count"], v["duplicate_count"], v["rejected_count"]]
               for k, v in r["by_source"].items()]
    return response(workbook([("الملخص", ["البند", "القيمة"], summary_rows),
                              ("القطاعات", ["القطاع", "تم التواصل", "ردود", "اجتماعات", "صفقات"], sectors),
                              ("العروض", ["العرض", "أُرسل", "ردود", "اجتماعات", "صفقات"], offers),
                              ("المصادر", ["المصدر", "مرشحون", "مكتملة", "قيد الاستكمال", "مكررة", "مرفوضة"], sources),
                              ("اكتشاف حسب المصدر", ["المصدر", "المكتشفة", "الجديدة بعد استبعاد التكرار", "المكررة",
                                                     "المكتملة (هاتف + بريد)", "قيد الاستكمال", "التكلفة ($)",
                                                     "تكلفة المنشأة المكتملة ($)", "وصل الفريق إلى المسؤول"],
                               [[x["source"], x["discovered"], x["new"], x["duplicates"], x["complete"], x["incomplete"],
                                 x["cost"], x["cost_per_complete"] or "", x["reached_dm"]]
                                for x in __import__("mandoob.directory", fromlist=["x"]).daily_by_source(s, d)]),
                              ("مهام الغد والمتأخرة", ["المنشأة", "المهمة", "الموعد", "المسؤول"], actions)]),
                    f"daily-report-{d.isoformat()}.xlsx")


@app.get("/weekly-report.xlsx")
def weekly_report_xlsx(request: Request, day: str = "", user: User = Depends(require_manager), s=Depends(get_db)):
    from ..reports import weekly_segments
    from ..xlsx import response, workbook
    d = date.fromisoformat(day) if day else riyadh_today()
    w = weekly_segments(s, d)
    rows = [[r["label"], "اختبار" if r["experiment"] else "", r["contacted"], r["interested"], r["interested_per_100"],
             r["meetings"], r["meetings_per_100"], r["won"]] for _, r in w["rows"]]
    rows.append(["الإجمالي", "", w["totals"]["contacted"], w["totals"]["interested"], "", w["totals"]["meetings"],
                 w["totals"]["meetings_per_100"], w["totals"]["won"]])
    return response(workbook([("الشرائح", ["الشريحة", "", "تم التواصل", "مهتمة", "مهتمة/١٠٠", "اجتماعات", "اجتماعات/١٠٠",
                                            "صفقات"], rows)], {"الشرائح": [[f"أسبوع {w['start']} — {w['end']}"]]}),
                    f"weekly-report-{w['start']}.xlsx")


@app.get("/leads.xlsx")
def leads_xlsx(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from ..leads import source_label
    from ..models import InboundLead
    from ..xlsx import response, workbook
    rows = []
    for r in s.scalars(select(InboundLead).order_by(InboundLead.id.desc())):
        rows.append([fmt(r.created_at), r.company_name, r.contact_name, r.phone or "", r.email or "", r.city,
                     r.vehicles or "", r.service_text, source_label(r.source or ""), r.notes])
    return response(workbook([("طلبات العروض", ["التاريخ", "المنشأة", "الاسم", "الهاتف", "البريد", "المدينة",
                                                "عدد المركبات", "الخدمة", "المصدر", "ملاحظات"], rows)]), "leads.xlsx")


@app.post("/companies/assign")
async def companies_bulk_assign(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    """إسناد مجموعة منشآت لموظف مبيعات. المنشأة المسندة لموظف آخر لا تُنقل إلا بتأكيد صريح،
    حتى لا يتواصل موظفان مع المنشأة نفسها."""
    from ..auth import audit
    form = await request.form()
    rep_id = int(form.get("owner_id") or 0)
    u = s.get(User, rep_id) if rep_id else None
    if not u or not u.active:
        flash(request, "اختر موظفًا نشطًا", "err")
        return back(request.headers.get("referer") or "/companies")
    reassign = bool(form.get("reassign"))
    ids = [int(x) for x in form.getlist("company_ids") if str(x).isdigit()]
    done, skipped, moved = 0, [], 0
    for c in s.scalars(select(Company).where(Company.id.in_(ids))):
        if c.owner_id == u.id:
            continue
        if c.owner_id and not reassign:
            skipped.append(f"{c.name} (لدى {c.owner.name})")
            continue
        prev = c.owner.name if c.owner else None
        moved += 1 if prev else 0
        c.owner_id, c.assigned_at = u.id, now_utc()
        for t in s.scalars(select(Task).where(Task.company_id == c.id, Task.status == "open")):
            t.owner_id = u.id
        log_activity(s, c.id, "assigned", f"أُسندت المنشأة إلى {u.name}",
                     f"بواسطة {user.name}" + (f" (نُقلت من {prev})" if prev else ""), user_id=user.id)
        done += 1
    audit(s, user, "bulk_assign", u, rows=done, moved=moved, skipped=len(skipped))
    msg = f"أُسندت {done} منشأة إلى {u.name}."
    if skipped:
        msg += f" تُركت {len(skipped)} لأنها مسندة لموظف آخر: " + "، ".join(skipped[:5]) + \
               ("…" if len(skipped) > 5 else "") + " — فعّل «نقل المسند لموظف آخر» إن أردت نقلها."
    flash(request, msg, "ok" if done else "err")
    return back(request.headers.get("referer") or "/companies")


@app.get("/companies/{cid}", response_class=HTMLResponse)
def company_detail(cid: int, request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    c = s.get(Company, cid) or _404()
    if not _company_visible(user, c):
        raise HTTPException(403)
    timeline = company_timeline(s, cid)
    msgs = list(s.scalars(select(OutboundMessage).where(OutboundMessage.company_id == cid)
                          .order_by(OutboundMessage.id.desc())))
    replies = list(s.scalars(select(InboundMessage).where(InboundMessage.company_id == cid,
                                                          InboundMessage.message_type == "reply")
                             .order_by(InboundMessage.received_at.desc())))
    tasks = list(s.scalars(select(Task).where(Task.company_id == cid).order_by(Task.due_at)))
    calls = list(s.scalars(select(Call).where(Call.company_id == cid).order_by(Call.id.desc())))
    meetings = list(s.scalars(select(Meeting).where(Meeting.company_id == cid).order_by(Meeting.starts_at)))
    consents = list(s.scalars(select(Consent).where(Consent.company_id == cid).order_by(Consent.id.desc())))
    reps = list(s.scalars(select(User).where(User.active.is_(True)).order_by(User.name)))
    from ..models import CompanyRelation, CompanySignal
    from ..leads import source_label
    relations = list(s.scalars(select(CompanyRelation).where(
        (CompanyRelation.company_id == cid) | (CompanyRelation.related_id == cid))))
    signals = list(s.scalars(select(CompanySignal).where(CompanySignal.company_id == cid)
                             .order_by(CompanySignal.announced_on.desc())))
    from ..voice import check_call_eligibility
    call_ok, call_why = check_call_eligibility(s, c)
    return render(request, "company.html", user, s, c=c, timeline=timeline, msgs=msgs, replies=replies, tasks=tasks,
                  calls=calls, meetings=meetings, consents=consents, reps=reps, call_ok=call_ok, call_why=call_why,
                  relations=relations, signals=signals, source_label=source_label, **_contact_ctx(s, c),
                  REASON_KINDS=__import__("mandoob.directory", fromlist=["REASON_KINDS"]).REASON_KINDS,
                  GEO_REGIONS=__import__("mandoob.geo", fromlist=["REGIONS"]).REGIONS)


@app.post("/companies/{cid}/action")
def company_action(cid: int, request: Request, action: str = Form(...), note: str = Form(""),
                   owner_id: Optional[int] = Form(None), when: str = Form(""), value: Optional[float] = Form(None),
                   user: User = Depends(current_user), s=Depends(get_db)):
    c = s.get(Company, cid) or _404()
    if not _company_visible(user, c):
        raise HTTPException(403)
    from ..inbox import apply_opt_out
    if action == "assign" and user.role in ("admin", "manager") and owner_id:
        from ..assignment import assign_owner
        c.owner_id = None
        assign_owner(s, c, force_user_id=owner_id)
        for t in s.scalars(select(Task).where(Task.company_id == cid, Task.status == "open")):
            t.owner_id = owner_id
        flash(request, "تم تغيير المندوب المسؤول")
    elif action == "note" and note:
        c.last_action, c.last_action_at = note[:300], now_utc()
        log_activity(s, cid, "note", f"ملاحظة من {user.name}", note, user_id=user.id)
        flash(request, "أُضيفت الملاحظة")
    elif action == "task" and when:
        due = riyadh_to_utc(datetime.fromisoformat(when))
        s.add(Task(company_id=cid, owner_id=c.owner_id or user.id, kind="call", title=note or f"متابعة {c.name}",
                   due_at=due))
        c.next_action, c.next_action_at = note or "متابعة", due
        log_activity(s, cid, "task", f"أُنشئت مهمة: {note or 'متابعة'}", f"الموعد: {fmt(due)}", user_id=user.id)
        flash(request, "أُنشئت المهمة")
    elif action == "meeting" and when:
        from ..meetings import SlotUnavailable, book_meeting
        owner = c.owner or user
        try:
            book_meeting(s, c, owner, riyadh_to_utc(datetime.fromisoformat(when)), booked_by=user)
            flash(request, "حُجز الاجتماع في تقويم المندوب وسيُرسل التأكيد للعميل")
            _bg(jobs.outreach_tick)
        except SlotUnavailable as e:
            flash(request, "الموعد غير متاح في تقويم المندوب. مواعيد مقترحة: " +
                  "، ".join(fmt(x) for x in e.suggestions), "err")
        except RuntimeError as e:
            flash(request, str(e), "err")
    elif action == "unlink_branch" and user.role in ("admin", "manager"):
        from ..models import CompanyRelation
        for r in s.scalars(select(CompanyRelation).where(CompanyRelation.company_id == cid,
                                                         CompanyRelation.kind == "branch")):
            s.delete(r)
        c.parent_id = None
        log_activity(s, cid, "branch", "فُك ربط الشركة بالمقر الرئيسي", note, user_id=user.id)
        flash(request, "أصبحت الشركة سجلًا مستقلًا")
    elif action == "set_parent" and user.role in ("admin", "manager") and owner_id:
        from ..models import CompanyRelation
        hq = s.get(Company, owner_id)
        if hq and hq.id != cid:
            c.parent_id = hq.parent_id or hq.id
            s.add(CompanyRelation(company_id=cid, related_id=c.parent_id, kind="branch", evidence=f"ربط يدوي بواسطة {user.name}"))
            log_activity(s, cid, "branch", f"رُبطت كفرع لـ {hq.name}", user_id=user.id)
            flash(request, "رُبطت الشركة بالمقر الرئيسي")
    elif action == "won":
        c.stage, c.deal_value = "won", value
        log_activity(s, cid, "won", "صفقة رابحة", f"القيمة: {value or '—'}", user_id=user.id)
        flash(request, "سُجلت الصفقة الرابحة")
    elif action == "lost":
        c.stage = "lost"
        log_activity(s, cid, "lost", "أُغلقت الفرصة", note, user_id=user.id)
    elif action == "opt_out":
        apply_opt_out(s, c, source="manual", detail=note or f"سجّله {user.name}")
        flash(request, "سُجل طلب إيقاف التواصل واستُبعدت الشركة من الحملات اللاحقة")
    elif action in ("call_consent", "ai_call") and not settings.voice_calls_enabled:
        flash(request, "الاتصال الآلي معطّل في هذه المرحلة", "err")
    elif action == "call_consent":
        s.add(Consent(company_id=cid, channel="call", basis="explicit", granted=True, recorded_by=user.id,
                      evidence=note or f"سجّلها {user.name}"))
        log_activity(s, cid, "consent", "سُجلت موافقة على الاتصال الآلي", note, user_id=user.id)
        flash(request, "سُجلت الموافقة")
    elif action == "ai_call":
        from ..voice import start_call
        offer = c.category.offer if c.category and c.category.offer else None
        try:
            start_call(s, c, service_name=offer.service_name if offer else "خدمات سيارة آب",
                       offer_points=offer.value_points if offer else "", rep_name=c.owner.name if c.owner else "")
            flash(request, "بدأت المكالمة. ستظهر النتيجة في السجل عند انتهائها.")
        except (PermissionError, RuntimeError) as e:
            flash(request, str(e), "err")
    return back(f"/companies/{cid}")


@app.get("/messages/{mid}", response_class=HTMLResponse)
def message_view(mid: int, request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    m = s.get(OutboundMessage, mid) or _404()
    if not _company_visible(user, m.company):
        raise HTTPException(403)
    return render(request, "message.html", user, s, m=m)


@app.get("/messages/{mid}/attachment")
def message_attachment(mid: int, user: User = Depends(current_user), s=Depends(get_db)):
    m = s.get(OutboundMessage, mid) or _404()
    if not _company_visible(user, m.company) or not m.attachment_id:
        raise HTTPException(404)
    att = s.get(Attachment, m.attachment_id)
    return Response(att.read_bytes(), media_type=att.mime_type,
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{_q(m.attachment_name)}"})


def _q(name):
    from urllib.parse import quote
    return quote(name or "file.pdf")


# ---------------------------------------------------------------- تقرير العروض

def _filters(request: Request) -> OfferFilters:
    p = request.query_params
    d = lambda k: date.fromisoformat(p[k]) if p.get(k) else None  # noqa: E731
    i = lambda k: int(p[k]) if p.get(k) else None  # noqa: E731
    return OfferFilters(date_from=d("from"), date_to=d("to"), city=p.get("city") or None,
                        sector=p.get("sector") or None, campaign_id=i("campaign"), owner_id=i("owner"),
                        offer_id=i("offer"), status=p.get("status") or None, reply=p.get("reply") or None,
                        q=p.get("q") or None, source=p.get("source") or None)


@app.get("/offers-report", response_class=HTMLResponse)
def offers_report_page(request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    f = _filters(request)
    rep = offers_report(s, user, f)
    return render(request, "offers_report.html", user, s, rep=rep, f=f, qs=str(request.query_params),
                  camps=list(s.scalars(select(Campaign))), offers=list(s.scalars(select(Offer))),
                  reps=list(s.scalars(select(User).where(User.role == "rep"))))


@app.get("/offers-report.xlsx")
def offers_report_xlsx(request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    rep = offers_report(s, user, _filters(request))
    data = export_offers_xlsx(rep)
    fname = f"offers-report-{riyadh_today().isoformat()}.xlsx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.get("/followups", response_class=HTMLResponse)
def followups(request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    return render(request, "followups.html", user, s, items=needs_followup_today(s, user))


@app.post("/tasks/{tid}/done")
def task_done(tid: int, request: Request, note: str = Form(""), user: User = Depends(current_user), s=Depends(get_db)):
    t = s.get(Task, tid) or _404()
    if user.role == "rep" and t.owner_id != user.id:
        raise HTTPException(403)
    t.status, t.done_at = "done", now_utc()
    c = t.company
    c.last_action, c.last_action_at = f"{t.title}" + (f" — {note}" if note else ""), now_utc()
    nxt = s.scalar(select(Task).where(Task.company_id == c.id, Task.status == "open").order_by(Task.due_at))
    c.next_action, c.next_action_at = (nxt.title, nxt.due_at) if nxt else (None, None)
    log_activity(s, c.id, "task_done", f"أُنجزت المهمة: {t.title}", note, user_id=user.id)
    flash(request, "أُنجزت المهمة")
    return back(request.headers.get("referer") or "/followups")


# ---------------------------------------------------------------- العروض والمرفقات

@app.get("/offers", response_class=HTMLResponse)
def offers_page(request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    return render(request, "offers.html", user, s, offers=list(s.scalars(select(Offer).order_by(Offer.id))),
                  attachments=list(s.scalars(select(Attachment).order_by(Attachment.id.desc()))))


@app.post("/offers")
async def offers_save(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    form = await request.form()
    oid = form.get("id")
    o = s.get(Offer, int(oid)) if oid else None
    if o is None:
        key = (form.get("key") or "").strip() or f"offer-{int(now_utc().timestamp())}"
        o = Offer(key=key, service_name="", subject_template="")
        s.add(o)
    o.service_name = form.get("service_name", "")
    o.subject_template = form.get("subject_template") or "{service} لشركة {company}"
    o.value_points = form.get("value_points", "")
    o.approved_body = form.get("approved_body", "")
    o.pricing_text = form.get("pricing_text", "")
    o.call_to_action = form.get("call_to_action") or o.call_to_action
    o.coverage = _split(form.get("coverage", ""))
    o.segments = form.getlist("segments")
    o.audience = form.get("audience") or "customer"
    att = form.get("attachment_id")
    o.attachment_id = int(att) if att else None
    upload = form.get("attachment")
    if upload is not None and getattr(upload, "filename", ""):
        o.attachment_id = _store_attachment(s, upload.filename, await upload.read(), form.get("attachment_title")).id
    o.approved = False  # أي تعديل يتطلب اعتمادًا جديدًا
    o.approved_at = None
    s.flush()
    flash(request, "حُفظ العرض. اعتمده ليصبح قابلًا للإرسال.")
    return back("/offers")


def _store_attachment(s, filename: str, data: bytes, title: str | None = None) -> Attachment:
    sha = hashlib.sha256(data).hexdigest()
    if len(data) > 3 * 1024 * 1024:
        raise HTTPException(400, "حجم المرفق يتجاوز 3 ميجابايت (حد الإرسال المباشر في Microsoft Graph)")
    a = Attachment(title=title or filename, filename=filename, data=data, size_bytes=len(data), sha256=sha,
                   mime_type="application/pdf" if filename.lower().endswith(".pdf") else "application/octet-stream")
    s.add(a)
    s.flush()
    return a


@app.post("/offers/{oid}/approve")
def offer_approve(oid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    o = s.get(Offer, oid) or _404()
    o.approved, o.approved_by, o.approved_at = True, user.id, now_utc()
    flash(request, f"اعتُمد العرض «{o.service_name}»")
    return back("/offers")


@app.get("/offers/{oid}/preview", response_class=HTMLResponse)
def offer_preview(oid: int, request: Request, company_id: Optional[int] = None, user: User = Depends(current_user),
                  s=Depends(get_db)):
    from ..outreach import active_account, compose, render_html
    o = s.get(Offer, oid) or _404()
    c = s.get(Company, company_id) if company_id else s.scalar(
        select(Company).where(Company.data_status == "complete").limit(1))
    if c is None:
        c = Company(name="شركة المثال للنقل", activity="نقل وخدمات لوجستية", city="الرياض", name_norm="", fit_reason="")
    acct = active_account(s)
    subject, body = compose(c, o, acct.sender_name if acct else "سيارة آب")
    html = render_html(body, acct.signature_html if acct else "")
    return render(request, "offer_preview.html", user, s, o=o, c=c, subject=subject, html=html)


# ---------------------------------------------------------------- ربط بريد الشركة

@app.get("/mail", response_class=HTMLResponse)
def mail_page(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    accounts = list(s.scalars(select(MailAccount).order_by(MailAccount.id)))
    status = {"microsoft365": bool(settings.ms_tenant_id and settings.ms_client_id and settings.ms_client_secret),
              "google": bool(settings.google_client_id and settings.google_refresh_token)}
    return render(request, "mail.html", user, s, accounts=accounts, status=status)


@app.post("/mail")
def mail_save(request: Request, provider: str = Form(...), sender_address: str = Form(...),
              sender_name: str = Form("سيارة آب"), signature_html: str = Form(""), reply_mailbox: str = Form(""),
              user: User = Depends(require_manager), s=Depends(get_db)):
    for a in s.scalars(select(MailAccount)):
        a.active = False
    s.add(MailAccount(provider=provider, sender_address=sender_address.strip().lower(), sender_name=sender_name,
                      signature_html=signature_html, reply_mailbox=reply_mailbox.strip().lower(), active=True))
    flash(request, "رُبط بريد الشركة. أرسل رسالة تجربة للتأكد.")
    return back("/mail")


@app.post("/mail/test")
def mail_test(request: Request, to: str = Form(""), message: str = Form(""), user: User = Depends(require_manager),
              s=Depends(get_db)):
    from ..auth import provider_configured
    from ..mail import get_provider
    from ..mail.base import OutgoingEmail
    from ..outreach import active_account, render_html, render_text
    acct = active_account(s) or _404()
    to = (normalize_email(to) or user.email or "").strip()
    if not normalize_email(to):
        flash(request, "اكتب بريدًا صحيحًا في خانة «إلى»", "err")
        return back("/mail")
    if not provider_configured(acct):
        need = {"microsoft365": "MS_TENANT_ID و MS_CLIENT_ID و MS_CLIENT_SECRET",
                "smtp": "SMTP_HOST و SMTP_USER و SMTP_PASSWORD",
                "google": "GOOGLE_CLIENT_ID و GOOGLE_CLIENT_SECRET و GOOGLE_REFRESH_TOKEN"}.get(acct.provider, "")
        flash(request, f"مفاتيح المزود غير مضبوطة على الخادم بعد ({need}). أضفها في مجموعة mandoob-secrets "
                       "ثم أعد المحاولة.", "err")
        return back("/mail")
    body = message.strip() or "هذه رسالة تجربة من منصة «المندوب الذكي لسيارة اب» للتأكد من ربط البريد والتوقيع."
    try:
        res = get_provider(acct).send(OutgoingEmail(acct.sender_address, acct.sender_name, to,
                                                    "رسالة تجربة — المندوب الذكي لسيارة اب",
                                                    render_text(body, acct.signature_html),
                                                    render_html(body, acct.signature_html),
                                                    reply_to=acct.reply_mailbox or None))
        if res.accepted:
            first = acct.verified_at is None
            acct.verified_at = acct.verified_at or now_utc()
            flash(request, f"نجح الإرسال إلى {to}. " + ("البريد جاهز الآن: صار الدخول للجميع بكود البريد، "
                                                      "وأُوقف الدخول بكلمة المرور." if first else ""))
        else:
            flash(request, "فشل الإرسال: " + res.detail, "err")
    except Exception as e:
        flash(request, f"فشل الإرسال: {e}", "err")
    return back("/mail")


@app.post("/mail/sync")
def mail_sync(request: Request, user: User = Depends(current_user)):
    st = jobs.inbox_tick()
    flash(request, f"تمت المزامنة: {st.get('replies', 0)} رد، {st.get('delivered', 0)} تأكيد تسليم، "
                   f"{st.get('bounced', 0)} ارتداد")
    return back(request.headers.get("referer") or "/")


@app.post("/outreach/run")
def outreach_run(request: Request, ignore_window: str = Form(""), user: User = Depends(require_manager)):
    if not settings.auto_outreach_enabled:
        raise HTTPException(403, "الإرسال التلقائي معطّل في هذه المرحلة")
    from ..outreach import queue_offers, send_due
    with session_scope() as s:
        ids = [c.id for c in s.scalars(select(Campaign).where(Campaign.status == "active",
                                                              Campaign.send_enabled.is_(True)))]
    q = sum(queue_offers(cid)["queued"] for cid in ids)
    st = send_due(ignore_window=bool(ignore_window))
    flash(request, f"جُهزت {q} رسالة، أُرسلت {st['sent']}، فشلت {st['failed']}، مؤجلة (خارج النافذة/الحد) {st['held']}")
    return back(request.headers.get("referer") or "/")


# ---------------------------------------------------------------- صندوق التجربة

@app.get("/sandbox", response_class=HTMLResponse)
def sandbox_page(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    msgs = list(s.scalars(select(OutboundMessage).join(MailAccount, MailAccount.id == OutboundMessage.mail_account_id)
                          .where(MailAccount.provider == "sandbox", OutboundMessage.status.in_(["sent", "delivered", "bounced"]))
                          .order_by(OutboundMessage.id.desc()).limit(50)))
    return render(request, "sandbox.html", user, s, msgs=msgs)


@app.post("/sandbox/reply")
def sandbox_reply(request: Request, message_id: int = Form(...), text: str = Form(...),
                  user: User = Depends(require_manager), s=Depends(get_db)):
    from ..mail.sandbox import SandboxProvider
    m = s.get(OutboundMessage, message_id) or _404()
    SandboxProvider.inject_reply(from_addr=m.to_address, original_message_id=m.internet_message_id,
                                 thread_id=m.thread_id, subject=m.subject, text=text)
    jobs.inbox_tick()
    flash(request, "وصل الرد وعولج. افتح سجل الشركة لرؤية النتيجة.")
    return back(f"/companies/{m.company_id}")


# ---------------------------------------------------------------- الفريق

@app.get("/team", response_class=HTMLResponse)
def team(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    users = list(s.scalars(select(User).order_by(User.role, User.name)))
    perf = {}
    for u in users:
        perf[u.id] = {
            "open": s.scalar(select(func.count(Company.id)).where(Company.owner_id == u.id,
                                                                  Company.stage.in_(["contacted", "offer_sent", "needs_followup", "replied", "interested", "meeting"]))) or 0,
            "tasks": s.scalar(select(func.count(Task.id)).where(Task.owner_id == u.id, Task.status == "open")) or 0,
            "overdue": s.scalar(select(func.count(Task.id)).where(Task.owner_id == u.id, Task.status == "open",
                                                                  Task.due_at < now_utc())) or 0,
            "meetings": s.scalar(select(func.count(Meeting.id)).where(Meeting.owner_id == u.id)) or 0,
            "won": s.scalar(select(func.count(Company.id)).where(Company.owner_id == u.id, Company.stage == "won")) or 0,
        }
    return render(request, "team.html", user, s, users=users, perf=perf)


@app.post("/team")
async def team_save(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    form = await request.form()
    uid = form.get("id")
    u = s.get(User, int(uid)) if uid else None
    role = form.get("role", "rep")
    if role == "admin" and user.role != "admin":
        raise HTTPException(403)
    if u is None:
        email = (form.get("email") or "").strip().lower()
        if s.scalar(select(User.id).where(User.email == email)):
            flash(request, "البريد مستخدم مسبقًا", "err")
            return back("/team")
        from ..permissions import PROFILES
        u = User(email=email, name="", password_hash=hash_password(form.get("password") or os.urandom(16).hex()),
                 profile="sales_rep", scope="own", perms=dict(PROFILES["sales_rep"]["perms"]), created_by=user.id)
        s.add(u)
    u.name = form.get("name") or u.name
    u.phone = form.get("phone") or None
    u.role = role
    u.sectors = form.getlist("sectors")
    u.cities = form.getlist("cities")
    u.max_open_leads = int(form.get("max_open_leads") or 60)
    u.calendar_email = (form.get("calendar_email") or "").strip().lower() or None
    u.active = form.get("active") == "on"
    if form.get("password") and uid:
        u.password_hash = hash_password(form.get("password"))
    flash(request, "حُفظ المستخدم")
    return back("/team")


# ---------------------------------------------------------------- الإعدادات والاستيراد

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    voice = jobs.get_setting("voice", {"enabled": False})
    integrations = {
        "Claude (الذكاء الاصطناعي)": settings.ai_enabled,
        "Google Places API": bool(settings.google_places_api_key),
        "Brave Search API": bool(settings.brave_search_api_key),
        "Microsoft 365": bool(settings.ms_client_id),
        "Google Workspace": bool(settings.google_client_id),
        "ElevenLabs (المكالمات)": bool(settings.elevenlabs_api_key),
    }
    from ..seed_test import verify as _verify_test
    return render(request, "settings.html", user, s, voice=voice, integrations=integrations, test_check=_verify_test(s),
                  customers=s.scalar(select(func.count(ExistingCustomer.id))),
                  suppressions=list(s.scalars(select(Suppression).order_by(Suppression.id.desc()).limit(50))))


@app.post("/settings/voice")
def settings_voice(request: Request, enabled: str = Form(""), user: User = Depends(require_manager)):
    if not settings.voice_calls_enabled:
        raise HTTPException(403, "الاتصال الآلي معطّل في هذه المرحلة")
    jobs.put_setting("voice", {"enabled": bool(enabled)})
    flash(request, "حُفظت إعدادات قناة الاتصال")
    return back("/settings")


@app.post("/settings/customers")
async def import_customers(request: Request, file: UploadFile = File(...), user: User = Depends(require_manager),
                           s=Depends(get_db)):
    import csv
    import io
    text = (await file.read()).decode("utf-8-sig")
    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("name") or row.get("الاسم") or "").strip()
        if not name:
            continue
        s.add(ExistingCustomer(name=name, name_norm=normalize_name(name),
                               domain=domain_of(row.get("website") or row.get("email") or "") or None,
                               phone=normalize_phone(row.get("phone")), email=normalize_email(row.get("email"))))
        n += 1
    flash(request, f"استُورد {n} عميل حالي للاستبعاد")
    return back("/settings")


@app.post("/settings/directory")
async def import_directory(request: Request, file: UploadFile = File(...), campaign_id: int = Form(...),
                           directory_name: str = Form("directory"), user: User = Depends(require_manager)):
    from ..discovery.runner import _handle_candidate
    text = (await file.read()).decode("utf-8-sig")
    cands = DirectoryImportSource.parse_csv(text, directory_name)
    with session_scope() as s:
        camp = s.get(Campaign, campaign_id) or _404()
        cats = [(c.id, c.sector_key, c.label) for c in camp.categories]
        run = s.scalar(select(DiscoveryRun).where(DiscoveryRun.campaign_id == campaign_id,
                                                  DiscoveryRun.run_date == riyadh_today()))
        if run is None:
            run = DiscoveryRun(run_date=riyadh_today(), campaign_id=campaign_id, target=camp.daily_target,
                               status="partial", checkpoint={}, per_category={}, per_source={}, source_errors={}, log=[])
            s.add(run)
            s.flush()
        run_id = run.id
    from ..discovery.crawler import Crawler
    crawler = Crawler()
    outcomes = {}
    for cand in cands:
        cat_id = next((cid for cid, key, label in cats if key in (cand.activity or "") or label in (cand.activity or "")),
                      cats[0][0])
        o = _safe(_handle_candidate, run_id, cat_id, cand, crawler) or "error"
        outcomes[o] = outcomes.get(o, 0) + 1
    flash(request, "نتيجة الاستيراد: " + "، ".join(f"{k}: {v}" for k, v in outcomes.items()))
    return back("/companies")


@app.post("/settings/seed-test")
def settings_seed_test(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from ..auth import audit
    from ..seed_test import seed, verify
    res = seed(s)
    v = verify(s)
    audit(s, user, "seed_test_data", None, added=res["added"], total=v["test_customers"])
    flash(request, f"أُضيف {res['added']} سجل تجريبي — الإجمالي {v['test_customers']} (مسند {v['assigned']}، بانتظار التوزيع {v['unassigned']}"
                   + ("، لا يوجد موظفون بعد — أضفهم ثم اضغط الزر مرة أخرى لإسناد جزء منها" if not res["staff"] else "")
                   + f"). رسائل مرسلة: {v['messages_sent']}، "
                   f"مجدولة: {v['messages_queued']}، مكالمات: {v['calls']}.")
    return back("/companies?kind=test")


@app.post("/demo-view")
def toggle_demo_view(request: Request, on: str = Form(""), user: User = Depends(require_manager), s=Depends(get_db)):
    from ..auth import audit
    from ..training_demo import _set_demo_view
    _set_demo_view(s, bool(on))
    audit(s, user, "demo_view", None, on=bool(on))
    flash(request, "وضع العرض التدريبي مفعّل: تظهر البيانات التجريبية في الإحصائيات والتقارير." if on else
          "أُطفئ وضع العرض التدريبي: الإحصائيات للبيانات الحقيقية فقط.")
    return back(request.headers.get("referer") or "/")


@app.post("/settings/training")
def settings_training(request: Request, action: str = Form("create"), user: User = Depends(require_manager),
                      s=Depends(get_db)):
    from ..auth import audit
    from ..training_demo import create, remove
    if action == "remove":
        n = remove(s)
        audit(s, user, "training_remove", None, removed=n)
        flash(request, f"حُذفت البيانات التدريبية ({n} شركة) وأُطفئ وضع العرض التدريبي.")
        return back("/settings")
    res = create(s)
    audit(s, user, "training_create", None, **{k: v for k, v in res.items() if isinstance(v, int)})
    flash(request, "البيانات التدريبية موجودة مسبقًا." if not res.get("offers") else
          f"أُنشئت بيانات تدريبية: {res['created']} شركة، {res['offers']} عرضًا (لم يُرسل أي منها)، {res['replies']} ردًا، "
          f"{res['tasks']} مهمة، {res['meetings']} اجتماعًا، {res['won']} صفقات، {res['calls']} مكالمات مسجلة. "
          "وضع العرض التدريبي مفعّل.")
    return back("/")


@app.post("/settings/suppress")
def add_suppression(request: Request, value: str = Form(...), user: User = Depends(require_manager), s=Depends(get_db)):
    v = value.strip().lower()
    kind = "email" if "@" in v else ("phone" if normalize_phone(v) else "domain")
    v = normalize_phone(v) if kind == "phone" else v
    s.add(Suppression(kind=kind, value=v, channel="all", reason="أضيف يدويًا", source="manual"))
    flash(request, "أضيف إلى قائمة إيقاف التواصل")
    return back("/settings")


# ---------------------------------------------------------------- سجل التشغيل والتقارير

@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    from ..directory import SOURCES as DSRC
    from ..health import classify, search_status
    from ..models import DirectoryRun
    rows = list(s.scalars(select(DiscoveryRun).order_by(DiscoveryRun.id.desc()).limit(30)))
    drows = list(s.scalars(select(DirectoryRun).order_by(DirectoryRun.id.desc()).limit(15)))
    return render(request, "runs.html", user, s, rows=rows, drows=drows, DSRC=DSRC, classify=classify,
                  health=search_status(s))


@app.get("/daily-report", response_class=HTMLResponse)
def daily_report_page(request: Request, day: str = "", user: User = Depends(require_manager), s=Depends(get_db)):
    d = date.fromisoformat(day) if day else riyadh_today()
    from ..directory import daily_by_source
    return render(request, "daily_report.html", user, s, r=daily_report(s, d), by_src=daily_by_source(s, d))


@app.get("/notifications", response_class=HTMLResponse)
def notifications(request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    rows = list(s.scalars(select(Notification).where(Notification.user_id == user.id)
                          .order_by(Notification.id.desc()).limit(100)))
    page = render(request, "notifications.html", user, s, rows=rows)
    for n in rows:
        n.read = True
    return page


# ---------------------------------------------------------------- البحث الآلي عن العملاء

AUTO_EXAMPLE = ("ابحث لي يوميًا في الرياض وجدة والدمام عن المنشآت وأصحاب الأنشطة الذين يستخدمون سيارات في عملهم. "
                "اجمع لي اسم كل عميل، ورقم التواصل، والبريد الإلكتروني، والمدينة، والدليل الذي وجدت منه أنه يستخدم مركبات.")


@app.get("/auto-search", response_class=HTMLResponse)
def auto_search_page(request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    camps = list(s.scalars(select(Campaign).where(Campaign.name.like("البحث الآلي%")).order_by(Campaign.id.desc())))
    runs = {c.id: s.scalar(select(DiscoveryRun).where(DiscoveryRun.campaign_id == c.id)
                           .order_by(DiscoveryRun.id.desc())) for c in camps}
    return render(request, "auto_search.html", user, s, camps=camps, runs=runs, example=AUTO_EXAMPLE,
                  ai_ready=settings.ai_enabled)


@app.post("/auto-search")
def auto_search_create(request: Request, text: str = Form(...), daily_target: int = Form(400),
                       user: User = Depends(require_manager), s=Depends(get_db)):
    from ..discovery.agent import plan_queries
    parsed = parse_request(text, daily_target)
    cities = list(dict.fromkeys(c for cat in parsed["categories"] for c in cat["cities"]))
    camp = Campaign(name="البحث الآلي — " + "، ".join(cities[:4]), request_text=text, status="active",
                    daily_target=parsed["daily_target"], exclusions=parsed["exclusions"],
                    exclude_existing_customers=parsed["exclude_existing_customers"], send_enabled=False,
                    created_by=user.id, approved_at=now_utc())
    s.add(camp)
    s.flush()
    for c in parsed["categories"]:
        queries = plan_queries(text if len(parsed["categories"]) == 1 else c["label"], c["cities"])
        if len(parsed["categories"]) > 1:
            queries = list(dict.fromkeys(c["keywords_ar"] + queries))
        s.add(Category(campaign_id=camp.id, sector_key=c["sector_key"], label=c["label"], cities=c["cities"],
                       keywords_ar=queries, keywords_en=[q for q in c["keywords_en"] if q not in queries],
                       priority=c["priority"], priority_signals=c["priority_signals"], quota=c["quota"],
                       is_experiment=c.get("is_experiment", False)))
    flash(request, "جُهّز البحث الآلي. اضغط «ابحث الآن» لأول دفعة، وسيعمل يوميًا في وقت الاكتشاف المحدد.")
    return back(f"/auto-search#camp-{camp.id}")


@app.post("/auto-search/{cid}/run")
def auto_search_run(cid: int, request: Request, batch: int = Form(20), user: User = Depends(require_manager),
                    s=Depends(get_db)):
    from ..discovery.runner import run_campaign_discovery
    camp = s.get(Campaign, cid) or _404()
    if camp.status != "active":
        flash(request, "الحملة موقوفة", "err")
        return back("/auto-search")
    _bg(run_campaign_discovery, cid, max_new=max(1, min(batch, 400)))
    flash(request, f"بدأ البحث الآلي على الخادم (دفعة حتى {batch} عميل مكتمل جديد). تظهر النتائج هنا تباعًا.")
    return back(f"/today?campaign={cid}")


def _period(mode: str, day: str, date_from: str, date_to: str):
    """(من، إلى، رسالة خطأ). يومي: يوم واحد (اليوم افتراضيًا). فترة: من/إلى شاملين يومي البداية والنهاية."""
    def d(x):
        try:
            return date.fromisoformat(x) if x else None
        except ValueError:
            return None
    today = riyadh_today()
    if mode == "range":
        a, b = d(date_from) or today, d(date_to) or today
        if b < a:
            return a, b, "تاريخ النهاية قبل تاريخ البداية — عدّل الفترة ثم اضغط «عرض»."
        if (b - a).days > 366:
            return a, b, "الفترة أطول من سنة — اختر فترة أقصر."
        return a, b, None
    x = d(day) or today
    return x, x, None


@app.get("/today", response_class=HTMLResponse)
def today_page(request: Request, mode: str = "day", day: str = "", date_from: str = Query("", alias="from"),
               date_to: str = Query("", alias="to"), campaign: Optional[int] = None,
               user: User = Depends(current_user), s=Depends(get_db)):
    """استعراض العملاء حسب تاريخ الجمع (يوم أو فترة) — قراءة فقط: لا بحث جديد ولا تكلفة إضافية."""
    from ..reports import customers_in_period
    mode = "range" if mode == "range" else "day"
    a, b, err = _period(mode, day, date_from, date_to)
    t = customers_in_period(s, user, a, b, campaign) if not err else None
    return render(request, "today.html", user, s, t=t, err=err, mode=mode, a=a, b=b, campaign=campaign,
                  csum=_csum(s, [r["c"] for r in t["rows"]]) if t else {},
                  qs=str(request.query_params), camps=list(s.scalars(select(Campaign).order_by(Campaign.id.desc()))))


@app.get("/today.xlsx")
def today_xlsx(request: Request, mode: str = "day", day: str = "", date_from: str = Query("", alias="from"),
               date_to: str = Query("", alias="to"), campaign: Optional[int] = None,
               user: User = Depends(current_user), s=Depends(get_db)):
    from ..reports import customers_in_period, export_today_xlsx
    a, b, err = _period("range" if mode == "range" else "day", day, date_from, date_to)
    if err:
        raise HTTPException(400, err)
    data = export_today_xlsx(customers_in_period(s, user, a, b, campaign), s)
    name = a.isoformat() if a == b else f"{a.isoformat()}_to_{b.isoformat()}"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=customers-{name}.xlsx"})


# ---------------------------------------------------------------- اطلب عرضًا لأسطولك (صفحة عامة)

@app.get("/request-quote", response_class=HTMLResponse)
def request_quote_page(request: Request, s=Depends(get_db)):
    p = request.query_params
    offers = list(s.scalars(select(Offer).where(Offer.approved.is_(True), Offer.audience != "partner")))
    return templates.TemplateResponse(request, "request_quote.html", {
        "user": None, "flash": request.session.pop("flash", None), "path": request.url.path, "offers": offers,
        "src": p.get("src", ""), "ref": p.get("ref", ""), "utm": p.get("utm_campaign", ""), "done": p.get("done")})


@app.post("/request-quote")
def request_quote_submit(request: Request, company_name: str = Form(...), city: str = Form(...),
                         vehicles: str = Form(""), activity: str = Form(""), offer_id: str = Form(""),
                         service_text: str = Form(""), contact_name: str = Form(...), phone: str = Form(""),
                         email: str = Form(""), preferred_channel: str = Form("phone"), consent: str = Form(""),
                         notes: str = Form(""), src: str = Form(""), ref: str = Form(""), utm: str = Form(""),
                         website_url: str = Form(""), s=Depends(get_db)):
    from ..leads import create_lead, hash_ip, normalize_source, too_many
    if website_url:  # حقل مخفي لاصطياد البرامج الآلية
        return back("/request-quote?done=1")
    if not consent or not (normalize_phone(phone) or normalize_email(email)):
        flash(request, "يرجى إدخال رقم جوال أو بريد صحيح والموافقة على التواصل", "err")
        return back(f"/request-quote?src={src}&ref={ref}&utm_campaign={utm}")
    ip = hash_ip(request.client.host if request.client else None, settings.secret_key)
    if too_many(s, ip):
        flash(request, "تم استلام عدة طلبات من هذا الجهاز. سنتواصل معكم قريبًا.", "err")
        return back("/request-quote")
    try:
        nveh = max(0, min(int(vehicles), 100000)) if vehicles.strip() else None
    except ValueError:
        nveh = None
    create_lead(s, company_name=company_name, city=city, vehicles=nveh,
                activity=activity if activity in SECTORS else None,
                offer_id=int(offer_id) if offer_id.isdigit() else None, service_text=service_text,
                contact_name=contact_name, phone=phone, email=email,
                preferred_channel=preferred_channel if preferred_channel in ("phone", "email", "whatsapp") else "phone",
                source=normalize_source(src, ref, utm), notes=notes, ip_hash=ip)
    return back("/request-quote?done=1")


@app.get("/leads", response_class=HTMLResponse)
def leads_page(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from ..leads import source_label
    from ..models import InboundLead
    rows = list(s.scalars(select(InboundLead).order_by(InboundLead.id.desc()).limit(200)))
    companies_ = {c.id: c for c in s.scalars(select(Company).where(Company.id.in_([r.company_id for r in rows if r.company_id])))}
    base = settings.public_base_url.rstrip("/")
    return render(request, "leads.html", user, s, rows=rows, companies=companies_, source_label=source_label,
                  link=f"{base}/request-quote")


@app.get("/weekly-report", response_class=HTMLResponse)
def weekly_report_page(request: Request, day: str = "", user: User = Depends(require_manager), s=Depends(get_db)):
    from ..reports import weekly_segments
    d = date.fromisoformat(day) if day else riyadh_today()
    w = weekly_segments(s, d)
    return render(request, "weekly_report.html", user, s, w=w, prev=(w["start"] - timedelta(days=7)).isoformat(),
                  next=(w["start"] + timedelta(days=7)).isoformat())


# ---------------------------------------------------------------- الموظفون والصلاحيات (بوابة الإدارة)

def _perm_form(form) -> tuple[str, str, dict]:
    from ..permissions import ACTION_PERMS, DATA_PERMS, PROFILES
    profile = form.get("profile") or "sales_rep"
    if profile in PROFILES and profile != "custom" and not form.get("customize"):
        return profile, PROFILES[profile]["scope"], dict(PROFILES[profile]["perms"])
    perms = {k: form.get(k) == "on" for k in list(DATA_PERMS) + list(ACTION_PERMS)}
    scope = form.get("scope") if form.get("scope") in ("own", "team", "all") else "own"
    return "custom" if form.get("customize") else profile, scope, perms


@app.get("/staff", response_class=HTMLResponse)
def staff_page(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from ..models import AuditLog
    from ..permissions import ACTION_PERMS, DATA_PERMS, PROFILES, SCOPES, describe
    users = list(s.scalars(select(User).order_by(User.active.desc(), User.role, User.name)))
    counts = {u.id: s.scalar(select(func.count(Company.id)).where(Company.owner_id == u.id)) or 0 for u in users}
    audit_rows = list(s.scalars(select(AuditLog).where(AuditLog.action != "login").order_by(AuditLog.id.desc()).limit(60)))
    from ..auth import mail_ready
    return render(request, "staff.html", user, s, users=users, counts=counts, audit_rows=audit_rows, describe=describe,
                  PROFILES=PROFILES, SCOPES=SCOPES, DATA_PERMS=DATA_PERMS, ACTION_PERMS=ACTION_PERMS,
                  portal_link=settings.public_base_url.rstrip("/") + "/portal/login", mail_ok=mail_ready(s))


@app.post("/staff/add")
async def staff_add(request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from ..auth import audit, send_invite_email
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    name = (form.get("name") or "").strip()
    if not email or "@" not in email or not name:
        flash(request, "أدخل الاسم والبريد الوظيفي", "err")
        return back("/staff")
    if s.scalar(select(User.id).where(User.email == email)):
        flash(request, "هذا البريد مسجل مسبقًا", "err")
        return back("/staff")
    profile, scope, perms = _perm_form(form)
    role = "manager" if form.get("portal") == "admin" and user.role == "admin" else "rep"
    u = User(name=name, email=email, team=(form.get("team") or "").strip() or None, role=role, profile=profile,
             scope=scope, perms=perms, password_hash=hash_password(os.urandom(16).hex()), created_by=user.id)
    s.add(u)
    s.flush()
    audit(s, user, "add_user", u, email=email, team=u.team, role=role, profile=profile, scope=scope, perms=perms)
    sent = send_invite_email(s, u) if role == "rep" else False
    flash(request, f"أُضيف {name}. " + ("أُرسل له رابط بوابة الموظفين على بريده." if sent else
                                         "لم يُرسل البريد (بريد الشركة غير مربوط) — أرسل له رابط البوابة يدويًا."))
    return back(f"/staff/{u.id}")


@app.get("/staff/{uid}", response_class=HTMLResponse)
def staff_edit(uid: int, request: Request, q: str = "", user: User = Depends(require_manager), s=Depends(get_db)):
    from ..models import AuditLog
    from ..permissions import ACTION_PERMS, DATA_PERMS, PROFILES, SCOPES
    u = s.get(User, uid) or _404()
    assigned = list(s.scalars(select(Company).where(Company.owner_id == uid).order_by(Company.name).limit(300)))
    found = []
    if q:
        found = list(s.scalars(select(Company).where(Company.name.like(f"%{q}%") | Company.domain.like(f"%{q}%"))
                               .order_by(Company.name).limit(50)))
    colleagues = list(s.scalars(select(User).where(User.active.is_(True), User.id != uid).order_by(User.name)))
    audit_rows = list(s.scalars(select(AuditLog).where(AuditLog.target_user_id == uid).order_by(AuditLog.id.desc()).limit(40)))
    open_tasks = s.scalar(select(func.count(Task.id)).where(Task.owner_id == uid, Task.status == "open")) or 0
    return render(request, "staff_edit.html", user, s, u=u, assigned=assigned, found=found, q=q, colleagues=colleagues,
                  audit_rows=audit_rows, open_tasks=open_tasks, PROFILES=PROFILES, SCOPES=SCOPES,
                  DATA_PERMS=DATA_PERMS, ACTION_PERMS=ACTION_PERMS)


@app.post("/staff/{uid}/update")
async def staff_update(uid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from ..auth import audit
    u = s.get(User, uid) or _404()
    if u.role == "admin" and user.role != "admin":
        raise HTTPException(403)
    form = await request.form()
    before = {"team": u.team, "profile": u.profile, "scope": u.scope, "perms": dict(u.perms or {}), "name": u.name}
    profile, scope, perms = _perm_form(form)
    u.name = (form.get("name") or u.name).strip()
    u.team = (form.get("team") or "").strip() or None
    u.profile, u.scope, u.perms = profile, scope, perms
    after = {"team": u.team, "profile": u.profile, "scope": u.scope, "perms": perms, "name": u.name}
    audit(s, user, "update_permissions", u, before=before, after=after)
    flash(request, "حُفظت الصلاحيات، وتسري فورًا على أي طلب جديد من الموظف.")
    return back(f"/staff/{uid}")


@app.post("/staff/{uid}/assign")
async def staff_assign(uid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from ..auth import audit
    u = s.get(User, uid) or _404()
    form = await request.form()
    ids = [int(x) for x in form.getlist("company_ids") if str(x).isdigit()]
    names = []
    for c in s.scalars(select(Company).where(Company.id.in_(ids))):
        prev = c.owner.name if c.owner else None
        c.owner_id = u.id
        for t in s.scalars(select(Task).where(Task.company_id == c.id, Task.status == "open")):
            t.owner_id = u.id
        log_activity(s, c.id, "assigned", f"أُسندت الشركة إلى {u.name}", f"بواسطة {user.name}" + (f" (كانت لدى {prev})" if prev else ""),
                     user_id=user.id)
        names.append(c.name)
    if names:
        audit(s, user, "assign_customers", u, companies=names)
    flash(request, f"أُسند {len(names)} عميل إلى {u.name}")
    return back(f"/staff/{uid}")


@app.post("/staff/{uid}/deactivate")
def staff_deactivate(uid: int, request: Request, transfer_to: Optional[int] = Form(None),
                     user: User = Depends(require_manager), s=Depends(get_db)):
    from ..auth import audit
    u = s.get(User, uid) or _404()
    if u.id == user.id or (u.role == "admin" and user.role != "admin"):
        raise HTTPException(403, "لا يمكن إيقاف هذا الحساب")
    u.active = False
    u.session_version = (u.session_version or 1) + 1  # يغلق جلساته المفتوحة فورًا
    for lc in s.scalars(select(LoginCode).where(LoginCode.user_id == uid, LoginCode.used.is_(False))):
        lc.revoked = True
    moved = 0
    target = s.get(User, transfer_to) if transfer_to else None
    if target is not None and target.active:
        for c in s.scalars(select(Company).where(Company.owner_id == uid)):
            c.owner_id = target.id
            log_activity(s, c.id, "assigned", f"نُقلت الشركة من {u.name} إلى {target.name}",
                         "إيقاف حساب الموظف — سجل العروض والمحادثات محفوظ", user_id=user.id)
            moved += 1
        for t in s.scalars(select(Task).where(Task.owner_id == uid, Task.status == "open")):
            t.owner_id = target.id
    audit(s, user, "deactivate", u, transferred_to=target.name if target else None, customers_moved=moved)
    flash(request, f"أُوقف حساب {u.name} وأُغلقت جلساته" + (f"، ونُقل {moved} عميل ومتابعاتهم إلى {target.name}" if target else ""))
    return back(f"/staff/{uid}")


@app.post("/staff/{uid}/activate")
def staff_activate(uid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from ..auth import audit
    u = s.get(User, uid) or _404()
    u.active = True
    audit(s, user, "activate", u)
    flash(request, f"أُعيد تفعيل {u.name}")
    return back(f"/staff/{uid}")


@app.post("/staff/{uid}/invite")
def staff_invite(uid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db)):
    from ..auth import audit, send_invite_email
    u = s.get(User, uid) or _404()
    ok = send_invite_email(s, u)
    if ok:
        audit(s, user, "invite", u)
    flash(request, "أُرسلت الدعوة" if ok else "تعذر الإرسال: اربط بريد الشركة أولًا", "ok" if ok else "err")
    return back(f"/staff/{uid}")


# ---------------------------------------------------------------- بوابة الموظفين

_PHONE_RX = None


def mask_text(u: User, text: str | None) -> str:
    """يحجب الأرقام والإيميلات من النصوص الحرة إن لم يكن للموظف حق رؤيتها."""
    import re
    from ..permissions import can
    t = text or ""
    if not can(u, "see_phone"):
        t = re.sub(r"(?:\+|00)?[\d٠-٩][\d٠-٩\s\-()]{7,}[\d٠-٩]", "••• مخفي •••", t)
    if not can(u, "see_email"):
        t = re.sub(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+", "••• مخفي •••", t)
    return t


def _portal_rows(s, u: User, q: str = ""):
    from ..permissions import can, scoped
    qq = scoped(select(Company), s, u).where(Company.data_status.in_(["complete", "incomplete"]))
    if q:
        conds = [Company.name.like(f"%{q}%"), Company.activity.like(f"%{q}%"), Company.city.like(f"%{q}%")]
        # البحث بالهاتف/البريد فقط لمن يحق له رؤيتهما — وإلا لا يمكن الاستدلال عليهما بالبحث
        from ..models import Contact
        digits = "".join(ch for ch in q if ch.isdigit())
        if can(u, "see_phone") and len(digits) >= 5:
            conds.append(Company.id.in_(select(Contact.company_id).where(Contact.kind == "phone", Contact.value.like(f"%{digits}%"))))
        if can(u, "see_email") and "@" in q or (can(u, "see_email") and "." in q):
            conds.append(Company.id.in_(select(Contact.company_id).where(Contact.kind == "email", Contact.value.like(f"%{q.lower()}%"))))
        from sqlalchemy import or_
        qq = qq.where(or_(*conds))
    rows = []
    for c in s.scalars(qq.order_by(Company.next_action_at.is_(None), Company.next_action_at, Company.name).limit(500)):
        ph, em = c.primary("phone"), c.primary("email")
        last_offer = s.scalar(select(OutboundMessage).where(OutboundMessage.company_id == c.id, OutboundMessage.kind == "offer",
                                                            OutboundMessage.sent_at.isnot(None)).order_by(OutboundMessage.id.desc())) \
            if can(u, "see_offers") else None
        last_reply = s.scalar(select(InboundMessage).where(InboundMessage.company_id == c.id, InboundMessage.message_type == "reply")
                              .order_by(InboundMessage.received_at.desc())) if can(u, "see_replies") else None
        nxt = s.scalar(select(Task).where(Task.company_id == c.id, Task.status == "open").order_by(Task.due_at))
        rows.append({"c": c, "phone": format_phone_display(ph.value) if ph and can(u, "see_phone") else None,
                     "email": em.value if em and can(u, "see_email") else None,
                     "phone_src": ph.source_url if ph and can(u, "see_phone") else None,
                     "phones": [(format_phone_display(x.value), x.source_url) for x in _owned_phones(c)] if can(u, "see_phone") else [],
                     "email_src": em.source_url if em and can(u, "see_email") else None, "offer": last_offer, "reply": last_reply,
                     "next": nxt})
    return rows


@app.get("/portal", response_class=HTMLResponse)
def portal_home(request: Request, q: str = "", user: User = Depends(staff_user), s=Depends(get_db)):
    from ..permissions import can, describe
    rows = _portal_rows(s, user, q)
    return render(request, "portal_home.html", user, s, rows=rows, q=q, can=lambda p: can(user, p),
                  d=describe(user), mask=lambda t: mask_text(user, t), csum=_csum(s, [r["c"] for r in rows]))


@app.get("/portal/export.xlsx")
def portal_export(request: Request, q: str = "", user: User = Depends(staff_user), s=Depends(get_db)):
    from openpyxl import Workbook
    import io as _io
    from ..auth import audit
    from ..permissions import can
    if not can(user, "export"):
        raise HTTPException(403, "لا تملك صلاحية التصدير")
    wb = Workbook()
    wb.properties.title, wb.properties.creator = "المندوب الذكي لسيارة اب", "المندوب الذكي لسيارة اب"
    ws = wb.active
    ws.title = "عملائي"
    ws.sheet_view.rightToLeft = True
    head = ["الشركة", "النشاط", "المدينة", "الحالة", "تاريخ إرسال العرض (المسجّل)", "مؤشر استخدام المركبات"] + \
        (["الهاتف", "مصدر الهاتف"] if can(user, "see_phone") else []) + \
        (["البريد", "مصدر البريد"] if can(user, "see_email") else []) + (["آخر عرض", "تاريخ الإرسال"] if can(user, "see_offers") else []) + \
        (["آخر رد", "نوع الرد"] if can(user, "see_replies") else []) + ["المتابعة القادمة", "موعدها"]
    from ..xlsx import CONTACT_HEADERS, contact_cells
    head += CONTACT_HEADERS
    ws.append(head)
    for r in _portal_rows(s, user, q):
        c = r["c"]
        row = [c.name, c.activity, c.city, STAGE_LABELS.get(c.stage, c.stage), fmt(c.offer_sent_at) if c.offer_sent_at else "",
               "مؤكد" if c.vehicle_indicator == "confirmed" or (not c.vehicle_indicator and c.fit_phrase) else "غير مؤكد"] + \
            ([r["phone"] or "", r["phone_src"] or ""] if can(user, "see_phone") else []) + \
            ([r["email"] or "", r["email_src"] or ""] if can(user, "see_email") else [])
        if can(user, "see_offers"):
            row += [r["offer"].subject if r["offer"] else "", fmt(r["offer"].sent_at) if r["offer"] else ""]
        if can(user, "see_replies"):
            row += [mask_text(user, r["reply"].body_text[:500]) if r["reply"] else "", r["reply"].intent_label if r["reply"] else ""]
        row += [r["next"].title if r["next"] else "", fmt(r["next"].due_at) if r["next"] else ""]
        row += contact_cells(s, c)
        ws.append(row)
    for row_ in ws.iter_rows(min_row=2):
        for cell in row_:
            if isinstance(cell.value, str):
                cell.data_type = "s"  # نص لا صيغة
    buf = _io.BytesIO()
    from ..i18n import localize_workbook
    localize_workbook(wb)
    wb.save(buf)
    audit(s, user, "export", user, rows=ws.max_row - 1)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=my-customers.xlsx"})


def _portal_company(s, user: User, cid: int) -> Company:
    from ..permissions import can_view_company
    c = s.get(Company, cid)
    if not can_view_company(s, user, c):
        raise HTTPException(404, "غير موجود")  # لا نكشف وجود سجل لا يخصه
    return c


@app.get("/portal/companies/{cid}", response_class=HTMLResponse)
def portal_company(cid: int, request: Request, user: User = Depends(staff_user), s=Depends(get_db)):
    from ..permissions import can
    c = _portal_company(s, user, cid)
    offer_kinds = {"email_sent", "email_delivered", "email_bounced"}
    reply_kinds = {"reply", "auto_reply", "opt_out"}
    timeline = []
    for a in company_timeline(s, cid):
        if a.kind in offer_kinds and not can(user, "see_offers"):
            continue
        if a.kind in reply_kinds and not can(user, "see_replies"):
            continue
        timeline.append(a)
    tasks = list(s.scalars(select(Task).where(Task.company_id == cid, Task.status == "open").order_by(Task.due_at)))
    msgs = list(s.scalars(select(OutboundMessage).where(OutboundMessage.company_id == cid).order_by(OutboundMessage.id.desc()))) \
        if can(user, "see_offers") else []
    from ..contact import STATUSES, UPDATE_CHOICES, history, summary
    from ..permissions import visible_owner_ids
    ids = visible_owner_ids(s, user)
    owners = list(s.scalars(select(User).where(User.active.is_(True), *( [User.id.in_(ids)] if ids is not None else []))))
    return render(request, "portal_company.html", user, s, c=c, timeline=timeline, tasks=tasks, msgs=msgs,
                  can=lambda p: can(user, p), mask=lambda t: mask_text(user, t), cs=summary(s, c),
                  chist=history(s, c.id), STATUSES=STATUSES, UPDATE_CHOICES=UPDATE_CHOICES, owners=owners,
                  base=f"/portal/companies/{c.id}")


def _require(user: User, perm: str):
    from ..permissions import can
    if not can(user, perm):
        raise HTTPException(403, "لا تملك صلاحية هذا الإجراء")


@app.post("/portal/companies/{cid}/note")
def portal_note(cid: int, request: Request, note: str = Form(...), user: User = Depends(staff_user), s=Depends(get_db)):
    _require(user, "add_note")
    c = _portal_company(s, user, cid)
    c.last_action, c.last_action_at = note[:300], now_utc()
    log_activity(s, cid, "note", f"ملاحظة من {user.name}", note, user_id=user.id)
    flash(request, "أُضيفت الملاحظة")
    return back(f"/portal/companies/{cid}")


@app.post("/portal/companies/{cid}/status")
def portal_status(cid: int, request: Request, stage: str = Form(""), next_title: str = Form(""), next_at: str = Form(""),
                  done_task: Optional[int] = Form(None), offer_at: str = Form(""), offer_note: str = Form(""),
                  user: User = Depends(staff_user), s=Depends(get_db)):
    _require(user, "update_status")
    c = _portal_company(s, user, cid)
    changes = []
    if stage in ("contacted", "offer_sent", "needs_followup", "replied", "interested", "meeting", "won", "lost") \
            and stage != c.stage:
        changes.append(f"الحالة: {STAGE_LABELS.get(c.stage)} ← {STAGE_LABELS.get(stage)}")
        c.stage = stage
        if stage == "won":
            c.won_at = now_utc()
    if stage in ("contacted", "offer_sent", "needs_followup") and not c.contacted_at:
        c.contacted_at, c.contacted_by_id = now_utc(), user.id
    if stage == "offer_sent":
        # تاريخ الإرسال كما سجّله الموظف (تواصل بنفسه من بريده أو هاتفه)
        when = riyadh_to_utc(datetime.fromisoformat(offer_at)) if offer_at else now_utc()
        c.offer_sent_at, c.offer_sent_by_id = when, user.id
        c.offer_note = (offer_note or "").strip()[:300] or c.offer_note
        changes.append(f"أُرسل عرض بتاريخ {fmt(when)}" + (f" — {c.offer_note}" if c.offer_note else ""))
    if done_task:
        t = s.get(Task, done_task)
        if t and t.company_id == cid and t.status == "open":
            t.status, t.done_at = "done", now_utc()
            changes.append(f"أُنجزت: {t.title}")
    if next_at:
        due = riyadh_to_utc(datetime.fromisoformat(next_at))
        s.add(Task(company_id=cid, owner_id=c.owner_id or user.id, kind="call", title=next_title or f"متابعة {c.name}", due_at=due))
        c.next_action, c.next_action_at = next_title or "متابعة", due
        changes.append(f"المتابعة القادمة: {fmt(due)}")
    if changes:
        c.last_action, c.last_action_at = "؛ ".join(changes)[:300], now_utc()
        log_activity(s, cid, "status", f"تحديث من {user.name}", "\n".join(changes), user_id=user.id)
        flash(request, "حُدّثت المتابعة")
    return back(f"/portal/companies/{cid}")


@app.post("/portal/companies/{cid}/send-offer")
def portal_send_offer(cid: int, request: Request, user: User = Depends(staff_user), s=Depends(get_db)):
    from ..outreach import active_account, check_eligibility, compose, dedupe_key, ensure_email_basis, offer_for, \
        render_html, render_text, send_due
    _require(user, "send_offer")
    if not settings.auto_outreach_enabled:
        raise HTTPException(403, "الإرسال من المنصة معطّل؛ أرسل العرض بنفسك ثم سجّل «أُرسل عرض»")
    c = _portal_company(s, user, cid)
    offer = offer_for(c, s)
    acct = active_account(s)
    if offer is None or acct is None:
        flash(request, "لا يوجد عرض معتمد مناسب أو بريد الشركة غير مربوط", "err")
        return back(f"/portal/companies/{cid}")
    ok, why = check_eligibility(s, c, offer)
    if not ok:
        flash(request, f"لا يمكن الإرسال: {why}", "err")
        return back(f"/portal/companies/{cid}")
    em = c.primary("email")
    ensure_email_basis(s, c, em)
    subject, body = compose(c, offer, acct.sender_name)
    camp = c.campaign
    s.add(OutboundMessage(company_id=c.id, campaign_id=c.campaign_id, category_id=c.category_id, offer_id=offer.id,
                          dedupe_key=dedupe_key(c.id, offer.key), mail_account_id=acct.id, from_address=acct.sender_address,
                          to_address=(camp.test_recipient if camp and camp.test_recipient else em.value), intended_to=em.value,
                          subject=subject, body_text=render_text(body, acct.signature_html),
                          body_html=render_html(body, acct.signature_html),
                          attachment_name=offer.attachment.filename if offer.attachment else None))
    s.commit()
    send_due(c.campaign_id, ignore_window=True, max_batch=5)
    log_activity(s, cid, "note", f"أرسل {user.name} العرض يدويًا", offer.service_name, user_id=user.id)
    flash(request, "أُرسل العرض")
    return back(f"/portal/companies/{cid}")


@app.post("/portal/companies/{cid}/contact/start")
def portal_contact_start(cid: int, request: Request, user: User = Depends(staff_user), s=Depends(get_db)):
    _require(user, "update_status")
    c = _portal_company(s, user, cid)
    return _contact_start(request, s, user, c, _safe_back(request, f"/portal/companies/{cid}"))


@app.post("/portal/companies/{cid}/contact/update")
async def portal_contact_update(cid: int, request: Request, user: User = Depends(staff_user), s=Depends(get_db)):
    from ..permissions import visible_owner_ids
    _require(user, "update_status")
    c = _portal_company(s, user, cid)
    form = await request.form()
    ids = visible_owner_ids(s, user)
    owners = set(ids) if ids is not None else {u.id for u in s.scalars(select(User).where(User.active.is_(True)))}
    return _contact_update(request, s, user, c, form, _safe_back(request, f"/portal/companies/{cid}"), owners)


@app.get("/portal/officials", response_class=HTMLResponse)
def portal_officials(request: Request, user: User = Depends(staff_user), s=Depends(get_db)):
    """تقرير مسؤولي الشركات: جهة القرار ووسائل التواصل وحالة التواصل للمنشآت المسندة للموظف فقط."""
    from ..permissions import can
    rows = _portal_rows(s, user, "")
    return render(request, "portal_officials.html", user, s, rows=rows, can=lambda p: can(user, p),
                  csum=_csum(s, [r["c"] for r in rows]), offs=_officials_map(s, [r["c"].id for r in rows]),
                  **_official_ctx())


def _officials_map(s, ids):
    from ..models import CompanyOfficial
    out: dict = {}
    for o in s.scalars(select(CompanyOfficial).where(CompanyOfficial.company_id.in_(ids or [0]))
                       .order_by(CompanyOfficial.is_primary.desc(), CompanyOfficial.id)):
        out.setdefault(o.company_id, []).append(o)
    return out


def _official_ctx():
    from .. import officials as O
    return {"OSTATUS": O.STATUS, "OSOURCES": O.SOURCE_TYPES}


OFFICIAL_HEADERS = ["المسؤول الموثّق", "المنصب", "نبذة عن دوره", "حالة التحقق", "رابط الدليل", "نوع المصدر", "تاريخ آخر تحقق",
                    "بريد المسؤول المهني", "هاتف المسؤول المهني"]


def _official_cells(c, os_):
    from .. import officials as O
    o = next((x for x in os_ if x.is_primary), os_[0] if os_ else None)
    if o is None:
        return ["لم يُعثر على مسؤول موثّق" if c.officials_checked_at else "لم يُبحث بعد"] + [""] * 8
    return [o.name, o.title, o.summary, O.STATUS.get(o.status, o.status) + (f" ({o.status_reason})" if o.status_reason else ""),
            o.evidence_url, O.SOURCE_TYPES.get(o.source_type, o.source_type), fmt(o.verified_at), o.email or "", o.phone or ""]


@app.get("/officials", response_class=HTMLResponse)
def admin_officials(request: Request, ostatus: str = "", user: User = Depends(current_user), s=Depends(get_db)):
    """«موظفو الشركات» في بوابة الإدارة: جهة القرار المقترحة + المسؤول الموثّق + وسائل التواصل وحالة التواصل."""
    from .. import officials as O
    rows = _portal_rows(s, user, "")
    all_n = len(rows)
    if ostatus == "pending":
        rows = [r for r in rows if r["c"].officials_checked_at is None]
    elif ostatus in ("verified", "needs_check", "none"):
        rows = [r for r in rows if r["c"].official_status == ostatus and r["c"].officials_checked_at is not None]
    cfg = O.get_auto(s)
    cnt = lambda st: s.scalar(select(func.count(Company.id)).where(Company.official_status == st)) or 0  # noqa: E731
    ostats = {"verified": cnt("verified"), "needs": cnt("needs_check"), "none": cnt("none"), "total": all_n,
              "pending": s.scalar(select(func.count(Company.id)).where(
                  Company.is_test.is_(False), Company.officials_checked_at.is_(None),
                  (Company.data_status == "complete") | Company.contactable.is_(True))) or 0,
              "spent": round(O.spent_today(s), 2),
              "last": fmt(datetime.fromisoformat(cfg["last_cycle_at"])) if cfg.get("last_cycle_at") else None}
    return render(request, "portal_officials.html", user, s, rows=rows, can=lambda p: True, cbase="/companies",
                  xlsx_url="/officials.xlsx", csum=_csum(s, [r["c"] for r in rows]),
                  offs=_officials_map(s, [r["c"].id for r in rows]), ocfg=cfg, ostats=ostats, ostatus=ostatus,
                  all_n=all_n, **_official_ctx())


@app.post("/officials/control")
def officials_control(request: Request, action: str = Form(...), user: User = Depends(require_manager), s=Depends(get_db)):
    from .. import officials as O
    if action == "pause":
        O.set_auto(s, paused=True)
        flash(request, "أُوقف البحث عن أصحاب القرار. ما وُجد يبقى كما هو.")
    elif action == "resume":
        O.set_auto(s, paused=False)
        flash(request, "استُؤنف البحث عن أصحاب القرار — الدورة التالية خلال دقائق.")
    elif action == "run":
        O.set_auto(s, paused=False, last_cycle_at=None)
        flash(request, "طُلبت دورة بحث الآن — تبدأ على الخادم خلال ٥ دقائق، ابتداءً بالمنشآت التي لم يُحدَّد مسؤولها.")
    return back("/officials")


@app.get("/officials.xlsx")
def admin_officials_xlsx(request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    from ..xlsx import CONTACT_HEADERS, contact_cells, response, workbook
    prs = _portal_rows(s, user, "")
    offs = _officials_map(s, [r["c"].id for r in prs])
    rows = [[r["c"].name, r["c"].activity, r["c"].city, r["c"].decision_unit or ""] + _official_cells(r["c"], offs.get(r["c"].id, []))
            + [r["phone"] or "", r["email"] or ""] + contact_cells(s, r["c"]) for r in prs]
    head = ["المنشأة", "النشاط", "المدينة", "جهة القرار المقترحة"] + OFFICIAL_HEADERS + \
        ["هاتف الشركة العام", "بريد الشركة العام"] + CONTACT_HEADERS
    return response(workbook([("موظفو الشركات", head, rows)]), "company-officials.xlsx")


@app.get("/portal/officials.xlsx")
def portal_officials_xlsx(request: Request, user: User = Depends(staff_user), s=Depends(get_db)):
    from ..auth import audit
    from ..permissions import can
    from ..xlsx import CONTACT_HEADERS, contact_cells, response, workbook
    _require(user, "export")
    rows = []
    for r in _portal_rows(s, user, ""):
        c = r["c"]
        rows.append([c.name, c.activity, c.city, c.decision_unit or ""]
                    + (lambda cells: cells[:7] + [cells[7] if can(user, "see_email") else "", cells[8] if can(user, "see_phone") else ""])(
                        _official_cells(c, _officials_map(s, [c.id]).get(c.id, [])))
                    + ([r["phone"] or ""] if can(user, "see_phone") else [])
                    + ([r["email"] or ""] if can(user, "see_email") else []) + contact_cells(s, c))
    head = ["المنشأة", "النشاط", "المدينة", "جهة القرار المقترحة"] + OFFICIAL_HEADERS + \
        (["هاتف الشركة العام"] if can(user, "see_phone") else []) + (["بريد الشركة العام"] if can(user, "see_email") else []) + CONTACT_HEADERS
    audit(s, user, "export", user, kind="officials", rows=len(rows))
    return response(workbook([("مسؤولو الشركات", head, rows)]), "company-officials.xlsx")


@app.get("/portal/reports.xlsx")
def portal_reports_xlsx(request: Request, user: User = Depends(staff_user), s=Depends(get_db)):
    from ..contact import STATUSES
    from ..permissions import scoped
    from ..xlsx import response, workbook
    _require(user, "see_reports")
    _require(user, "export")
    comps = list(s.scalars(scoped(select(Company), s, user)))
    by = {}
    for c in comps:
        k = STATUSES.get(c.contact_status or "new", c.contact_status)
        by[k] = by.get(k, 0) + 1
    return response(workbook([("تقرير مبيعاتي", ["حالة التواصل", "عدد العملاء"], sorted(by.items()))]),
                    "my-sales-report.xlsx")


@app.get("/portal/messages/{mid}", response_class=HTMLResponse)
def portal_message(mid: int, request: Request, user: User = Depends(staff_user), s=Depends(get_db)):
    _require(user, "see_offers")
    m = s.get(OutboundMessage, mid) or _404()
    _portal_company(s, user, m.company_id)
    return render(request, "portal_message.html", user, s, m=m, mask=lambda t: mask_text(user, t))


@app.get("/portal/reports", response_class=HTMLResponse)
def portal_reports(request: Request, user: User = Depends(staff_user), s=Depends(get_db)):
    from ..permissions import scoped
    _require(user, "see_reports")
    base = scoped(select(Company), s, user).subquery()
    by_stage = dict(s.execute(select(base.c.stage, func.count()).group_by(base.c.stage)).all())
    ids = [r[0] for r in s.execute(select(base.c.id))]
    sent = s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.company_id.in_(ids), OutboundMessage.kind == "offer",
                                                                  OutboundMessage.sent_at.isnot(None))) or 0
    replies = s.scalar(select(func.count(InboundMessage.id)).where(InboundMessage.company_id.in_(ids),
                                                                   InboundMessage.message_type == "reply")) or 0
    meetings = s.scalar(select(func.count(Meeting.id)).where(Meeting.company_id.in_(ids))) or 0
    return render(request, "portal_reports.html", user, s, by_stage=by_stage, total=len(ids), sent=sent, replies=replies,
                  meetings=meetings)


# ---------------------------------------------------------------- Webhooks

@app.post("/webhooks/voice")
async def voice_webhook(request: Request):
    if not settings.voice_calls_enabled:
        raise HTTPException(404)
    from ..voice import handle_post_call, verify_webhook
    body = await request.body()
    if not verify_webhook(body, request.headers.get("elevenlabs-signature"), settings.voice_webhook_secret):
        raise HTTPException(401, "invalid signature")
    payload = json.loads(body)
    with session_scope() as s:
        call = handle_post_call(s, payload)
    return JSONResponse({"ok": True, "call_id": call.id if call else None})


def _404():
    raise HTTPException(404, "غير موجود")
