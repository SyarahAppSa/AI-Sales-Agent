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
from ..emergency import DownloadGuard  # noqa: E402
app.add_middleware(DownloadGuard)  # منع كل تنزيل وتسجيل المحاولة (يحتاج الجلسة لمعرفة المستخدم)
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


def current_user(request: Request, s=Depends(get_db, scope="function")) -> User:
    """بوابة الإدارة."""
    user = _session_user(request, s, "admin")
    if user is None:
        raise LoginRequired("/portal" if request.session.get("portal") == "staff" else "/admin/login")
    return user


def staff_user(request: Request, s=Depends(get_db, scope="function")) -> User:
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
    emerg = 0
    if user is not None and s is not None:
        from ..emergency import can_access
        from ..models import DownloadAttempt
        if can_access(user):
            emerg = s.scalar(select(func.count(DownloadAttempt.id)).where(DownloadAttempt.read.is_(False))) or 0
    cm_unread = 0
    if user is not None and s is not None and user.role in ("admin", "manager"):
        from ..customer_mail import unread_count
        cm_unread = unread_count(s)
    return templates.TemplateResponse(request, name, {"user": user, "unread": unread, "flash": flash, "demo_on": demo,
                                                      "emerg_unread": emerg, "cm_unread": cm_unread, "can_emergency": _can_emerg(user),
                                                      # العلامة المائية لكل الحسابات عدا مدير المنصة (المالك)
                                                      "wm": _watermark(user) if user is not None and user.role != "admin" else "",
                                                      "voice_on": settings.voice_calls_enabled,
                                                      "outreach_on": settings.auto_outreach_enabled,
                                                      "path": request.url.path, **ctx})


def _can_emerg(user) -> bool:
    from ..emergency import can_access
    return can_access(user)


def _watermark(user) -> str:
    """علامة مائية متكررة: اسم المستخدم ومعرّفه ووقت العرض (بتوقيت الرياض) — صورة SVG خفيفة خلف المحتوى."""
    from html import escape
    from urllib.parse import quote
    t = escape(f"{user.name} · #{user.id} · {fmt(now_utc())}")
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='380' height='190'><text x='16' y='105' "
           f"transform='rotate(-20 190 95)' font-family='Tahoma,Arial' font-size='14' font-weight='700' fill='%23292431' "
           f"fill-opacity='0.13'>{t}</text></svg>")
    return "url(\"data:image/svg+xml;utf8," + quote(svg, safe="/:=' %,.()") + "\")"


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
def admin_login_page(request: Request, s=Depends(get_db, scope="function")):
    return _login_page(request, "admin", s)


@app.get("/portal/login", response_class=HTMLResponse)
def staff_login_page(request: Request, s=Depends(get_db, scope="function")):
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
def admin_request_code(request: Request, email: str = Form(...), s=Depends(get_db, scope="function")):
    _request_code(request, "admin", email, s)
    return back("/admin/login")


@app.post("/portal/login/request")
def staff_request_code(request: Request, email: str = Form(...), s=Depends(get_db, scope="function")):
    _request_code(request, "staff", email, s)
    return back("/portal/login")


@app.post("/admin/login/verify")
def admin_verify(request: Request, code: str = Form(...), s=Depends(get_db, scope="function")):
    return back("/" if _verify_code(request, "admin", code, s) else "/admin/login")


@app.post("/portal/login/verify")
def staff_verify(request: Request, code: str = Form(...), s=Depends(get_db, scope="function")):
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
def admin_bootstrap_password(request: Request, email: str = Form(...), password: str = Form(...), s=Depends(get_db, scope="function")):
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
def switch_lang(request: Request, to: str = "ar", next: str = "/", s=Depends(get_db, scope="function")):
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


def require_emergency(user: User = Depends(current_user)) -> User:
    if not _can_emerg(user):
        raise HTTPException(403, "لوحة الطوارئ للمالك ومن يمنحه صلاحية الطوارئ فقط")
    return user


@app.get("/emergency", response_class=HTMLResponse)
def emergency_page(request: Request, who: str = "", day_from: str = "", day_to: str = "", etype: str = "",
                   user: User = Depends(require_emergency), s=Depends(get_db, scope="function")):
    from .. import emergency as E
    from ..config import settings as _st
    from ..models import DownloadAttempt
    attempts = list(s.scalars(select(DownloadAttempt).order_by(DownloadAttempt.id.desc()).limit(100)))
    repeats = {(u, t): n for u, t, n in s.execute(select(DownloadAttempt.user_id, DownloadAttempt.target,
                                                         func.count(DownloadAttempt.id))
                                                  .group_by(DownloadAttempt.user_id, DownloadAttempt.target))}
    unread_ids = [a.id for a in attempts if not a.read]
    def _d(v):
        try:
            return date.fromisoformat(v) if v else None
        except ValueError:
            return None
    svcs = E.services(s)
    page = render(request, "emergency.html", user, s, attempts=attempts, repeats=repeats, unread_ids=set(unread_ids),
                  svcs=svcs, mon=E.monitor(s, svcs), tl=E.timeline(s, who, _d(day_from), _d(day_to), etype), EVENT_TYPES=E.EVENT_TYPES,
                  who=who, day_from=day_from, day_to=day_to, etype=etype, blocked=_st.downloads_blocked,
                  render_key=bool(_st.render_api_key), now=now_utc(), dt=E._dt)
    # فتح اللوحة = قراءة التنبيهات (تبقى غير مقروءة حتى تُفتح)
    for a in s.scalars(select(DownloadAttempt).where(DownloadAttempt.read.is_(False))):
        a.read = True
    for n in s.scalars(select(Notification).where(Notification.user_id == user.id, Notification.read.is_(False),
                                                  Notification.title.like("مُنع تنزيل%"))):
        n.read = True
    return page


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- الرئيسية

@app.get("/", response_class=HTMLResponse)
def home(request: Request, period: str = "7d", user: User = Depends(current_user), s=Depends(get_db, scope="function")):
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
    from ..goal import achieved as _ach, shortfall_reason as _why
    goal_n = _ach(s)
    return render(request, "dashboard.html", user, s, adm=adm, cards=cards, links=links, target=daily_target(s),
                  goal_n=goal_n, goal_why=_why(s) if goal_n < daily_target(s) else "",
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
                   q: str = "", owner: str = "", all: int = 0, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
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
                  purchases=P.purchases_report(s),
                  gkey=with_key, gspent=round(P.google_spent_today(s), 2), auto=P.get_auto(s), SOURCE_LABEL=P.SOURCE_LABEL, total=total, d=d, city=city,
                  sector=sector, level=level, state=state, q=q, owner=owner, all=all, cities=sorted(cities),
                  ASECTORS=ASECTORS, LEVELS=P.LEVELS, CONTACT_LEVELS=P.CONTACT_LEVELS, last=last,
                  csum=_csum(s, rows), qs=str(request.query_params),
                  staff=list(s.scalars(select(User).where(User.role == "rep", User.active.is_(True)).order_by(User.name))))


@app.get("/prospects.xlsx")
def prospects_xlsx(request: Request, day: str = "", city: str = "", sector: str = "", level: str = "", state: str = "ready",
                   q: str = "", owner: str = "", user: User = Depends(current_user), s=Depends(get_db, scope="function")):
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
def prospects_run(request: Request, target: int = Form(100), user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from .. import prospects as P
    r = P.request_run(s, user, target, P.get_auto(s)["sources"])
    from ..auth import audit
    audit(s, user, "prospects_run", None, batch=r.id, target=r.target)
    flash(request, f"بدأت دفعة «العملاء المحتملون» #{r.id} على الخادم — الهدف {r.target} منشأة قابلة للتواصل. "
                   "تتحدث الصفحة تلقائيًا.")
    return back("/prospects")


@app.post("/prospects/import")
async def prospects_import(request: Request, file: UploadFile = File(...), license_note: str = Form(""),
                           src_kind: str = Form("device"), src_url: str = Form(""), provider: str = Form(""),
                           data_updated_on: str = Form(""), sample_cost: str = Form(""),
                           user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    """ملف مزوّد بيانات مرخّص (CSV أو Excel): يُضاف إلى طابور المعالجة اليومية."""
    from .. import prospects as P
    from ..auth import audit
    license_note = _source_note(user, src_kind, src_url, license_note)
    data = await file.read()
    if len(data) > 30 * 1024 * 1024:
        flash(request, "الملف أكبر من ٣٠ ميغابايت — قسّمه إلى ملفات أصغر.", "err")
        return back("/prospects")
    purchase = None
    if provider.strip():  # عيّنة بيانات مشتراة: المزوّد وتاريخ تحديث البيانات وتكلفتها
        from ..models import DataPurchase
        try:
            upd = date.fromisoformat(data_updated_on) if data_updated_on else None
        except ValueError:
            upd = None
        try:
            cost = max(0.0, float(re.sub(r"[^\d.]", "", sample_cost) or 0))
        except ValueError:
            cost = 0.0
        purchase = DataPurchase(provider=provider.strip()[:200], data_updated_on=upd, cost_usd=cost,
                                file_name=(file.filename or "")[:200], license_note=license_note.strip()[:500],
                                uploaded_by=user.id)
        s.add(purchase)
        s.flush()
        if cost:
            from ..costs import record_cost
            record_cost("data_purchase", 1, cost, note=f"purchase:{purchase.id}", session=s)
    n, cols = P.queue_upload(s, user, file.filename or "upload.csv", data, license_note.strip(),
                             purchase_id=purchase.id if purchase else None)
    audit(s, user, "import", user, kind="prospects", rows=n, file=(file.filename or "")[:100])
    if purchase is not None and n:
        r = P.request_run(s, user, target=n, sources="import")  # تُعالج العيّنة فورًا
        flash(request, f"عيّنة «{purchase.provider}»: {purchase.rows} سجلًا — {n} في طابور المعالجة و"
                       f"{purchase.duplicate_in_file} مكرر داخل الملف أو لدينا. بدأت معالجتها (دفعة #{r.id}).")
        return back("/prospects#purchases")
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
templates.env.filters["textdir"] = lambda v: "rtl" if re.search(r"[\u0600-\u06FF]", str(v or "")) else "ltr"
# رقم إصدار ملف التنسيق: يتغير مع كل نشر يعدّله، فلا يبقى المتصفح على نسخة قديمة محفوظة
templates.env.filters["isodt"] = lambda v: datetime.fromisoformat(v) if v else None
templates.env.globals["css_v"] = hashlib.sha256((HERE / "static" / "app.css").read_bytes()).hexdigest()[:10]


def _scope_label(sc) -> str:
    from mandoob import geo as _g
    sc = sc or {}
    if not isinstance(sc, dict) or sc.get("all") or not (sc.get("places") or sc.get("regions")):
        return "جميع مناطق المملكة"
    parts = [_g.REGIONS[r]["ar"] for r in sc.get("regions") or [] if r in _g.REGIONS]
    parts += [_g.PLACES[p]["ar"] for p in sc.get("places") or [] if p in _g.PLACES]
    return "، ".join(parts) or "—"


templates.env.filters["scope_label"] = _scope_label
templates.env.filters["todt"] = lambda v: datetime.fromisoformat(v) if isinstance(v, str) and v else v


LIST_STATUS = {"replied": "تم التواصل — رد", "no_answer": "تم التواصل — لم يرد", "wrong_number": "رقم خاطئ",
               "do_not_contact": "طلب عدم التواصل"}


def _admin_lists_view(request, user, s, lid, q, page, cstate, base, owner=""):
    """الإدارة: كل القوائم مع الإسناد. الموظف: ما أُسند إليه فقط (القوائم التي فيها صفوف مسندة إليه)."""
    from ..models import AdminList, AdminListAssignment, AdminListContact
    staff = base.startswith("/portal")
    if staff:
        mine = list(s.execute(select(AdminListAssignment.list_id, AdminListAssignment.row_idx)
                              .where(AdminListAssignment.owner_id == user.id)))
        my_lists = {l_ for l_, _ in mine}
        lists = [x for x in s.execute(select(AdminList.id, AdminList.name, AdminList.sheet, AdminList.created_at, AdminList.note)
                                      .order_by(AdminList.id.desc())) if x[0] in my_lists]
        if lid not in my_lists:
            lid = lists[0][0] if lists else None
    else:
        lists = list(s.execute(select(AdminList.id, AdminList.name, AdminList.sheet, AdminList.created_at, AdminList.note)
                               .order_by(AdminList.id.desc())))
    cur = s.get(AdminList, lid) if lid else (s.get(AdminList, lists[0][0]) if lists else None)
    rows, total, kinds, cmap, amap = [], 0, {}, {}, {}
    counts = {"all": 0, "none": 0, "done": 0, "replied": 0, "unassigned": 0, "assigned": 0}
    if cur is not None:
        kinds = _col_kinds(cur.headers or [], cur.rows or [])
        cmap = {x.row_idx: x for x in s.scalars(select(AdminListContact).where(AdminListContact.list_id == cur.id))}
        amap = {x.row_idx: x for x in s.scalars(select(AdminListAssignment).where(AdminListAssignment.list_id == cur.id))}
        data = list(enumerate(cur.rows or []))
        if staff:
            data = [(i, r) for i, r in data if i in amap and amap[i].owner_id == user.id]
        idxs = {i for i, _ in data}
        counts = {"all": len(data), "done": sum(1 for i in idxs if i in cmap), "none": sum(1 for i in idxs if i not in cmap),
                  "replied": sum(1 for i in idxs if i in cmap and cmap[i].status == "replied"),
                  "unassigned": sum(1 for i in idxs if i not in amap), "assigned": sum(1 for i in idxs if i in amap)}
        if q:
            ql = q.strip().lower()
            data = [(i, r) for i, r in data if any(ql in str(c).lower() for c in r)]
        if cstate == "none":
            data = [(i, r) for i, r in data if i not in cmap]
        elif cstate == "done":
            data = [(i, r) for i, r in data if i in cmap]
        elif cstate == "unassigned":
            data = [(i, r) for i, r in data if i not in amap]
        elif cstate == "assigned":
            data = [(i, r) for i, r in data if i in amap]
        elif cstate in LIST_STATUS:
            data = [(i, r) for i, r in data if i in cmap and cmap[i].status == cstate]
        if owner.isdigit():
            data = [(i, r) for i, r in data if i in amap and amap[i].owner_id == int(owner)]
        total = len(data)
        rows = data[(page - 1) * 200: page * 200]
    if staff:
        sizes = {}
        for i, *_ in lists:
            sizes[i] = s.scalar(select(func.count(AdminListAssignment.id)).where(AdminListAssignment.list_id == i,
                                                                                  AdminListAssignment.owner_id == user.id)) or 0
    else:
        sizes = {i: len(s.get(AdminList, i).rows or []) for i, *_ in lists}
    skip_first = bool(cur and cur.headers and str(cur.headers[0]).strip() in ("م", "#", "No", "no", "رقم", "ت", "مسلسل"))
    staff_users = list(s.scalars(select(User).where(User.active.is_(True), User.role == "rep").order_by(User.name))) if not staff else []
    lstats: dict = {}
    aq = select(AdminListAssignment.list_id, func.count(AdminListAssignment.id))
    cq = select(AdminListContact.list_id, func.count(AdminListContact.id))
    if staff:
        aq = aq.where(AdminListAssignment.owner_id == user.id)
        cq = cq.join(AdminListAssignment, (AdminListAssignment.list_id == AdminListContact.list_id)
                     & (AdminListAssignment.row_idx == AdminListContact.row_idx)).where(AdminListAssignment.owner_id == user.id)
    for l_, n_ in s.execute(aq.group_by(AdminListAssignment.list_id)):
        lstats.setdefault(l_, {})["assigned"] = n_
    for l_, n_ in s.execute(cq.group_by(AdminListContact.list_id)):
        lstats.setdefault(l_, {})["done"] = n_
    names = {u.id: u.name for u in s.scalars(select(User))}
    elsewhere = {}
    if cur is not None and not staff and rows:
        other = _assigned_elsewhere(s, exclude_list=cur.id)
        if other:
            for idx, r in rows:
                if idx in amap:
                    continue
                ps, _ = _row_keys(cur.headers or [], r, kinds)
                hit = next((other[p] for p in ps if p in other), None)
                if hit:
                    elsewhere[idx] = hit
    return render(request, "admin_lists.html", user, s, elsewhere=elsewhere, lists=lists, cur=cur, rows=rows, total=total, q=q, page=page,
                  sizes=sizes, kinds=kinds, cell=_cell, skip_first=skip_first, cmap=cmap, amap=amap, cstate=cstate,
                  counts=counts, LIST_STATUS=LIST_STATUS, base=base, can_manage=user.role in ("admin", "manager") and not staff,
                  staff_users=staff_users, names=names, owner=owner, staff_view=staff, lstats=lstats)


@app.get("/admin-lists", response_class=HTMLResponse)
def admin_lists(request: Request, lid: Optional[int] = None, q: str = "", page: int = 1, cstate: str = "", owner: str = "",
                user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    return _admin_lists_view(request, user, s, lid, q, page, cstate, "/admin-lists", owner)


@app.post("/admin-lists/{lid}/assign")
async def admin_lists_assign(lid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    """إسناد عملاء من القائمة لموظف: عدد (١٠/٥٠/١٠٠) من غير المسندين بالترتيب ضمن البحث/الفلتر الحالي، أو المحدد يدويًا.
    المسند لموظف آخر لا يُنقل إلا بتأكيد «نقل المسند لموظف آخر»."""
    from ..auth import audit
    from ..models import AdminList, AdminListAssignment, AdminListContact
    form = await request.form()
    al = s.get(AdminList, lid) or _404()
    owner_id = int(form.get("owner_id") or 0)
    u = s.get(User, owner_id) if owner_id else None
    ret = _safe_back(request, f"/admin-lists?lid={lid}")
    if not u or not u.active:
        flash(request, "اختر موظفًا نشطًا.", "err")
        return back(ret)
    reassign = bool(form.get("reassign"))
    amap = {x.row_idx: x for x in s.scalars(select(AdminListAssignment).where(AdminListAssignment.list_id == lid))}
    kinds = _col_kinds(al.headers or [], al.rows or [])
    other = _assigned_elsewhere(s, exclude_list=lid)  # رقم ← (الموظف، القائمة، التاريخ) في القوائم الأخرى
    names = {x.id: x.name for x in s.scalars(select(User))}

    def prior(i):
        """إسناد سابق لهذا العميل لموظف آخر: في هذه القائمة أو برقمه نفسه في قائمة أخرى."""
        x = amap.get(i)
        if x is not None and x.owner_id != u.id:
            return names.get(x.owner_id, "موظف"), x.assigned_at, None
        ps, _ = _row_keys(al.headers or [], al.rows[i], kinds)
        for p in ps:
            o = other.get(p)
            if o and o[0] != u.id:
                return names.get(o[0], "موظف"), o[2], o[1]
        return None
    picked = [int(x) for x in form.getlist("rows") if str(x).isdigit()]
    auto_skipped = []
    if not picked:
        n = int(form.get("count") or 0)
        if n not in (10, 50, 100):
            flash(request, "اختر العدد: ١٠ أو ٥٠ أو ١٠٠، أو حدّد عملاء من الجدول.", "err")
            return back(ret)
        q, cstate = (form.get("q") or "").strip().lower(), form.get("cstate") or ""
        cmap = {x.row_idx for x in s.scalars(select(AdminListContact).where(AdminListContact.list_id == lid))}
        for i, r in enumerate(al.rows or []):
            if len(picked) >= n:
                break
            if i in amap or (q and not any(q in str(c).lower() for c in r)):
                continue
            if cstate == "none" and i in cmap:
                continue
            if prior(i):  # مسند من قبل برقمه في قائمة أخرى ← يُتخطّى ويُذكر
                auto_skipped.append(i)
                continue
            picked.append(i)
    done, skipped = 0, []
    for i in picked:
        if not (0 <= i < len(al.rows or [])):
            continue
        pr = prior(i)
        if pr and not reassign:
            skipped.append((i, pr))
            continue
        x = amap.get(i)
        if x is None:
            s.add(AdminListAssignment(list_id=lid, row_idx=i, owner_id=u.id, assigned_by=user.id))
        else:
            x.owner_id, x.assigned_by, x.assigned_at = u.id, user.id, now_utc()
        done += 1
    skipped += [(i, prior(i)) for i in auto_skipped]
    audit(s, user, "assign_customers", u, source="admin_list", list=lid, count=done, already_assigned=len(skipped))
    name_j = next((j for j, k in kinds.items() if k in ("text", "long")), 0)
    lines = []
    for i, (who, at, lst) in skipped[:5]:
        cname = _cell(al.rows[i][name_j]) if name_j < len(al.rows[i]) else f"صف {i + 1}"
        lines.append(f"«{cname}» مسند من قبل إلى {who} منذ {fmt(at, False)}" + (f" (في قائمة «{lst}»)" if lst else ""))
    msg = f"أُسند {done} عميل إلى {u.name}." if done else "لم يُسند أي عميل."
    if skipped:
        msg += (f" هذا العميل مسند من قبل: " if len(skipped) == 1 else f" {len(skipped)} عميل مسندون من قبل لموظفين آخرين: ") + \
            "؛ ".join(lines) + ("…" if len(skipped) > 5 else "") + ". لنقلهم فعّل «نقل المسند لموظف آخر»."
    elif not done:
        msg = "لا يوجد عملاء غير مسندين مطابقون."
    flash(request, msg, "ok" if done and not skipped else "err")
    return back(ret)


def _assigned_elsewhere(s, exclude_list: int | None = None) -> dict:
    """رقم الهاتف ← (معرّف الموظف، اسم القائمة، تاريخ الإسناد) لكل عميل مسند في القوائم الإدارية."""
    from ..models import AdminList, AdminListAssignment
    out: dict = {}
    by_list: dict = {}
    for a in s.scalars(select(AdminListAssignment).where(AdminListAssignment.list_id != (exclude_list or 0))):
        by_list.setdefault(a.list_id, []).append(a)
    for lid_, items in by_list.items():
        al = s.get(AdminList, lid_)
        if al is None:
            continue
        kinds = _col_kinds(al.headers or [], al.rows or [])
        for a in items:
            if 0 <= a.row_idx < len(al.rows or []):
                ps, _ = _row_keys(al.headers or [], al.rows[a.row_idx], kinds)
                for p in ps:
                    out.setdefault(p, (a.owner_id, al.name, a.assigned_at))
    return out


@app.get("/portal/lists", response_class=HTMLResponse)
def portal_lists(request: Request, lid: Optional[int] = None, q: str = "", page: int = 1, cstate: str = "",
                 user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
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
                       confirm: str = Form(""), user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    return _list_contact(request, s, user, lid, idx, status, note, bool(confirm), "/admin-lists")


@app.post("/portal/lists/{lid}/row/{idx}/contact")
def portal_list_contact(lid: int, idx: int, request: Request, status: str = Form(""), note: str = Form(""),
                        confirm: str = Form(""), user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
    from ..models import AdminListAssignment
    if not s.scalar(select(AdminListAssignment.id).where(AdminListAssignment.list_id == lid, AdminListAssignment.row_idx == idx,
                                                          AdminListAssignment.owner_id == user.id)):
        raise HTTPException(403, "هذا العميل غير مسند إليك")
    return _list_contact(request, s, user, lid, idx, status, note, bool(confirm), "/portal/lists")


# ---------------------------------------------------------------- منع التكرار في القوائم الإدارية

def _row_keys(headers, row, kinds) -> tuple[set[str], str]:
    """(أرقام الصف بصيغة موحّدة، مفتاح الاسم الموحّد) — للمقارنة بين الملفات."""
    phones = set()
    for j, k in kinds.items():
        if k == "phone" and j < len(row) and _cell(row[j]):
            p = _phone_full(row[j])
            if p and p.startswith("+"):
                phones.add(p)
    name_j = next((j for j, h in enumerate(headers) if any(w in str(h) for w in ("اسم", "الشركة", "المنشأة", "المؤسسة", "name", "Name"))
                   and kinds.get(j) not in ("phone", "email", "url", "num", "date")), None)
    if name_j is None:
        name_j = next((j for j, k in kinds.items() if k in ("text", "long")), None)
    nm = normalize_name(_cell(row[name_j])) if name_j is not None and name_j < len(row) else ""
    return phones, nm


def _known_keys(s, exclude_list: int | None = None) -> tuple[set[str], set[str]]:
    """كل الأرقام والأسماء الموجودة: القوائم الإدارية، وعملاء المنصة، و«عملاء عشوائيون»."""
    from ..models import AdminList, Contact, ProspectPool
    from ..prospects import normalize_arabic_digits
    phones, names = set(), set()
    for al in s.scalars(select(AdminList).where(AdminList.id != (exclude_list or 0))):
        kinds = _col_kinds(al.headers or [], al.rows or [])
        for r in al.rows or []:
            ps, nm = _row_keys(al.headers or [], r, kinds)
            phones |= ps
            if nm and not ps:
                names.add(nm)
    phones |= {v for (v,) in s.execute(select(Contact.value).where(Contact.kind == "phone"))}
    for (row,) in s.execute(select(ProspectPool.row)):
        for p in str((row or {}).get("phone") or "").split(";"):
            if p.strip():
                pp = _phone_full(normalize_arabic_digits(p))
                if pp.startswith("+"):
                    phones.add(pp)
    return phones, names


def _dedupe_rows(head, body, known_phones, known_names):
    """يعيد (الصفوف الجديدة، مكرر داخل الملف، مكرر موجود سابقًا)."""
    kinds = _col_kinds(head, body)
    seen_p, seen_n, out = set(), set(), []
    inside = before = 0
    for r in body:
        ps, nm = _row_keys(head, r, kinds)
        if ps:
            if ps & known_phones:
                before += 1
                continue
            if ps & seen_p:
                inside += 1
                continue
            seen_p |= ps
        elif nm:
            if nm in known_names:
                before += 1
                continue
            if nm in seen_n:
                inside += 1
                continue
            seen_n.add(nm)
        else:
            key = "|".join(_cell(c) for c in r)
            if key in seen_n:
                inside += 1
                continue
            seen_n.add(key)
        out.append(r)
    return out, inside, before


@app.post("/admin-lists/dedupe")
def admin_lists_dedupe(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    """إزالة المكرر من القوائم الحالية: يبقى أول ظهور (الأقدم رفعًا)، ولا يُحذف صف مسند أو مسجّل عليه تواصل."""
    from ..auth import audit
    from ..models import AdminList, AdminListAssignment, AdminListContact
    seen_p, seen_n = set(), set()
    removed = 0
    for al in s.scalars(select(AdminList).order_by(AdminList.id)):
        kinds = _col_kinds(al.headers or [], al.rows or [])
        keep_idx = {x.row_idx for x in s.scalars(select(AdminListAssignment).where(AdminListAssignment.list_id == al.id))} | \
            {x.row_idx for x in s.scalars(select(AdminListContact).where(AdminListContact.list_id == al.id))}
        new_rows, remap = [], {}
        for i, r in enumerate(al.rows or []):
            ps, nm = _row_keys(al.headers or [], r, kinds)
            dup = bool(ps & seen_p) if ps else bool(nm and nm in seen_n)
            if dup and i not in keep_idx:
                removed += 1
                continue
            seen_p |= ps
            if nm and not ps:
                seen_n.add(nm)
            remap[i] = len(new_rows)
            new_rows.append(r)
        if len(new_rows) != len(al.rows or []):
            for model in (AdminListAssignment, AdminListContact):
                for x in s.scalars(select(model).where(model.list_id == al.id)):
                    x.row_idx = -1 - remap[x.row_idx]  # مؤقتًا لتجنّب تعارض القيد الفريد
            s.flush()
            for model in (AdminListAssignment, AdminListContact):
                for x in s.scalars(select(model).where(model.list_id == al.id)):
                    x.row_idx = -1 - x.row_idx
            al.rows = new_rows
    audit(s, user, "admin_list_dedupe", None, removed=removed)
    flash(request, f"أُزيل {removed} صف مكرر من القوائم الحالية (بقي أول ظهور لكل عميل، ولم يُمس أي عميل مسند أو مسجّل عليه تواصل)."
          if removed else "لا توجد صفوف مكررة في القوائم الحالية.")
    return back("/admin-lists")


@app.post("/admin-lists/upload")
async def admin_lists_upload(request: Request, file: UploadFile = File(...), note: str = Form(""),
                             user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
    known_p, known_n = _known_keys(s)
    added = inside = before = 0
    for i, rows in enumerate(sheets):
        head, body = _split_table(rows)
        if not body:
            continue
        body, d_in, d_before = _dedupe_rows(head, body, known_p, known_n)
        inside, before = inside + d_in, before + d_before
        if not body:
            continue
        kinds_ = _col_kinds(head, body)
        for r in body:  # الأوراق التالية في الملف نفسه تقارن بما أُضيف من الأوراق السابقة
            ps, nm = _row_keys(head, r, kinds_)
            known_p |= ps
            if nm and not ps:
                known_n.add(nm)
        added += len(body)
        al = AdminList(name=fname[:200], sheet=(names[i] if i < len(names) else "")[:100], headers=head, rows=body,
                       note=(note or "").strip()[:500], uploaded_by=user.id)
        s.add(al)
        s.flush()
        first = first or al.id
        made += 1
    audit(s, user, "import", user, kind="admin_list", file=fname[:100], sheets=made, added=added,
          dup_inside=inside, dup_existing=before)
    dup_msg = (f" وتُرك {inside + before} صف مكرر ({before} موجود سابقًا في القوائم أو عملاء المنصة، و{inside} مكرر داخل الملف)."
               if inside or before else " لا يوجد تكرار.")
    if not made:
        flash(request, f"«{fname}»: لم يُضف شيء — كل الصفوف مكررة أو الملف فارغ." + (dup_msg if inside or before else ""), "err")
        return back("/admin-lists")
    flash(request, f"«{fname}»: أُضيف {added} عميل جديد في {made} {'ورقة' if made == 1 else 'أوراق'}." + dup_msg)
    return back(f"/admin-lists?lid={first}")


@app.post("/admin-lists/{lid}/delete")
def admin_lists_delete(lid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from ..auth import audit
    from ..models import AdminList, AdminListAssignment, AdminListContact
    al = s.get(AdminList, lid)
    if al:
        n = len(al.rows or [])
        for model in (AdminListContact, AdminListAssignment):
            for x in s.scalars(select(model).where(model.list_id == lid)):
                s.delete(x)
        s.flush()
        s.delete(al)
        audit(s, user, "admin_list_delete", None, file=al.name[:100], rows=n)
        flash(request, f"حُذفت القائمة «{al.name}» ({n} صف) مع إسناداتها وحالات التواصل.")
    return back("/admin-lists")


@app.post("/admin-lists/{lid}/verify")
def admin_lists_verify(lid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def portal_lists_xlsx(lid: int, user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
    _require(user, "export")
    return admin_lists_xlsx(lid, user, s)


@app.get("/admin-lists/{lid}.xlsx")
def admin_lists_xlsx(lid: int, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
                 user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
                              user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
async def random_leads_verify(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def random_leads_xlsx(request: Request, q: str = "", status: str = "", user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
                   user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from .. import prospects as P
    if not re.fullmatch(r"\d{2}:\d{2}", time_ or ""):
        time_ = "05:30"
    P.set_auto(s, enabled=bool(enabled), time=time_, target=max(10, min(int(target), 5000)),
               google_budget_usd=max(0.0, min(float(google_budget_usd), 500.0)))
    from ..goal import set_goal
    set_goal(s, max(1, min(int(target), 5000)))
    cfg = P.get_auto(s)
    flash(request, (f"التشغيل اليومي مفعّل الساعة {cfg['time']} بتوقيت الرياض — الهدف {cfg['target']} منشأة، "
                    f"وميزانية خرائط Google ${cfg['google_budget_usd']:.2f} يوميًا.") if cfg["enabled"] else "التشغيل اليومي متوقف.")
    return back("/prospects")


@app.post("/prospects/stop")
def prospects_stop(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from ..models import ProspectRun
    from ..auth import audit
    for r in s.scalars(select(ProspectRun).where(ProspectRun.status.in_(["pending", "running"]))):
        r.status = "stopped"
        audit(s, user, "prospects_stop", None, batch=r.id)
    flash(request, "أُوقفت الدفعة. ما جُمع يبقى في القائمة.")
    return back("/prospects")


@app.post("/companies/{cid}/enrich")
def company_enrich_now(cid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def targets(request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    camps = list(s.scalars(select(Campaign).where(func.coalesce(Campaign.kind, "") != "social")
                           .order_by(Campaign.id.desc())))
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
                  user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    parsed = parse_request(text, daily_target)
    offers = list(s.scalars(select(Offer).order_by(Offer.id)))
    return render(request, "target_review.html", user, s, parsed=parsed, text=text, offers=offers, camp=None,
                  **_social_ctx(s, None, user))


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
    if "target_place" in form:
        camp.target_place = (form.get("target_place") or "").strip()[:200] or None
    if form.get("social_present"):
        from ..campaign_sources import SOCIAL_SOURCES
        picked = form.getlist("social_sources") if hasattr(form, "getlist") else []
        camp.social_sources = [k for k in SOCIAL_SOURCES if k in picked]


@app.post("/targets/save")
async def targets_save(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def target_edit(cid: int, request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
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
                  camp=camp, runs=runs, **_social_ctx(s, camp, user, request.query_params.get("src", "")))


def _social_ctx(s, camp, user, src: str = "") -> dict:
    """قسم «مواقع التواصل الاجتماعي»: ملفات كل مصدر ومعاينتها، وعملاء الحملة مع فلتر «مصدر العميل»."""
    from ..campaign_sources import SOCIAL_SOURCES, campaign_leads, mark_counts, marks_for, view
    from ..models import CampaignSourceFile
    can = user.role in ("admin", "manager")
    files: dict[str, list] = {}
    if camp is not None and can:
        for f in s.scalars(select(CampaignSourceFile).where(CampaignSourceFile.campaign_id == camp.id)
                           .order_by(CampaignSourceFile.id)):
            files.setdefault(f.source_key, []).append({"f": f, "v": view(f, limit=200, marks=marks_for(s, f.id)),
                                                       "mc": mark_counts(s, f.id)})
    leads = campaign_leads(s, camp.id, src if src in SOCIAL_SOURCES else "") if camp is not None and can else []
    all_n = len(campaign_leads(s, camp.id)) if camp is not None and can else 0
    owners = {u.id: u.name for u in s.scalars(select(User))} if leads else {}
    return {"SOCIAL": SOCIAL_SOURCES, "social_files": files, "lead_owners": owners, "social_can": can, "leads": leads, "leads_all": all_n,
            "lead_src": src if src in SOCIAL_SOURCES else "", "place_options": list(CITIES),
            "staged_n": sum(1 for v in files.values() for x in v if x["f"].status == "staged")}


def _social_home(s) -> Campaign:
    """الحاوية المستقلة لصفحة «مواقع التواصل الاجتماعي» (لا تظهر في الحملات ولا تعمل يوميًا)."""
    camp = s.scalar(select(Campaign).where(Campaign.kind == "social").order_by(Campaign.id).limit(1))
    if camp is None:
        camp = Campaign(name="مواقع التواصل الاجتماعي", kind="social", status="draft", daily_target=0)
        s.add(camp)
        s.flush()
    return camp


@app.get("/social", response_class=HTMLResponse)
def social_page(request: Request, c: int = 0, src: str = "", user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    home = _social_home(s)
    camp = s.get(Campaign, c) if c else home
    camp = camp or home
    camps = list(s.scalars(select(Campaign).where(func.coalesce(Campaign.kind, "") != "social")
                           .order_by(Campaign.id.desc())))
    return render(request, "social.html", user, s, camp=camp, home=home, camps=camps, **_social_ctx(s, camp, user, src))


@app.post("/social/settings")
async def social_settings(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from ..campaign_sources import SOCIAL_SOURCES
    form = await request.form()
    camp = s.get(Campaign, int(form.get("cid") or 0)) or _social_home(s)
    camp.target_place = (form.get("target_place") or "").strip()[:200] or None
    picked = form.getlist("social_sources")
    camp.social_sources = [k for k in SOCIAL_SOURCES if k in picked]
    flash(request, "حُفظ المكان المستهدف والمصادر المختارة")
    return back(f"/social?c={camp.id}")


@app.post("/targets/{cid}/social/upload")
async def social_upload(cid: int, request: Request, source: str = Form(...), file: UploadFile = File(...),
                        replace_id: int = Form(0), target_place: str = Form(""),
                        user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from ..campaign_sources import SOCIAL_SOURCES, parse_file
    from ..models import CampaignSourceFile
    from ..prospects import UploadError
    camp = s.get(Campaign, cid) or _404()
    if source not in SOCIAL_SOURCES:
        raise HTTPException(400)
    data = await file.read()
    name = (file.filename or "ملف").strip()[:200]
    if not data:
        flash(request, "اختر ملف Excel أولًا", "err")
        return back(f"/social?c={cid}#src-{source}")
    try:
        headers, rows, cols = parse_file(name, data)
    except UploadError as e:
        flash(request, f"تعذّر قراءة «{name}»: {e}", "err")
        return back(f"/social?c={cid}#src-{source}")
    if target_place.strip():
        camp.target_place = target_place.strip()[:200]
    picked = list(camp.social_sources or [])
    if source not in picked:
        camp.social_sources = [k for k in SOCIAL_SOURCES if k in picked + [source]]
    old = s.get(CampaignSourceFile, replace_id) if replace_id else None
    if old is not None and old.campaign_id == cid and old.status == "staged":
        from ..models import CampaignSourceMark
        for m in s.scalars(select(CampaignSourceMark).where(CampaignSourceMark.file_id == old.id)):
            s.delete(m)
        s.flush()
        s.delete(old)
    s.add(CampaignSourceFile(campaign_id=cid, source_key=source, place=camp.target_place or "", file_name=name,
                             headers=headers, rows=rows, cols=cols, uploaded_by=user.id))
    flash(request, f"رُفع «{name}» أمام {SOCIAL_SOURCES[source]}: {len(rows)} عميل للمعاينة. راجع الأسماء والأرقام "
                   "ثم اضغط «اعتماد واستيراد العملاء».")
    return back(f"/social?c={cid}&open={source}#src-{source}")


@app.post("/targets/{cid}/social/{fid}/delete")
def social_delete(cid: int, fid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from ..models import CampaignSourceFile
    f = s.get(CampaignSourceFile, fid)
    if f is None or f.campaign_id != cid:
        _404()
    from ..models import CampaignLead, CampaignSourceMark
    for m in s.scalars(select(CampaignSourceMark).where(CampaignSourceMark.file_id == f.id)):
        s.delete(m)
    removed = 0
    for lead in s.scalars(select(CampaignLead).where(CampaignLead.file_id == f.id)):
        s.delete(lead)
        removed += 1
    s.flush()
    name, key = f.file_name, f.source_key
    s.delete(f)
    flash(request, f"حُذف «{name}»" + (f" وأُزيل ربط {removed} عميل بمصدره في هذه الصفحة (سجلات العملاء وتواصلهم باقية في المنصة)"
                                      if removed else ""))
    return back(f"/social?c={cid}#src-{key}")


@app.post("/targets/{cid}/social/import")
def social_import(cid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from ..campaign_sources import SOCIAL_SOURCES, import_campaign
    camp = s.get(Campaign, cid) or _404()
    done = import_campaign(s, camp, user.id)
    if not done:
        flash(request, "لا توجد ملفات بانتظار الاستيراد", "err")
    else:
        flash(request, "استُورد العملاء: " + " — ".join(
            f"{SOCIAL_SOURCES.get(f.source_key, f.source_key)} «{f.file_name}»: {st['new']} جديد، {st['duplicate']} مكرر، "
            f"{st['failed']} فشل" for f, st in done))
    return back(f"/social?c={cid}#social")


@app.post("/targets/{cid}/social/{fid}/mark")
def social_mark(cid: int, fid: int, request: Request, row: int = Form(...), field: str = Form(...), value: str = Form(""),
                user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    """«تم التواصل؟» و«مهتم؟» أمام كل عميل في الملف."""
    from ..campaign_sources import CONTACT_CHOICES, INTEREST_CHOICES
    from ..models import CampaignSourceFile, CampaignSourceMark
    f = s.get(CampaignSourceFile, fid)
    if f is None or f.campaign_id != cid or not (0 <= row < len(f.rows or [])):
        _404()
    choices = {"contacted": CONTACT_CHOICES, "interest": INTEREST_CHOICES}.get(field)
    if choices is None or (value and value not in choices):
        raise HTTPException(400)
    m = s.scalar(select(CampaignSourceMark).where(CampaignSourceMark.file_id == fid, CampaignSourceMark.row_idx == row))
    if m is None:
        m = CampaignSourceMark(file_id=fid, row_idx=row)
        s.add(m)
    setattr(m, field, value or None)
    if field == "interest" and value and not m.contacted:
        m.contacted = "yes"  # من عُرف اهتمامه فقد تُوصل معه
    m.by_id, m.by_name, m.at = user.id, user.name, now_utc()
    s.flush()
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True, "contacted": m.contacted or "", "interest": m.interest or "", "by": user.name})
    return back(f"/social?c={cid}&open={f.source_key}#src-{f.source_key}")


@app.get("/targets/{cid}/social/{fid}", response_class=HTMLResponse)
def social_file(cid: int, fid: int, request: Request, page: int = 1, user: User = Depends(require_manager),
                s=Depends(get_db, scope="function")):
    from ..campaign_sources import SOCIAL_SOURCES, view
    from ..models import CampaignSourceFile
    camp = s.get(Campaign, cid) or _404()
    f = s.get(CampaignSourceFile, fid)
    if f is None or f.campaign_id != cid:
        _404()
    per = 200
    page = max(1, page)
    from ..campaign_sources import marks_for
    v = view(f, limit=per, offset=(page - 1) * per, marks=marks_for(s, f.id))
    return render(request, "social_file.html", user, s, camp=camp, f=f, v=v, page=page,
                  pages=max(1, -(-v["total"] // per)), SOCIAL=SOCIAL_SOURCES)


@app.post("/targets/{cid}")
async def target_update(cid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    camp = s.get(Campaign, cid) or _404()
    form = await request.form()
    _save_campaign_fields(camp, form)
    _save_categories(s, camp, form)
    flash(request, "حُفظت التعديلات")
    return back(f"/targets/{cid}")


@app.post("/targets/{cid}/status")
def target_status(cid: int, request: Request, action: str = Form(...), user: User = Depends(require_manager),
                  s=Depends(get_db, scope="function")):
    camp = s.get(Campaign, cid) or _404()
    if action == "activate":
        missing = [c.label for c in camp.categories if c.active and c.offer_id and not c.offer.approved] \
            if settings.auto_outreach_enabled else []
        if missing:
            flash(request, "اعتمد العروض المرتبطة أولًا: " + "، ".join(missing), "err")
            return back(f"/targets/{cid}")
        camp.status, camp.approved_at = "active", now_utc()
        from ..campaign_sources import import_campaign
        done = import_campaign(s, camp, user.id)
        extra = (" واستُورد عملاء الملفات: " + "، ".join(f"«{f.file_name}» {st['new']} جديد/{st['duplicate']} مكرر/"
                                                       f"{st['failed']} فشل" for f, st in done)) if done else ""
        flash(request, "اعتُمدت الحملة وأصبحت نشطة. ستعمل يوميًا في الوقت المحدد." + extra)
    elif action == "pause":
        camp.status = "paused"
        flash(request, "أُوقفت الحملة مؤقتًا")
    return back(f"/targets/{cid}")


@app.post("/targets/{cid}/run")
def target_run(cid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def prospecting_page(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
async def prospecting_save(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def prospecting_run(request: Request, batch: int = Form(0), user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
    if found == "today":  # عملاء جدد اليوم = تعريف الهدف الموحّد (جوال سعودي + بريد برابط إثبات، بلا عملاء حاليين ولا تجريبي)
        from ..goal import counted_filter
        qq = qq.where(*counted_filter(riyadh_today()))
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
              owner: str = "", contact: str = "", found: str = "", user: User = Depends(current_user), s=Depends(get_db, scope="function")):
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
def company_contact_start(cid: int, request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    c = s.get(Company, cid) or _404()
    return _contact_start(request, s, user, c, _safe_back(request, f"/companies/{cid}"))


@app.post("/companies/{cid}/contact/update")
async def company_contact_update(cid: int, request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    c = s.get(Company, cid) or _404()
    form = await request.form()
    owners = {u.id for u in s.scalars(select(User).where(User.active.is_(True)))}
    return _contact_update(request, s, user, c, form, _safe_back(request, f"/companies/{cid}"), owners)


# ---------------------------------------------------------------- اكتشاف عملاء جدد (أدلة النقل والمقاولين والمصانع)

DISCOVER_DEFAULT = ["transport.freight", "transport.parcels", "transport.cold_chain", "construction.contractors",
                    "industry.food_factories", "industry.water_factories", "industry.building_material_factories"]


@app.get("/discover", response_class=HTMLResponse)
def discover_page(request: Request, run: Optional[int] = None, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
                  today_rows=today_rows, auto=auto, google_key=bool(settings.google_places_api_key),
                  google_status=__import__("mandoob.google_live", fromlist=["x"]).get_status(s),
                  today_new_complete=sum(x["complete"] for x in today_rows if x["key"] != "prospecting"),
                  busy=busy, ACTIVITIES=ACTIVITIES, DIR_SECTORS=SECTORS, GEO_REGIONS=geo.REGIONS,
                  defaults=DISCOVER_DEFAULT, KIND={"works": ("ok", "يعمل"), "needs_key": ("warn", "يحتاج ربطًا (مفتاح)"),
                                                   "needs_file": ("warn", "ينتظر ملفًا"),
                                                   "blocked": ("err", "غير متاح آليًا من الخادم")})


@app.post("/discover/auto")
def discover_auto(request: Request, enabled: str = Form(""), time: str = Form("05:30"), target: int = Form(400),
                  budget: float = Form(20), user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from .. import directory as D
    from ..auth import audit
    D.set_auto(s, enabled=bool(enabled), time=(time or "05:30")[:5], target_complete=max(1, min(target, 5000)),
               budget_usd=max(1.0, min(budget, 2000.0)))
    from ..goal import set_goal
    set_goal(s, max(1, min(target, 5000)), cost_cap_usd=max(1.0, min(budget, 2000.0)))  # الهدف وحد التكلفة الموحّدان
    v = D.get_auto(s)
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
async def discover_run(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
                          user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
async def company_similar(cid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def company_reasons_find(cid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
                        s=Depends(get_db, scope="function")):
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
                   contact: str = "", found: str = "", user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    from ..auth import audit
    from ..xlsx import CONTACT_HEADERS, response, workbook
    qq = _companies_query(user, status, q, city, sector, source, operator, signals, kind, owner, contact, found)
    rows = [_customer_row(s, c) for c in s.scalars(qq.order_by(Company.first_found_at.desc()))]
    audit(s, user, "export_download", user, kind="companies", rows=len(rows))
    return response(workbook([("الشركات", CUSTOMER_HEADERS + CONTACT_HEADERS, rows)],
                             {"الشركات": [["الشركات المسندة وغير المسندة حسب الفلاتر", f"{len(rows)} سجل"]]}),
                    "companies.xlsx")


@app.get("/followups.xlsx")
def followups_xlsx(request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    from ..xlsx import CONTACT_HEADERS, response, workbook
    items = needs_followup_today(s, user)
    rows = [[it["task"].title, fmt(it["task"].due_at), "متأخرة" if it["overdue"] else "اليوم", it["owner"]]
            + _customer_row(s, it["company"]) for it in items]
    return response(workbook([("تحتاج متابعة", ["المهمة", "موعدها", "الحالة", "مسؤول المهمة"] + CUSTOMER_HEADERS
                               + CONTACT_HEADERS, rows)]), "followups.xlsx")


def _aic_args(period: str, d_from: str, d_to: str):
    def _d(v):
        try:
            return date.fromisoformat(v) if v else None
        except ValueError:
            return None
    return (period or "today"), _d(d_from), _d(d_to)


# ---------------------------------------------------------------- بريد العملاء

@app.get("/customer-mail", response_class=HTMLResponse)
def customer_mail_page(request: Request, m: int = 0, q: str = "", company: int = 0, to: str = "", new: int = 0,
                       user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from .. import customer_mail as CM
    from ..models import Contact, CustomerMail, CustomerMailFile
    cfg = CM.get_cfg(s)
    thread, files = [], {}
    if m:
        root = s.get(CustomerMail, m) or _404()
        thread = list(s.scalars(select(CustomerMail).where(CustomerMail.thread_key == root.thread_key).order_by(CustomerMail.at)))
        for f in s.scalars(select(CustomerMailFile).where(CustomerMailFile.mail_id.in_([x.id for x in thread]))):
            files.setdefault(f.mail_id, []).append(f)
        for x in thread:
            if x.direction == "in" and not x.read:
                x.read = True
    co = s.get(Company, company) if company else None
    pre_to = to
    if co and not pre_to:
        em = s.scalar(select(Contact.value).where(Contact.company_id == co.id, Contact.kind == "email").order_by(Contact.preferred.desc()))
        pre_to = em or ""
    senders = {u.id: u.name for u in s.scalars(select(User))}
    return render(request, "customer_mail.html", user, s, cfg=cfg, configured=CM.configured(cfg), threads=CM.threads(s, q),
                  thread=thread, files=files, q=q, co=co, pre_to=pre_to, senders=senders, compose=bool(new or co or to or not m),
                  max_mb=CM.MAX_TOTAL // 1048576)


@app.post("/customer-mail/connect")
def customer_mail_connect(request: Request, address: str = Form(...), name: str = Form(""), password: str = Form(...),
                          user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from .. import customer_mail as CM
    from ..auth import audit
    addr = (normalize_email(address) or "").lower()
    if not addr:
        flash(request, "اكتب عنوان بريد صحيح", "err")
        return back("/customer-mail")
    ok, why = CM.save_connection(s, addr, name.strip(), password, user)
    if not ok:
        flash(request, f"لم يُحفظ شيء: {why}.", "err")
        return back("/customer-mail")
    audit(s, user, "mail_secret", user, provider="customer_mail")
    flash(request, f"رُبط {addr} وحُفظت كلمة السر مشفّرة." + (f" تنبيه: {why}." if why else " الإرسال وقراءة الردود يعملان."),
          "err" if why else "ok")
    return back("/customer-mail")


@app.post("/customer-mail/settings")
def customer_mail_settings(request: Request, name: str = Form(""), signature: str = Form(""),
                           user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from .. import customer_mail as CM
    cfg = CM.get_cfg(s)
    cfg.update(name=name.strip()[:120] or cfg.get("name") or "سيارة آب", signature=signature.strip()[:4000])
    CM.put_cfg(s, cfg)
    flash(request, "حُفظ اسم المرسل والتوقيع")
    return back("/customer-mail")


@app.post("/customer-mail/send")
async def customer_mail_send(request: Request, to: str = Form(""), cc: str = Form(""), subject: str = Form(""),
                             body: str = Form(""), company_id: int = Form(0), reply_to_id: int = Form(0),
                             files: list[UploadFile] = File(default=[]),
                             user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from .. import customer_mail as CM
    blobs = []
    for f in files or []:
        if f and f.filename:
            data = await f.read()
            if data:
                blobs.append((f.filename, data, f.content_type or ""))
    try:
        row = CM.send(s, user, to, cc, subject, body, blobs, company_id or None, reply_to_id or None)
    except ValueError as e:
        flash(request, str(e), "err")
        return back(f"/customer-mail?m={reply_to_id}" if reply_to_id else "/customer-mail?new=1")
    if row.status == "sent":
        flash(request, f"أُرسل البريد إلى {row.to_addr}" + (f" مع {len(blobs)} مرفق" if blobs else ""))
    else:
        flash(request, row.detail or "تعذّر الإرسال", "err")
    return back(f"/customer-mail?m={row.id}")


@app.post("/customer-mail/sync")
def customer_mail_sync(request: Request, user: User = Depends(require_manager)):
    from .. import customer_mail as CM
    r = CM.sync()
    if r.get("skipped"):
        flash(request, "قراءة الردود غير مفعّلة: اربط البريد أولًا (ويلزم تفعيل IMAP في Zoho)", "err")
    elif "error" in r:
        flash(request, f"تعذّرت قراءة الردود ({r['error']})", "err")
    else:
        flash(request, f"تمت المزامنة: {r.get('new', 0)} رسالة جديدة")
    return back(request.headers.get("referer") or "/customer-mail")


@app.get("/customer-mail/attachment/{fid}")
def customer_mail_attachment(fid: int, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from fastapi import Response
    from urllib.parse import quote
    from ..models import CustomerMailFile
    f = s.get(CustomerMailFile, fid) or _404()
    return Response(f.data or b"", media_type=f.mime or "application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f.name)}", "Cache-Control": "no-store"})


@app.get("/ai-costs", response_class=HTMLResponse)
def ai_costs_page(request: Request, period: str = "today", d_from: str = "", d_to: str = "",
                  user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from .. import ai_costs as A
    k, a, b = _aic_args(period, d_from, d_to)
    return render(request, "ai_costs.html", user, s, r=A.report(s, k, a, b), PERIODS=A.PERIODS)


@app.get("/ai-costs.xlsx")
def ai_costs_xlsx(request: Request, period: str = "today", d_from: str = "", d_to: str = "",
                  user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from .. import ai_costs as A
    from ..xlsx import response, workbook
    k, a, b = _aic_args(period, d_from, d_to)
    r = A.report(s, k, a, b)
    return response(workbook([("تكلفة الذكاء الاصطناعي", ["البند", "القيمة", "", ""], A.rows_for_export(r))]),
                    f"ai-costs-{r['from']}-{r['to']}.xlsx")


@app.get("/daily-report.xlsx")
def daily_report_xlsx(request: Request, day: str = "", user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def weekly_report_xlsx(request: Request, day: str = "", user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def leads_xlsx(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
async def companies_bulk_assign(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def company_detail(cid: int, request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
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
                   user: User = Depends(current_user), s=Depends(get_db, scope="function")):
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
def message_view(mid: int, request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    m = s.get(OutboundMessage, mid) or _404()
    if not _company_visible(user, m.company):
        raise HTTPException(403)
    return render(request, "message.html", user, s, m=m)


@app.get("/messages/{mid}/attachment")
def message_attachment(mid: int, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
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
def offers_report_page(request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    f = _filters(request)
    rep = offers_report(s, user, f)
    return render(request, "offers_report.html", user, s, rep=rep, f=f, qs=str(request.query_params),
                  camps=list(s.scalars(select(Campaign).where(func.coalesce(Campaign.kind, "") != "social"))), offers=list(s.scalars(select(Offer))),
                  reps=list(s.scalars(select(User).where(User.role == "rep"))))


@app.get("/offers-report.xlsx")
def offers_report_xlsx(request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    rep = offers_report(s, user, _filters(request))
    data = export_offers_xlsx(rep)
    fname = f"offers-report-{riyadh_today().isoformat()}.xlsx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.get("/followups", response_class=HTMLResponse)
def followups(request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    return render(request, "followups.html", user, s, items=needs_followup_today(s, user))


@app.post("/tasks/{tid}/done")
def task_done(tid: int, request: Request, note: str = Form(""), user: User = Depends(current_user), s=Depends(get_db, scope="function")):
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
def offers_page(request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    return render(request, "offers.html", user, s, offers=list(s.scalars(select(Offer).order_by(Offer.id))),
                  attachments=list(s.scalars(select(Attachment).order_by(Attachment.id.desc()))))


@app.post("/offers")
async def offers_save(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def offer_approve(oid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    o = s.get(Offer, oid) or _404()
    o.approved, o.approved_by, o.approved_at = True, user.id, now_utc()
    flash(request, f"اعتُمد العرض «{o.service_name}»")
    return back("/offers")


@app.get("/offers/{oid}/preview", response_class=HTMLResponse)
def offer_preview(oid: int, request: Request, company_id: Optional[int] = None, user: User = Depends(current_user),
                  s=Depends(get_db, scope="function")):
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
def mail_page(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    accounts = list(s.scalars(select(MailAccount).order_by(MailAccount.id)))
    status = {"microsoft365": bool(settings.ms_tenant_id and settings.ms_client_id and settings.ms_client_secret),
              "google": bool(settings.google_client_id and settings.google_refresh_token)}
    return render(request, "mail.html", user, s, accounts=accounts, status=status)


@app.post("/mail")
def mail_save(request: Request, provider: str = Form(...), sender_address: str = Form(...),
              sender_name: str = Form("سيارة آب"), signature_html: str = Form(""), reply_mailbox: str = Form(""),
              user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    for a in s.scalars(select(MailAccount)):
        a.active = False
    s.add(MailAccount(provider=provider, sender_address=sender_address.strip().lower(), sender_name=sender_name,
                      signature_html=signature_html, reply_mailbox=reply_mailbox.strip().lower(), active=True))
    flash(request, "رُبط بريد الشركة. أرسل رسالة تجربة للتأكد.")
    return back("/mail")


@app.post("/mail/smtp")
def mail_smtp(request: Request, password: str = Form(...), smtp_user: str = Form(""), smtp_host: str = Form(""),
              user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    """كلمة سر بريد SMTP (مثل Zoho) من الصفحة: تُختبر بالدخول دون إرسال، ثم تُحفظ مشفّرة ولا تُعرض أبدًا."""
    from ..auth import audit
    from ..mail.smtp import check_login
    from ..outreach import active_account
    from ..secretbox import seal
    acct = active_account(s)
    if acct is None or acct.provider != "smtp":
        flash(request, "اربط أولًا بريد SMTP (مثل Zoho) من «ربط / تغيير البريد».", "err")
        return back("/mail")
    login = (normalize_email(smtp_user) or acct.sender_address).strip().lower()
    host, why = check_login(login, password, smtp_host.strip().lower() or None)
    if not host:
        flash(request, f"لم يُحفظ شيء: {why}.", "err")
        return back("/mail")
    acct.smtp_host, acct.smtp_port, acct.smtp_user, acct.smtp_password_enc = host, 465, login, seal(password)
    audit(s, user, "mail_secret", user, provider="smtp", host=host)
    flash(request, f"نجح الاتصال بخادم البريد ({host}) وحُفظت كلمة السر مشفّرة. أرسل الآن «رسالة تجربة» للتأكيد النهائي.")
    return back("/mail")


@app.post("/mail/test")
def mail_test(request: Request, to: str = Form(""), message: str = Form(""), user: User = Depends(require_manager),
              s=Depends(get_db, scope="function")):
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
        flash(request, ("اكتب كلمة سر البريد في خانة «كلمة سر البريد» بهذه الصفحة واضغط «حفظ واختبار الاتصال»، ثم أعد التجربة."
                        if acct.provider == "smtp" else
                        f"مفاتيح المزود غير مضبوطة على الخادم بعد ({need}). أضفها في مجموعة mandoob-secrets ثم أعد المحاولة."),
              "err")
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
def sandbox_page(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    msgs = list(s.scalars(select(OutboundMessage).join(MailAccount, MailAccount.id == OutboundMessage.mail_account_id)
                          .where(MailAccount.provider == "sandbox", OutboundMessage.status.in_(["sent", "delivered", "bounced"]))
                          .order_by(OutboundMessage.id.desc()).limit(50)))
    return render(request, "sandbox.html", user, s, msgs=msgs)


@app.post("/sandbox/reply")
def sandbox_reply(request: Request, message_id: int = Form(...), text: str = Form(...),
                  user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from ..mail.sandbox import SandboxProvider
    m = s.get(OutboundMessage, message_id) or _404()
    SandboxProvider.inject_reply(from_addr=m.to_address, original_message_id=m.internet_message_id,
                                 thread_id=m.thread_id, subject=m.subject, text=text)
    jobs.inbox_tick()
    flash(request, "وصل الرد وعولج. افتح سجل الشركة لرؤية النتيجة.")
    return back(f"/companies/{m.company_id}")


# ---------------------------------------------------------------- الفريق

@app.get("/team", response_class=HTMLResponse)
def team(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
async def team_save(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def settings_page(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
                           s=Depends(get_db, scope="function")):
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
def settings_seed_test(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def toggle_demo_view(request: Request, on: str = Form(""), user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from ..auth import audit
    from ..training_demo import _set_demo_view
    _set_demo_view(s, bool(on))
    audit(s, user, "demo_view", None, on=bool(on))
    flash(request, "وضع العرض التدريبي مفعّل: تظهر البيانات التجريبية في الإحصائيات والتقارير." if on else
          "أُطفئ وضع العرض التدريبي: الإحصائيات للبيانات الحقيقية فقط.")
    return back(request.headers.get("referer") or "/")


@app.post("/settings/training")
def settings_training(request: Request, action: str = Form("create"), user: User = Depends(require_manager),
                      s=Depends(get_db, scope="function")):
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
def add_suppression(request: Request, value: str = Form(...), user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    v = value.strip().lower()
    kind = "email" if "@" in v else ("phone" if normalize_phone(v) else "domain")
    v = normalize_phone(v) if kind == "phone" else v
    s.add(Suppression(kind=kind, value=v, channel="all", reason="أضيف يدويًا", source="manual"))
    flash(request, "أضيف إلى قائمة إيقاف التواصل")
    return back("/settings")


# ---------------------------------------------------------------- سجل التشغيل والتقارير

@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    from ..directory import SOURCES as DSRC
    from ..health import classify, search_status
    from ..models import DirectoryRun
    rows = list(s.scalars(select(DiscoveryRun).order_by(DiscoveryRun.id.desc()).limit(30)))
    drows = list(s.scalars(select(DirectoryRun).order_by(DirectoryRun.id.desc()).limit(15)))
    return render(request, "runs.html", user, s, rows=rows, drows=drows, DSRC=DSRC, classify=classify,
                  health=search_status(s))


@app.get("/daily-report", response_class=HTMLResponse)
def daily_report_page(request: Request, day: str = "", user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    d = date.fromisoformat(day) if day else riyadh_today()
    from ..directory import daily_by_source
    from ..goal import achieved as _ach, shortfall_reason as _why, target as _goal
    g_n, g_t = _ach(s, d), _goal(s)
    return render(request, "daily_report.html", user, s, r=daily_report(s, d), by_src=daily_by_source(s, d),
                  goal_n=g_n, goal_t=g_t, goal_why=_why(s, d) if g_n < g_t else "")


@app.get("/notifications", response_class=HTMLResponse)
def notifications(request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
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
def auto_search_page(request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    camps = list(s.scalars(select(Campaign).where(Campaign.name.like("البحث الآلي%")).order_by(Campaign.id.desc())))
    runs = {c.id: s.scalar(select(DiscoveryRun).where(DiscoveryRun.campaign_id == c.id)
                           .order_by(DiscoveryRun.id.desc())) for c in camps}
    return render(request, "auto_search.html", user, s, camps=camps, runs=runs, example=AUTO_EXAMPLE,
                  ai_ready=settings.ai_enabled)


@app.post("/auto-search")
def auto_search_create(request: Request, text: str = Form(...), daily_target: int = Form(400),
                       user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
                    s=Depends(get_db, scope="function")):
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
               user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    """استعراض العملاء حسب تاريخ الجمع (يوم أو فترة) — قراءة فقط: لا بحث جديد ولا تكلفة إضافية."""
    from ..reports import customers_in_period
    mode = "range" if mode == "range" else "day"
    a, b, err = _period(mode, day, date_from, date_to)
    t = customers_in_period(s, user, a, b, campaign) if not err else None
    return render(request, "today.html", user, s, t=t, err=err, mode=mode, a=a, b=b, campaign=campaign,
                  csum=_csum(s, [r["c"] for r in t["rows"]]) if t else {},
                  qs=str(request.query_params), camps=list(s.scalars(select(Campaign).where(
                      func.coalesce(Campaign.kind, "") != "social").order_by(Campaign.id.desc()))))


@app.get("/today.xlsx")
def today_xlsx(request: Request, mode: str = "day", day: str = "", date_from: str = Query("", alias="from"),
               date_to: str = Query("", alias="to"), campaign: Optional[int] = None,
               user: User = Depends(current_user), s=Depends(get_db, scope="function")):
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
def request_quote_page(request: Request, s=Depends(get_db, scope="function")):
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
                         website_url: str = Form(""), s=Depends(get_db, scope="function")):
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
def leads_page(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from ..leads import source_label
    from ..models import InboundLead
    rows = list(s.scalars(select(InboundLead).order_by(InboundLead.id.desc()).limit(200)))
    companies_ = {c.id: c for c in s.scalars(select(Company).where(Company.id.in_([r.company_id for r in rows if r.company_id])))}
    base = settings.public_base_url.rstrip("/")
    return render(request, "leads.html", user, s, rows=rows, companies=companies_, source_label=source_label,
                  link=f"{base}/request-quote")


@app.get("/weekly-report", response_class=HTMLResponse)
def weekly_report_page(request: Request, day: str = "", user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from ..reports import weekly_by_source, weekly_segments
    d = date.fromisoformat(day) if day else riyadh_today()
    w = weekly_segments(s, d)
    return render(request, "weekly_report.html", user, s, w=w, by_source=weekly_by_source(s, d), prev=(w["start"] - timedelta(days=7)).isoformat(),
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
def staff_page(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
async def staff_add(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def staff_edit(uid: int, request: Request, q: str = "", user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
async def staff_update(uid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
async def staff_assign(uid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
                     user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def staff_activate(uid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from ..auth import audit
    u = s.get(User, uid) or _404()
    u.active = True
    audit(s, user, "activate", u)
    flash(request, f"أُعيد تفعيل {u.name}")
    return back(f"/staff/{uid}")


@app.post("/staff/{uid}/invite")
def staff_invite(uid: int, request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
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
def portal_home(request: Request, q: str = "", page: int = 1, user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
    from ..permissions import can, describe
    page = max(1, page)
    all_rows = _portal_rows(s, user, q)
    total = len(all_rows)
    rows = all_rows[(page - 1) * 50: page * 50]  # يُرسل للجهاز ما تعرضه الصفحة فقط
    return render(request, "portal_home.html", user, s, rows=rows, q=q, can=lambda p: can(user, p), page=page, total=total,
                  d=describe(user), mask=lambda t: mask_text(user, t), csum=_csum(s, [r["c"] for r in rows]))


@app.get("/portal/export.xlsx")
def portal_export(request: Request, q: str = "", user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
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
def portal_company(cid: int, request: Request, user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
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
def portal_note(cid: int, request: Request, note: str = Form(...), user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
    _require(user, "add_note")
    c = _portal_company(s, user, cid)
    c.last_action, c.last_action_at = note[:300], now_utc()
    log_activity(s, cid, "note", f"ملاحظة من {user.name}", note, user_id=user.id)
    flash(request, "أُضيفت الملاحظة")
    return back(f"/portal/companies/{cid}")


@app.post("/portal/companies/{cid}/status")
def portal_status(cid: int, request: Request, stage: str = Form(""), next_title: str = Form(""), next_at: str = Form(""),
                  done_task: Optional[int] = Form(None), offer_at: str = Form(""), offer_note: str = Form(""),
                  user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
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
def portal_send_offer(cid: int, request: Request, user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
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
def portal_contact_start(cid: int, request: Request, user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
    _require(user, "update_status")
    c = _portal_company(s, user, cid)
    return _contact_start(request, s, user, c, _safe_back(request, f"/portal/companies/{cid}"))


@app.post("/portal/companies/{cid}/contact/update")
async def portal_contact_update(cid: int, request: Request, user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
    from ..permissions import visible_owner_ids
    _require(user, "update_status")
    c = _portal_company(s, user, cid)
    form = await request.form()
    ids = visible_owner_ids(s, user)
    owners = set(ids) if ids is not None else {u.id for u in s.scalars(select(User).where(User.active.is_(True)))}
    return _contact_update(request, s, user, c, form, _safe_back(request, f"/portal/companies/{cid}"), owners)


@app.get("/portal/officials", response_class=HTMLResponse)
def portal_officials(request: Request, user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
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
def admin_officials(request: Request, ostatus: str = "", user: User = Depends(current_user), s=Depends(get_db, scope="function")):
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
def officials_control(request: Request, action: str = Form(...), user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from .. import officials as O
    from ..auth import audit
    audit(s, user, "officials_control", None, op=action)
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
def admin_officials_xlsx(request: Request, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    from ..xlsx import CONTACT_HEADERS, contact_cells, response, workbook
    prs = _portal_rows(s, user, "")
    offs = _officials_map(s, [r["c"].id for r in prs])
    rows = [[r["c"].name, r["c"].activity, r["c"].city, r["c"].decision_unit or ""] + _official_cells(r["c"], offs.get(r["c"].id, []))
            + [r["phone"] or "", r["email"] or ""] + contact_cells(s, r["c"]) for r in prs]
    head = ["المنشأة", "النشاط", "المدينة", "جهة القرار المقترحة"] + OFFICIAL_HEADERS + \
        ["هاتف الشركة العام", "بريد الشركة العام"] + CONTACT_HEADERS
    return response(workbook([("موظفو الشركات", head, rows)]), "company-officials.xlsx")


@app.get("/portal/officials.xlsx")
def portal_officials_xlsx(request: Request, user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
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
def portal_reports_xlsx(request: Request, user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
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
def portal_message(mid: int, request: Request, user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
    _require(user, "see_offers")
    m = s.get(OutboundMessage, mid) or _404()
    _portal_company(s, user, m.company_id)
    return render(request, "portal_message.html", user, s, m=m, mask=lambda t: mask_text(user, t))


@app.get("/portal/reports", response_class=HTMLResponse)
def portal_reports(request: Request, user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
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


# ---------------------------------------------------------------- ملفات طلبات العملاء

def _kit_user(request: Request, s) -> User:
    """المستخدم من أي بوابة (الإدارة أو الموظفين)."""
    user = _session_user(request, s, "admin") or _session_user(request, s, "staff")
    if user is None:
        raise LoginRequired("/portal/login")
    return user


def _kit_manager(request: Request, s) -> User:
    user = _session_user(request, s, "admin")
    if user is None or user.role not in ("admin", "manager"):
        raise HTTPException(403, "إدارة «ملفات طلبات العملاء» للإدارة فقط")
    return user


def _kit_page(request: Request, s, user: User, base: str, company_id: int | None):
    from .. import kit
    from ..models import KitItem, KitLog
    from ..permissions import can_view_company
    kit.ensure_defaults(s)
    manage = base == "/kit" and user.role in ("admin", "manager")
    items = list(s.scalars(select(KitItem).order_by(KitItem.position, KitItem.id)))
    if not manage:
        items = [i for i in items if kit.visible_to_reps(i)]
    company, sent = None, []
    if company_id:
        company = s.get(Company, company_id)
        if company is None or (base.startswith("/portal") and not can_view_company(s, user, company)):
            raise HTTPException(404, "غير موجود")
        sent = list(s.scalars(select(KitLog).where(KitLog.company_id == company.id, KitLog.action == "sent")
                              .order_by(KitLog.id.desc()).limit(50)))
    logs = list(s.scalars(select(KitLog).order_by(KitLog.id.desc()).limit(40))) if manage else []
    view = [{"it": i, "share": kit.share_url(i), "lines": kit.message_lines(i), "copy_lbl": kit.copy_label(i),
             "ready": kit.visible_to_reps(i), "expired": kit.expired(i), "has": kit.has_content(i)} for i in items]
    return render(request, "kit.html", user, s, view=view, manage=manage, base=base, company=company, sent=sent,
                  logs=logs, KINDS=kit.KINDS, company_back=(f"/companies/{company.id}" if base == "/kit" else
                                                            f"/portal/companies/{company.id}") if company else None)


@app.get("/kit", response_class=HTMLResponse)
def kit_page(request: Request, company: int = 0, user: User = Depends(current_user), s=Depends(get_db, scope="function")):
    return _kit_page(request, s, user, "/kit", company or None)


@app.get("/portal/kit", response_class=HTMLResponse)
def portal_kit_page(request: Request, company: int = 0, user: User = Depends(staff_user), s=Depends(get_db, scope="function")):
    return _kit_page(request, s, user, "/portal/kit", company or None)


def _kit_fields(it, form, user) -> None:
    from datetime import date as _date
    it.title = (form.get("title") or it.title or "عنصر جديد").strip()[:200]
    it.note = (form.get("note") or "").strip()[:300]
    if form.get("kind") in __import__("mandoob.kit", fromlist=["KINDS"]).KINDS:
        it.kind = form.get("kind")
    for f in ("value", "url", "url2"):
        v = (form.get(f) or "").strip()
        if f != "value" and v and not re.match(r"https?://", v):
            v = "https://" + v
        setattr(it, f, v[:500] or None)
    it.body = (form.get("body") or "").strip() or None
    try:
        it.expires_on = _date.fromisoformat(form.get("expires_on")) if form.get("expires_on") else None
    except ValueError:
        it.expires_on = None
    it.published = form.get("published") == "on"
    it.share_enabled = form.get("share_enabled") == "on"
    it.updated_at, it.updated_by = now_utc(), user.name


async def _kit_file(it, form) -> bool:
    up = form.get("file")
    if up is None or not getattr(up, "filename", ""):
        return False
    data = await up.read()
    if not data:
        return False
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "الملف أكبر من 15 ميغابايت")
    import mimetypes
    it.file_name = up.filename[:255]
    it.file_data = data
    it.mime_type = (up.content_type if up.content_type and up.content_type != "application/octet-stream"
                    else mimetypes.guess_type(up.filename)[0]) or "application/octet-stream"
    it.size_bytes = len(data)
    it.version = (it.version or 0) + 1
    return True


@app.post("/kit/new")
async def kit_new(request: Request, s=Depends(get_db, scope="function")):
    from .. import kit
    from ..models import KitItem
    user = _kit_manager(request, s)
    form = await request.form()
    pos = (s.scalar(select(func.max(KitItem.position))) or 0) + 1
    it = KitItem(position=pos, share_token=kit.new_token(), title="عنصر جديد")
    _kit_fields(it, form, user)
    await _kit_file(it, form)
    s.add(it)
    s.flush()
    kit.log(s, "update", it, user)
    flash(request, f"أُضيف «{it.title}»" + ("" if it.published else " كمسودة — عاينه ثم انشره"))
    return back(f"/kit#item-{it.id}")


@app.post("/kit/{iid}/save")
async def kit_save(iid: int, request: Request, s=Depends(get_db, scope="function")):
    from .. import kit
    from ..models import KitItem
    user = _kit_manager(request, s)
    it = s.get(KitItem, iid) or _404()
    form = await request.form()
    was = it.published
    _kit_fields(it, form, user)
    replaced = await _kit_file(it, form)
    if form.get("remove_file") == "on" and not replaced:
        it.file_name = it.file_data = it.mime_type = None
        it.size_bytes = 0
    if it.published and not kit.has_content(it):
        it.published = False
        flash(request, f"«{it.title}» بلا محتوى بعد — حُفظ كمسودة. أضف الملف أو الرابط أو الرقم ثم انشره", "err")
    else:
        kit.log(s, "publish" if it.published and not was else "update", it, user)
        flash(request, f"حُفظ «{it.title}»" + (f" — النسخة {it.version}" if replaced else "")
              + (" ومنشور للمندوبين" if it.published else " كمسودة (لا يراه المندوبون)"))
    return back(f"/kit#item-{it.id}")


@app.post("/kit/{iid}/move")
def kit_move(iid: int, request: Request, dir: str = Form(...), s=Depends(get_db, scope="function")):
    from ..models import KitItem
    _kit_manager(request, s)
    items = list(s.scalars(select(KitItem).order_by(KitItem.position, KitItem.id)))
    i = next((k for k, x in enumerate(items) if x.id == iid), None)
    if i is None:
        _404()
    j = i - 1 if dir == "up" else i + 1
    if 0 <= j < len(items):
        items[i], items[j] = items[j], items[i]
    for k, x in enumerate(items, start=1):
        x.position = k
    return back(f"/kit#item-{iid}")


@app.post("/kit/{iid}/delete")
def kit_delete(iid: int, request: Request, s=Depends(get_db, scope="function")):
    from ..models import KitItem
    _kit_manager(request, s)
    it = s.get(KitItem, iid) or _404()
    flash(request, f"حُذف «{it.title}» وتوقف رابط مشاركته")
    s.delete(it)
    return back("/kit")


def _kit_item_for(request: Request, s, iid: int):
    from .. import kit
    from ..models import KitItem
    user = _kit_user(request, s)
    it = s.get(KitItem, iid)
    if it is None or not it.file_data:
        raise HTTPException(404, "غير موجود")
    is_mgr = request.session.get("portal") == "admin" and user.role in ("admin", "manager")
    if not kit.visible_to_reps(it) and not is_mgr:  # المسودة: معاينة الإدارة فقط
        raise HTTPException(404, "غير موجود")
    return user, it


def _kit_file_response(it, inline: bool) -> Response:
    from urllib.parse import quote
    disp = "inline" if inline else "attachment"
    ext = Path(it.file_name or "").suffix.lower()
    ascii_name = (re.sub(r"[^A-Za-z0-9._-]+", "-", Path(it.file_name or "").stem).strip("-") or "syarah-app") + ext
    return Response(it.file_data, media_type=it.mime_type or "application/octet-stream",
                    headers={"content-disposition": f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(it.file_name or 'file')}",
                             "cache-control": "no-store", "x-robots-tag": "noindex, nofollow",
                             "x-content-type-options": "nosniff"})


@app.get("/kit/{iid}/view")
def kit_view(iid: int, request: Request, s=Depends(get_db, scope="function")):
    user, it = _kit_item_for(request, s, iid)
    return _kit_file_response(it, inline=True)


@app.get("/kit/{iid}/file")
def kit_download(iid: int, request: Request, company: int = 0, s=Depends(get_db, scope="function")):
    from .. import kit
    user, it = _kit_item_for(request, s, iid)
    kit.log(s, "download", it, user, company or None)
    return _kit_file_response(it, inline=False)


@app.get("/kit/s/{token}")
def kit_share(token: str, s=Depends(get_db, scope="function")):
    """رابط المشاركة العام: المستند المعتمد وحده، بلا حساب وبلا أي جزء آخر من المنصة."""
    from .. import kit
    from ..models import KitItem
    it = s.scalar(select(KitItem).where(KitItem.share_token == token))
    if it is None or not kit.share_url(it):
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>غير متاح</title><body dir=rtl style='font-family:sans-serif;"
                            "padding:40px'>هذا الرابط غير متاح. اطلب رابطًا جديدًا من مندوب سيارة اب.</body>", status_code=404)
    kit.log(s, "share_open", it)
    if it.file_data:
        return _kit_file_response(it, inline=True)
    import html as _h
    body = "".join(f"<p>{_h.escape(p).replace(chr(10), '<br>')}</p>" for p in (it.body or "").split("\n\n"))
    return HTMLResponse(f"<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><meta name=viewport content='width=device-width,"
                        f"initial-scale=1'><meta name=robots content=noindex><title>{_h.escape(it.title)} — سيارة اب</title>"
                        "<body style='font-family:Almarai,Tahoma,sans-serif;max-width:760px;margin:0 auto;padding:28px 18px;"
                        f"line-height:1.9;color:#292431;background:#FAF8FC'><h1 style='color:#6941A5'>{_h.escape(it.title)}</h1>"
                        f"{body}<p style='color:#888;font-size:13px'>سيارة اب</p></body></html>")


@app.post("/kit/sent")
async def kit_sent(request: Request, s=Depends(get_db, scope="function")):
    """«تسجيل ما تم إرساله»: اسم المستند ونسخته واسم المندوب والتاريخ في سجل العميل."""
    from .. import kit
    from ..models import KitItem
    from ..permissions import can_view_company
    user = _kit_user(request, s)
    form = await request.form()
    comp = s.get(Company, int(form.get("company_id") or 0)) or _404()
    staff = request.session.get("portal") == "staff"
    if staff and not can_view_company(s, user, comp):
        raise HTTPException(404, "غير موجود")
    ids = [int(x) for x in form.getlist("item_ids") if str(x).isdigit()]
    items = [it for it in (s.get(KitItem, i) for i in ids) if it is not None and kit.visible_to_reps(it)]
    base = "/portal/kit" if staff else "/kit"
    if not items:
        flash(request, "اختر العناصر التي أرسلتها أولًا", "err")
        return back(f"{base}?company={comp.id}")
    for it in items:
        kit.log(s, "sent", it, user, comp.id)
    names = "، ".join(f"{it.title}" + (f" (نسخة {it.version})" if it.file_data else "") for it in items)
    log_activity(s, comp.id, "note", "أُرسلت ملفات للعميل", f"{names} — بواسطة {user.name}", user_id=user.id)
    flash(request, f"سُجّل إرسال {len(items)} عنصر إلى العميل")
    return back(f"{base}?company={comp.id}")


# ---------------------------------------------------------------- عملاء CRM (نسخة للقراءة فقط من crm.syarahapp.sa)

def _crm_page(request: Request, s, user: User, base: str, q: str, page: int):
    from .. import crm
    from ..permissions import can
    stmt = crm.visible_query(s, user, q)
    total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    per = 50
    page = max(1, page)
    rows = list(s.scalars(stmt.order_by(Company.name).offset((page - 1) * per).limit(per)))
    owners = {u.id: u.name for u in s.scalars(select(User))}
    manage = base == "/crm" and user.role in ("admin", "manager")
    conn, st = crm.get_conn(s), crm.get_state(s)
    all_total = s.scalar(select(func.count(Company.id)).where(Company.crm_id.isnot(None))) or 0
    return render(request, "crm.html", user, s, rows=rows, total=total, all_total=all_total, q=q, page=page,
                  pages=max(1, -(-total // per)), owners=owners, base=base, manage=manage,
                  is_admin=user.role == "admin" and base == "/crm", st=st,
                  conn={"username": conn.get("username"), "has_password": bool(conn.get("password_enc")),
                        "entity": conn.get("entity"), "daily": conn.get("daily"), "base_url": conn.get("base_url")},
                  see_phone=can(user, "see_phone") or base == "/crm", see_email=can(user, "see_email") or base == "/crm",
                  crm_num=crm.display_number)


@app.get("/crm", response_class=HTMLResponse)
def crm_page(request: Request, q: str = "", page: int = 1, user: User = Depends(current_user),
             s=Depends(get_db, scope="function")):
    return _crm_page(request, s, user, "/crm", q, page)


@app.get("/portal/crm", response_class=HTMLResponse)
def portal_crm_page(request: Request, q: str = "", page: int = 1, user: User = Depends(staff_user),
                    s=Depends(get_db, scope="function")):
    return _crm_page(request, s, user, "/portal/crm", q, page)


@app.post("/crm/settings")
async def crm_settings(request: Request, user: User = Depends(require_admin), s=Depends(get_db, scope="function")):
    """إعدادات الربط الآمنة: كلمة المرور تُشفّر ولا تُعاد للواجهة."""
    from .. import crm
    from ..auth import audit
    form = await request.form()
    crm.set_conn(s, username=(form.get("username") or "").strip()[:200] or None,
                 password=(form.get("password") or "") or None,
                 entity=form.get("entity") if form.get("entity") in ("contact", "lead") else None,
                 daily=form.get("daily") == "on")
    audit(s, user, "crm_settings", None, username=(form.get("username") or "").strip()[:200],
          password_changed=bool(form.get("password")))
    flash(request, "حُفظت إعدادات الربط (كلمة المرور مشفّرة ولا تُعرض). اضغط «اختبار الاتصال».")
    return back("/crm#settings")


@app.post("/crm/test")
def crm_test(request: Request, user: User = Depends(require_admin), s=Depends(get_db, scope="function")):
    from .. import crm
    try:
        r = crm.test_connection()
        flash(request, f"نجح الاتصال بالـCRM: {r['total']} عميل. الحقول: " + "، ".join(r["fields"][:25]))
    except crm.CrmError as e:
        flash(request, f"تعذّر الاتصال: {e}", "err")
    return back("/crm#settings")


@app.post("/crm/sync")
def crm_sync(request: Request, mode: str = Form("full"), user: User = Depends(require_manager),
             s=Depends(get_db, scope="function")):
    from .. import crm
    if not crm.has_credentials(crm.get_conn(s)):
        flash(request, "أدخل بيانات الدخول إلى الـCRM في «إعدادات الربط» أولًا، أو استورد ملف Excel", "err")
        return back("/crm")
    crm.set_state(s, requested={"limit": 20} if mode == "sample" else {"full": True}, requested_by=user.name)
    flash(request, ("سيُسحب عينة من ٢٠ عميلًا" if mode == "sample" else "سيبدأ التحديث من الـCRM") + " خلال دقيقة. حدّث الصفحة لرؤية النتيجة.")
    return back("/crm")


@app.post("/crm/import")
async def crm_import(request: Request, file: UploadFile = File(...), user: User = Depends(require_manager),
                     s=Depends(get_db, scope="function")):
    from .. import crm
    from ..prospects import UploadError
    data = await file.read()
    try:
        st = crm.import_file(file.filename or "crm.xlsx", data)
    except UploadError as e:
        flash(request, f"تعذّر قراءة الملف: {e}", "err")
        return back("/crm")
    flash(request, f"استُورد ملف الـCRM: {st['added']} مضاف، {st['updated']} محدّث"
                   + (f" (منها {st['linked']} رُبط بعميل موجود)" if st["linked"] else "") + f"، {st['failed']} متعذّر")
    return back("/crm")


# ---------------------------------------------------------------- بحث مباشر في خرائط Google (للعرض فقط)

def _google_cities():
    from ..geo import PLACES
    from ..overture import CITY_BBOX
    return [(pid, PLACES[pid]["ar"]) for pid in CITY_BBOX if pid in PLACES]


templates.env.globals.update(GOOGLE_CITIES=_google_cities())


@app.get("/discover/google", response_class=HTMLResponse)
def discover_google(request: Request, activity: str = "transport.freight", city: str = "SA-01/riyadh", q: str = "",
                    user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from urllib.parse import urlencode
    from .. import google_live as G
    from ..activities import ACTIVITIES
    from ..geo import PLACES
    from ..overture import CITY_BBOX
    query = q.strip() or (f"{ACTIVITIES[activity]['label']} في {PLACES[city]['ar']}"
                          if activity in ACTIVITIES and city in PLACES else "")
    results, error, map_qs = [], None, ""
    if query and request.query_params:
        bb = CITY_BBOX.get(city)
        center = ((bb[2] + bb[3]) / 2, (bb[0] + bb[1]) / 2) if bb else None
        try:
            results = G.search(query, center)
            pts = [f"{r['lat']:.6f},{r['lng']:.6f}" for r in results if r.get("lat") is not None]
            map_qs = urlencode([("p", x) for x in pts[:20]])
        except G.LiveError as e:
            error = str(e)
    from ..config import settings as _st
    return render(request, "google_live.html", user, s, ACTIVITIES=ACTIVITIES, activity=activity, city=city, q=q,
                  query=query if request.query_params else "", results=results, error=error, map_qs=map_qs,
                  cost=G.COST_PER_SEARCH, status=G.get_status(s), has_key=bool(_st.google_places_api_key), budget=G.budget(s))


@app.post("/discover/google/budget")
def discover_google_budget(request: Request, monthly_usd: str = Form(...), user: User = Depends(require_admin),
                           s=Depends(get_db, scope="function")):
    from .. import google_live as G
    try:
        v = float(re.sub(r"[^\d.]", "", monthly_usd) or 0)
    except ValueError:
        v = 0
    if v <= 0:
        flash(request, "اكتب مبلغًا صحيحًا بالدولار", "err")
        return back("/discover/google")
    G.set_budget(s, v)
    flash(request, f"سقف خرائط Google الشهري الآن ${v:,.0f}")
    return back("/discover/google")


@app.get("/discover/google/map")
def discover_google_map(request: Request, user: User = Depends(require_manager)):
    """صورة خريطة Google للنتائج — عبر الخادم (المفتاح لا يصل للمتصفح)، لا تُخزن."""
    from .. import google_live as G
    pts = []
    for v in request.query_params.getlist("p")[:20]:
        try:
            la, ln = (float(x) for x in v.split(","))
            if 15 < la < 33 and 34 < ln < 56:  # داخل المملكة فقط
                pts.append((la, ln))
        except ValueError:
            continue
    try:
        img, ct = G.static_map(pts)
    except G.LiveError as e:
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="640" height="80"><rect width="100%" height="100%" fill="#FAF8FC"/>'
               f'<text x="320" y="45" font-size="16" text-anchor="middle" fill="#6941A5" font-family="sans-serif">{e}</text></svg>')
        return Response(svg, media_type="image/svg+xml", headers={"cache-control": "no-store"})
    return Response(img, media_type=ct, headers={"cache-control": "private, max-age=300"})


# ---------------------------------------------------------------- «اكتشاف من خرائط جوجل»: فرص البيع بالأدلة

def _signal_runs(s):
    from .. import directory as D
    from ..models import DirectoryRun
    return [r for r in s.scalars(select(DirectoryRun).order_by(DirectoryRun.id.desc()).limit(60))
            if set(r.sources or []) & set(D.SIGNAL_TRACKS)][:12]


def _run_source_costs(s, r) -> dict:
    """تكلفة كل مصدر في الدفعة (من دفتر التكاليف: dir:<المصدر>:…)."""
    from ..models import CostEntry
    if not r.started_at:
        return {}
    end = r.finished_at or now_utc()
    out = {}
    for note, cost in s.execute(select(CostEntry.note, func.sum(CostEntry.cost_usd)).where(
            CostEntry.at >= r.started_at, CostEntry.at <= end, CostEntry.note.like("dir:%")).group_by(CostEntry.note)):
        src = (note or "").split(":")[1] if (note or "").count(":") >= 1 else ""
        out[src] = round(out.get(src, 0) + float(cost or 0), 3)
    return out


@app.get("/signals", response_class=HTMLResponse)
def signals_page(request: Request, run: int = 0, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from .. import directory as D, geo
    from ..activities import ACTIVITIES, SECTORS
    from ..contact import STATUSES
    from ..models import DirectoryRun, LeadStub
    from ..goal import achieved as _ach, target as _goal
    runs = _signal_runs(s)
    cur = s.get(DirectoryRun, run) if run else (runs[0] if runs else None)
    rows = []
    per_src = []
    if cur:
        stubs = list(s.scalars(select(LeadStub).where(LeadStub.run_id == cur.id).order_by(LeadStub.id)))
        cids = [x.company_id or x.duplicate_of for x in stubs if (x.company_id or x.duplicate_of)]
        comps = {c.id: c for c in s.scalars(select(Company).where(Company.id.in_(cids or [0])))}
        users = {u.id: u.name for u in s.scalars(select(User))}
        from ..models import Activity
        acts_by: dict[int, list] = {}
        for a in s.scalars(select(Activity).where(Activity.company_id.in_(list(comps) or [0]))
                           .order_by(Activity.at.desc()).limit(600)):
            if len(acts_by.setdefault(a.company_id, [])) < 6:
                acts_by[a.company_id].append(a)
        for st in stubs:
            c = comps.get(st.company_id or st.duplicate_of)
            note = st.operator_note or ""
            veh_word = "الحافلات" if any(w in (st.activity or "") for w in ("مدرس", "حافلات", "طلاب", "نقل الموظفين", "ركاب")) \
                else "المركبات"
            rows.append({"st": st, "c": c, "owner": users.get(c.owner_id) if c and c.owner_id else None,
                         "acts": acts_by.get(c.id, []) if c else [],
                         "op_short": (f"تشغيل {veh_word} غير مؤكد" if "غير مؤكد" in note else
                                      (f"تشغيل {veh_word} مؤكد" if note else "")),
                         "phones": [x for x in (c.contacts if c else []) if x.kind == "phone"],
                         "emails": [x for x in (c.contacts if c else []) if x.kind == "email"],
                         "last_by": users.get(c.last_attempt_by_id) if c and c.last_attempt_by_id else None})
        costs = _run_source_costs(s, cur)
        minutes = (cur.stats or {}).get("_minutes") or (round(((cur.finished_at or now_utc()) - cur.started_at)
                                                            .total_seconds() / 60, 1) if cur.started_at else None)
        for src in cur.sources or []:
            v = (cur.stats or {}).get(src) or {}
            comp = v.get("complete", 0)
            per_src.append({"src": src, "label": D.SOURCES.get(src, {}).get("label", src), **{k: v.get(k, 0) for k in (
                "discovered", "new", "complete", "incomplete", "duplicate", "existing_customer", "pending_verify", "rejected_name")},
                "cost": costs.get(src, 0.0), "cpc": round(costs.get(src, 0.0) / comp, 2) if comp else None})
    else:
        minutes = None
    return render(request, "signals.html", user, s, runs=runs, cur=cur, rows=rows, per_src=per_src, minutes=minutes,
                  TRACKS=D.SIGNAL_TRACKS, ACTIVITIES=ACTIVITIES, DIR_SECTORS=SECTORS, GEO_REGIONS=geo.REGIONS,
                  GEO_PLACES=geo.PLACES, STATUSES=STATUSES, goal_t=_goal(s), goal_n=_ach(s),
                  reps=list(s.scalars(select(User).where(User.active.is_(True)).order_by(User.name))),
                  SEARCH_GROUPS=__import__("mandoob.activities", fromlist=["x"]).SEARCH_GROUPS,
                  google_key=bool(settings.google_places_api_key),
                  google_status=__import__("mandoob.google_live", fromlist=["x"]).get_status(s),
                  busy=any(r.status in ("pending", "running") for r in s.scalars(
                      select(DirectoryRun).where(DirectoryRun.status.in_(["pending", "running"])))))


@app.post("/signals/run")
async def signals_run(request: Request, user: User = Depends(require_manager), s=Depends(get_db, scope="function")):
    from .. import directory as D, geo
    from ..activities import ACTIVITIES
    from ..auth import audit
    from ..goal import halt_reason
    form = await request.form()
    if (h := halt_reason()):
        flash(request, f"توقف الجمع لهذا اليوم: {h}", "err")
        return back("/signals")
    tracks = [x for x in form.getlist("tracks") if x in D.SIGNAL_TRACKS] or ["sig_direct"]
    acts = form.getlist("activities")
    acts = "__all__" if (not acts or "__all__" in acts) else \
        [a for a in acts if a in ACTIVITIES or a.startswith(("group:", "sector:"))]
    mode = form.get("scope_mode") or "all"
    if mode == "places":
        pl = [p for p in form.getlist("places") if p in geo.PLACES]
        scope = {"places": pl} if pl else {"all": True}
    elif mode == "regions":
        rg = [r for r in form.getlist("regions") if r in geo.REGIONS]
        scope = {"regions": rg} if rg else {"all": True}
    else:
        scope = {"all": True}
    try:
        limit = max(3, min(int(form.get("limit") or 30), 400))
        budget = max(0.5, min(float(form.get("budget") or 5), 200.0))
    except ValueError:
        limit, budget = 30, 5.0
    r = D.request(s, sources=tracks, params={"activities": acts, "scope": scope, "stage1_limit": limit,
                                             "signals": True}, budget=budget, user_id=user.id)
    if r.sources != tracks:
        flash(request, f"دفعة أخرى تعمل الآن (#{r.id}) — ستبدأ دفعتك بعد انتهائها؛ أعد المحاولة بعد قليل", "err")
        return back("/signals")
    audit(s, user, "signals_run", None, tracks=tracks, scope=scope, limit=limit, budget=budget)
    flash(request, f"بدأت دفعة فرص البيع #{r.id} — يلتقطها العامل خلال دقيقة. الحد {limit} اسمًا والميزانية ${budget:.2f}.")
    return back(f"/signals?run={r.id}")


# ---------------------------------------------------------------- حاسبة تكلفة وقود الأسطول (صفحة عامة)

@app.get("/fleet-calculator", response_class=HTMLResponse)
def fleet_calculator(request: Request, src: str = "", done: int = 0, s=Depends(get_db, scope="function")):
    staff = _session_user(request, s, "admin")
    return templates.TemplateResponse(request, "fleet_calculator.html", {
        "src": "".join(ch for ch in src if ch.isalnum() or ch in "-_")[:40], "done": done,
        "is_admin": bool(staff and staff.role in ("admin", "manager")),
        "flash": request.session.pop("flash", None)})


@app.post("/fleet-calculator")
def fleet_calculator_submit(request: Request, vehicles: str = Form(...), spend: str = Form(...),
                            company_name: str = Form(...), contact_name: str = Form(""), phone: str = Form(""),
                            email: str = Form(""), consent: str = Form(""), src: str = Form(""),
                            website_url: str = Form(""), s=Depends(get_db, scope="function")):
    """«أرغب في مراجعة تكلفة أسطولي مع فريق سيارة أب»: يظهر الطلب للفريق مع عدد المركبات ومصدر الطلب."""
    from ..leads import create_lead, hash_ip, too_many
    from ..models import InboundLead
    if website_url:  # حقل مخفي لاصطياد البرامج الآلية
        return back("/fleet-calculator?done=1")
    try:
        nveh = max(1, min(int(re.sub(r"\D", "", vehicles) or 0), 100000))
        per = max(0.0, min(float(re.sub(r"[^\d.]", "", spend) or 0), 1_000_000.0))
    except ValueError:
        nveh, per = 0, 0.0
    if not nveh or not company_name.strip() or not consent or not (normalize_phone(phone) or normalize_email(email)):
        flash(request, "أدخل عدد المركبات واسم المنشأة ورقم جوال أو بريدًا صحيحًا، ووافق على التواصل", "err")
        return back("/fleet-calculator")
    ip = hash_ip(request.client.host if request.client else None, settings.secret_key)
    if too_many(s, ip):
        flash(request, "تم استلام عدة طلبات من هذا الجهاز. سنتواصل معكم قريبًا.", "err")
        return back("/fleet-calculator")
    monthly, yearly = round(nveh * per), round(nveh * per * 12)
    staff = _session_user(request, s, "admin")
    is_test = bool(staff and staff.role in ("admin", "manager"))  # طلب من حساب الإدارة = تجريبي
    clean_src = "".join(ch for ch in src if ch.isalnum() or ch in "-_")[:40]
    lead = create_lead(s, company_name=company_name, city="", vehicles=nveh, activity=None, offer_id=None,
                       service_text="مراجعة تكلفة وقود الأسطول", contact_name=contact_name or company_name,
                       phone=phone, email=email, preferred_channel="phone",
                       source="fuel_calculator" + (f":{clean_src}" if clean_src else ""),
                       notes=f"حاسبة الوقود: {nveh} مركبة × {per:,.0f} ر.س شهريًا = {monthly:,} ر.س شهريًا / {yearly:,} ر.س سنويًا",
                       ip_hash=ip)
    if is_test:
        lead.is_test = True
        c = s.get(Company, lead.company_id) if lead.company_id else None
        if c is not None and c.lead_source and c.lead_source.startswith("fuel_calculator"):
            c.is_test = True  # مستبعد من أرقام المبيعات والهدف اليومي
    return back("/fleet-calculator?done=1")
