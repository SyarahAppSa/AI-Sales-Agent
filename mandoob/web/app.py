"""الواجهة العربية (من اليمين لليسار) — FastAPI + Jinja2."""
from __future__ import annotations

import hashlib
import os
import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
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


app = FastAPI(title="مندوب سيارة آب الذكي", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 12, same_site="lax",
                   https_only=settings.public_base_url.startswith("https"))
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(fmt=fmt, fmt_date=fmt_date, STATUS_LABELS=STATUS_LABELS, STAGE_LABELS=STAGE_LABELS,
                             SECTORS=SECTORS, CITIES=list(CITIES), REGIONS=REGIONS, OPERATOR_LABELS=OPERATOR_LABELS, ROLE_LABELS=ROLE_LABELS,
                             INTENT_LABELS=INTENT_LABELS, SOURCE_LABELS=SOURCE_LABELS, phone=format_phone_display)


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
    from ..auth import mail_ready
    return render(request, "otp_login.html", None, portal=portal, step=request.session.get(f"otp_{portal}"),
                  bootstrap=(portal == "admin" and not mail_ready(s)))


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
    request.session[f"otp_{portal}"] = email.strip().lower()
    flash(request, "أُرسل كود الدخول إلى بريدك. صالح لمدة ٥ دقائق ولمرة واحدة.")


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
    request.session.update({"uid": user.id, "sv": user.session_version, "portal": portal})
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
    request.session.update({"uid": u.id, "sv": u.session_version, "portal": "admin"})
    return back("/")


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
def home(request: Request, user: User = Depends(current_user), s=Depends(get_db)):
    d = dashboard(s, user)
    follow = needs_followup_today(s, user)[:8]
    camps = list(s.scalars(select(Campaign).order_by(Campaign.id.desc())))
    today = riyadh_today()
    cats = []
    for camp in camps:
        if camp.status != "active":
            continue
        for cat in camp.categories:
            if cat.active and cat.quota:
                done = s.scalar(select(func.count(Company.id)).where(
                    Company.category_id == cat.id, Company.counted_on == today,
                    Company.data_status == "complete")) or 0
                cats.append({"label": cat.label + (f" — {cat.cities[0]}" if len(cat.cities) == 1 else ""),
                             "done": done, "quota": cat.quota})
    test_groups = {}
    for reason, n in s.execute(select(Company.fit_reason, func.count()).where(Company.is_test.is_(True))
                               .group_by(Company.fit_reason)):
        g = reason.split(" — ")[1] if reason and " — " in reason else "أخرى"
        test_groups[g] = test_groups.get(g, 0) + n
    test_unassigned = s.scalar(select(func.count(Company.id)).where(Company.is_test.is_(True), Company.owner_id.is_(None))) or 0
    return render(request, "dashboard.html", user, s, d=d, follow=follow, camps=camps, cats=cats[:8],
                  test_groups=test_groups, test_total=sum(test_groups.values()), test_unassigned=test_unassigned)


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
        missing = [c.label for c in camp.categories if c.active and c.offer_id and not c.offer.approved]
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


# ---------------------------------------------------------------- الشركات

@app.get("/companies", response_class=HTMLResponse)
def companies(request: Request, status: str = "complete", q: str = "", city: str = "", sector: str = "",
              page: int = 1, source: str = "", operator: str = "", signals: str = "", kind: str = "",
              owner: str = "", user: User = Depends(current_user), s=Depends(get_db)):
    from ..models import CompanySignal
    qq = scope_companies(select(Company), user).where(Company.data_status == status)
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
                  staff=list(s.scalars(select(User).where(User.role == "rep").order_by(User.name))),
                  test_total=s.scalar(select(func.count(Company.id)).where(Company.is_test.is_(True))) or 0)


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
                  relations=relations, signals=signals, source_label=source_label)


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
        flash(request, "لم تُضف مفاتيح Microsoft 365 في Render بعد (MS_TENANT_ID و MS_CLIENT_ID و MS_CLIENT_SECRET). "
                       "أضفها في مجموعة mandoob-secrets ثم أعد المحاولة.", "err")
        return back("/mail")
    body = message.strip() or "هذه رسالة تجربة من منصة «مندوب سيارة آب الذكي» للتأكد من ربط البريد والتوقيع."
    try:
        res = get_provider(acct).send(OutgoingEmail(acct.sender_address, acct.sender_name, to,
                                                    "رسالة تجربة — مندوب سيارة آب الذكي",
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
                                                                  Company.stage.in_(["contacted", "replied", "interested", "meeting"]))) or 0,
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
    rows = list(s.scalars(select(DiscoveryRun).order_by(DiscoveryRun.id.desc()).limit(30)))
    return render(request, "runs.html", user, s, rows=rows)


@app.get("/daily-report", response_class=HTMLResponse)
def daily_report_page(request: Request, day: str = "", user: User = Depends(require_manager), s=Depends(get_db)):
    d = date.fromisoformat(day) if day else riyadh_today()
    return render(request, "daily_report.html", user, s, r=daily_report(s, d))


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


@app.get("/today", response_class=HTMLResponse)
def today_page(request: Request, day: str = "", campaign: Optional[int] = None, user: User = Depends(current_user),
               s=Depends(get_db)):
    from ..reports import todays_customers
    d = date.fromisoformat(day) if day else riyadh_today()
    t = todays_customers(s, user, d, campaign)
    return render(request, "today.html", user, s, t=t, campaign=campaign, qs=str(request.query_params),
                  camps=list(s.scalars(select(Campaign).order_by(Campaign.id.desc()))))


@app.get("/today.xlsx")
def today_xlsx(request: Request, day: str = "", campaign: Optional[int] = None, user: User = Depends(current_user),
               s=Depends(get_db)):
    from ..reports import export_today_xlsx, todays_customers
    d = date.fromisoformat(day) if day else riyadh_today()
    data = export_today_xlsx(todays_customers(s, user, d, campaign))
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=customers-{d.isoformat()}.xlsx"})


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
                     "email": em.value if em and can(u, "see_email") else None, "offer": last_offer, "reply": last_reply,
                     "next": nxt})
    return rows


@app.get("/portal", response_class=HTMLResponse)
def portal_home(request: Request, q: str = "", user: User = Depends(staff_user), s=Depends(get_db)):
    from ..permissions import can, describe
    return render(request, "portal_home.html", user, s, rows=_portal_rows(s, user, q), q=q, can=lambda p: can(user, p),
                  d=describe(user), mask=lambda t: mask_text(user, t))


@app.get("/portal/export.xlsx")
def portal_export(request: Request, q: str = "", user: User = Depends(staff_user), s=Depends(get_db)):
    from openpyxl import Workbook
    import io as _io
    from ..auth import audit
    from ..permissions import can
    if not can(user, "export"):
        raise HTTPException(403, "لا تملك صلاحية التصدير")
    wb = Workbook()
    ws = wb.active
    ws.title = "عملائي"
    ws.sheet_view.rightToLeft = True
    head = ["الشركة", "النشاط", "المدينة"] + (["الهاتف"] if can(user, "see_phone") else []) + \
        (["البريد"] if can(user, "see_email") else []) + (["آخر عرض", "تاريخ الإرسال"] if can(user, "see_offers") else []) + \
        (["آخر رد", "نوع الرد"] if can(user, "see_replies") else []) + ["المتابعة القادمة", "موعدها"]
    ws.append(head)
    for r in _portal_rows(s, user, q):
        c = r["c"]
        row = [c.name, c.activity, c.city] + ([r["phone"] or ""] if can(user, "see_phone") else []) + \
            ([r["email"] or ""] if can(user, "see_email") else [])
        if can(user, "see_offers"):
            row += [r["offer"].subject if r["offer"] else "", fmt(r["offer"].sent_at) if r["offer"] else ""]
        if can(user, "see_replies"):
            row += [mask_text(user, r["reply"].body_text[:500]) if r["reply"] else "", r["reply"].intent_label if r["reply"] else ""]
        row += [r["next"].title if r["next"] else "", fmt(r["next"].due_at) if r["next"] else ""]
        ws.append(row)
    buf = _io.BytesIO()
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
    return render(request, "portal_company.html", user, s, c=c, timeline=timeline, tasks=tasks, msgs=msgs,
                  can=lambda p: can(user, p), mask=lambda t: mask_text(user, t))


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
                  done_task: Optional[int] = Form(None), user: User = Depends(staff_user), s=Depends(get_db)):
    _require(user, "update_status")
    c = _portal_company(s, user, cid)
    changes = []
    if stage in ("contacted", "replied", "interested", "meeting", "won", "lost") and stage != c.stage:
        changes.append(f"الحالة: {STAGE_LABELS.get(c.stage)} ← {STAGE_LABELS.get(stage)}")
        c.stage = stage
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
