"""الواجهة العربية (من اليمين لليسار) — FastAPI + Jinja2."""
from __future__ import annotations

import hashlib
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
from ..models import (Attachment, Call, Campaign, Category, Company, Consent, DiscoveryRun, ExistingCustomer,
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
    if settings.run_scheduler_in_web:
        from ..worker import start_background
        start_background()


# ---------------------------------------------------------------- أدوات

class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _login_redirect(request, exc):
    return RedirectResponse("/login", status_code=303)


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


def current_user(request: Request, s=Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = s.get(User, uid) if uid else None
    if not user or not user.active:
        raise LoginRequired()
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
    return templates.TemplateResponse(request, name, {"user": user, "unread": unread, "flash": flash,
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

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", None)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    u = authenticate(email, password)
    if not u:
        flash(request, "البريد أو كلمة المرور غير صحيحة", "err")
        return back("/login")
    request.session["uid"] = u.id
    return back("/")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


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
    return render(request, "dashboard.html", user, s, d=d, follow=follow, camps=camps, cats=cats[:8])


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
              page: int = 1, source: str = "", operator: str = "", signals: str = "",
              user: User = Depends(current_user), s=Depends(get_db)):
    from ..models import CompanySignal
    qq = scope_companies(select(Company), user).where(Company.data_status == status)
    if source:
        qq = qq.where(Company.lead_source.like(f"{source}%"))
    if operator:
        qq = qq.where(Company.operator_status == operator)
    if signals:
        qq = qq.where(Company.id.in_(select(CompanySignal.company_id)))
    if q:
        qq = qq.where(Company.name.like(f"%{q}%") | Company.domain.like(f"%{q}%"))
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
                  operator=operator, signals=signals, sig=sig, source_label=source_label)


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
def mail_test(request: Request, to: str = Form(...), user: User = Depends(require_manager), s=Depends(get_db)):
    from ..mail import get_provider
    from ..mail.base import OutgoingEmail
    from ..outreach import active_account, render_html, render_text
    acct = active_account(s) or _404()
    body = "هذه رسالة تجربة من منصة «مندوب سيارة آب الذكي» للتأكد من ربط البريد والتوقيع."
    try:
        res = get_provider(acct).send(OutgoingEmail(acct.sender_address, acct.sender_name, to,
                                                    "رسالة تجربة — مندوب سيارة آب الذكي",
                                                    render_text(body, acct.signature_html),
                                                    render_html(body, acct.signature_html),
                                                    reply_to=acct.reply_mailbox or None))
        flash(request, ("نجح الإرسال: " + res.detail) if res.accepted else ("فشل الإرسال: " + res.detail),
              "ok" if res.accepted else "err")
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
        u = User(email=email, name="", password_hash=hash_password(form.get("password") or "change-me"))
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
    return render(request, "settings.html", user, s, voice=voice, integrations=integrations,
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
