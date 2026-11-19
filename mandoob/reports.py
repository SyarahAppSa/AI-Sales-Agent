"""التقارير: «الشركات التي أُرسلت إليها عروض»، «تحتاج متابعة اليوم»، لوحة الإدارة، والتقرير اليومي."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.orm import Session

from .models import (Activity, Call, Campaign, Category, Company, CostEntry, DiscoveryRun, InboundMessage,
                     Meeting, Offer, OutboundMessage, Task, User)
from .normalize import format_phone_display
from .targeting import SECTORS
from .timeutil import fmt, riyadh_day_bounds_utc, riyadh_today

STATUS_LABELS = {"queued": "بانتظار الإرسال", "sending": "قيد الإرسال", "sent": "أُرسل", "delivered": "تم التسليم",
                 "bounced": "مرتد", "failed": "فشل الإرسال", "skipped": "أُلغي"}
STAGE_LABELS = {"new": "لم يتم التواصل", "contacted": "تم التواصل", "offer_sent": "أُرسل عرض",
                "needs_followup": "تحتاج متابعة", "replied": "ردّت", "interested": "مهتمة",
                "meeting": "اجتماع محجوز", "won": "صفقة رابحة", "lost": "غير مهتمة",
                "do_not_contact": "أوقفت التواصل"}


def demo_view(s) -> bool:
    """«وضع العرض التدريبي»: حين يكون مفعّلًا تدخل البيانات التجريبية في الإحصائيات والتقارير (للشرح للموظفين)."""
    from .models import Setting
    row = s.get(Setting, "demo_view")
    return bool(row and (row.value or {}).get("on"))


def visible(s):
    """شرط الشركات المحتسبة في الإحصائيات: الحقيقية فقط، أو معها التجريبية في وضع العرض التدريبي."""
    return true() if demo_view(s) else Company.is_test.is_(False)


def visible_ids(s):
    return select(Company.id).where(visible(s))


def scope_companies(q, user: User):
    """المندوب يرى شركاته فقط؛ المدير ومدير المبيعات يريان الكل."""
    if user is not None and user.role == "rep":
        return q.where(Company.owner_id == user.id)
    return q


@dataclass
class OfferFilters:
    date_from: date | None = None
    date_to: date | None = None
    city: str | None = None
    sector: str | None = None
    campaign_id: int | None = None
    owner_id: int | None = None
    offer_id: int | None = None
    status: str | None = None          # sent / delivered / bounced ...
    reply: str | None = None           # replied / no_reply / <intent>
    q: str | None = None
    source: str | None = None          # مصدر الفرصة (discovery / website_form / campaign / partner / carrier_of)


@dataclass
class OfferRow:
    msg: OutboundMessage
    company: Company
    phone: str
    owner_name: str
    last_reply: InboundMessage | None
    offer_name: str
    campaign_name: str


@dataclass
class OffersReport:
    rows: list[OfferRow] = field(default_factory=list)
    unique_companies: int = 0
    total_offers: int = 0
    manual: list = field(default_factory=list)  # عروض أرسلها الموظفون بأنفسهم وسجّلوها في المنصة


def offers_report(s: Session, user: User, f: OfferFilters) -> OffersReport:
    q = (select(OutboundMessage).join(Company, Company.id == OutboundMessage.company_id)
         .where(OutboundMessage.kind == "offer",
                OutboundMessage.status.in_(["sent", "delivered", "bounced"])))
    q = scope_companies(q, user).where(visible(s))
    if f.date_from:
        q = q.where(OutboundMessage.sent_at >= riyadh_day_bounds_utc(f.date_from)[0])
    if f.date_to:
        q = q.where(OutboundMessage.sent_at < riyadh_day_bounds_utc(f.date_to)[1])
    if f.city:
        q = q.where(Company.city == f.city)
    if f.sector:
        q = q.where(Company.sector_key == f.sector)
    if f.campaign_id:
        q = q.where(OutboundMessage.campaign_id == f.campaign_id)
    if f.owner_id:
        q = q.where(Company.owner_id == f.owner_id)
    if f.offer_id:
        q = q.where(OutboundMessage.offer_id == f.offer_id)
    if f.status:
        q = q.where(OutboundMessage.status == f.status)
    if f.source:
        q = q.where(Company.lead_source.like(f"{f.source}%"))
    if f.q:
        like = f"%{f.q}%"
        q = q.where(or_(Company.name.like(like), OutboundMessage.to_address.like(like),
                        OutboundMessage.subject.like(like)))
    msgs = list(s.scalars(q.order_by(OutboundMessage.sent_at.desc())))
    rep = OffersReport()
    for m in msgs:
        last = s.scalar(select(InboundMessage).where(InboundMessage.company_id == m.company_id,
                                                     InboundMessage.message_type == "reply")
                        .order_by(InboundMessage.received_at.desc()))
        if f.reply == "replied" and not last:
            continue
        if f.reply == "no_reply" and last:
            continue
        if f.reply and f.reply not in ("replied", "no_reply") and (not last or last.intent != f.reply):
            continue
        c = m.company
        ph = c.primary("phone")
        rep.rows.append(OfferRow(m, c, format_phone_display(ph.value) if ph else "—",
                                 c.owner.name if c.owner else "غير مسند", last,
                                 m.offer.service_name if m.offer else "—",
                                 m.campaign.name if m.campaign else "—"))
    rep.manual = manual_offers(s, user, f)
    rep.total_offers = len(rep.rows) + len(rep.manual)
    rep.unique_companies = len({r.company.id for r in rep.rows} | {c.id for c in rep.manual})
    return rep


def manual_offers(s: Session, user: User, f: OfferFilters) -> list[Company]:
    """المنشآت التي سجّل موظف أنه أرسل لها عرضًا بنفسه: المنشأة والموظف وتاريخ الإرسال المسجّل."""
    q = scope_companies(select(Company), user).where(Company.offer_sent_at.isnot(None), visible(s))
    if f.date_from:
        q = q.where(Company.offer_sent_at >= riyadh_day_bounds_utc(f.date_from)[0])
    if f.date_to:
        q = q.where(Company.offer_sent_at < riyadh_day_bounds_utc(f.date_to)[1])
    if f.city:
        q = q.where(Company.city == f.city)
    if f.sector:
        q = q.where(Company.sector_key == f.sector)
    if f.campaign_id:
        q = q.where(Company.campaign_id == f.campaign_id)
    if f.owner_id:
        q = q.where((Company.owner_id == f.owner_id) | (Company.offer_sent_by_id == f.owner_id))
    if f.source:
        q = q.where(Company.lead_source.like(f"{f.source}%"))
    if f.q:
        q = q.where(Company.name.like(f"%{f.q}%"))
    if f.status or f.offer_id or f.reply:  # مرشحات خاصة بالبريد الآلي
        return []
    return list(s.scalars(q.order_by(Company.offer_sent_at.desc())))


def needs_followup_today(s: Session, user: User, day: date | None = None) -> list[dict]:
    day = day or riyadh_today()
    _, end = riyadh_day_bounds_utc(day)
    q = (select(Task).join(Company, Company.id == Task.company_id)
         .where(Task.status == "open", Task.due_at < end, visible(s)))
    if user is not None and user.role == "rep":
        q = q.where(Task.owner_id == user.id)
    out = []
    for t in s.scalars(q.order_by(Task.due_at)):
        c = t.company
        last = s.scalar(select(InboundMessage).where(InboundMessage.company_id == c.id,
                                                     InboundMessage.message_type == "reply")
                        .order_by(InboundMessage.received_at.desc()))
        last_offer = s.scalar(select(OutboundMessage).where(OutboundMessage.company_id == c.id,
                                                            OutboundMessage.kind == "offer")
                              .order_by(OutboundMessage.id.desc()))
        ph = c.primary("phone")
        from .models import CompanySignal
        sig = s.scalar(select(CompanySignal).where(CompanySignal.company_id == c.id)
                       .order_by(CompanySignal.announced_on.desc()))
        out.append({"task": t, "company": c, "phone": format_phone_display(ph.value) if ph else "—", "signal": sig,
                    "last_reply": last, "owner": t.owner.name if t.owner else "غير مسند",
                    "offer": last_offer, "overdue": t.due_at < riyadh_day_bounds_utc(day)[0]})
    return out


def export_offers_xlsx(rep: OffersReport) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "العروض المرسلة"
    ws.sheet_view.rightToLeft = True
    ws.append([f"عدد الشركات الفريدة: {rep.unique_companies}", f"إجمالي العروض المرسلة: {rep.total_offers}"])
    ws.append([])
    headers = ["الشركة", "النشاط", "المدينة", "رقم التواصل", "بريد المرسل", "بريد المستلم", "بريد الشركة",
               "تاريخ ووقت الإرسال (الرياض)", "الخدمة/العرض", "الحملة", "عنوان الرسالة", "نص الرسالة", "المرفق",
               "حالة البريد", "سبب الارتداد", "تاريخ آخر رد", "نص آخر رد", "نتيجة الرد", "المندوب المسؤول",
               "آخر إجراء", "الخطوة القادمة", "موعد الخطوة القادمة"]
    ws.append(headers)
    for cell in ws[3]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0F766E")
    for r in rep.rows:
        m, c = r.msg, r.company
        ws.append([c.name, c.activity, c.city, r.phone, m.from_address, m.to_address, m.intended_to, fmt(m.sent_at),
                   r.offer_name, r.campaign_name, m.subject, m.body_text, m.attachment_name or "",
                   STATUS_LABELS.get(m.status, m.status), m.bounce_reason or "",
                   fmt(r.last_reply.received_at) if r.last_reply else "",
                   (r.last_reply.body_text[:500] if r.last_reply else ""),
                   (r.last_reply.intent_label if r.last_reply else "لا يوجد رد"), r.owner_name,
                   c.last_action or "", c.next_action or "", fmt(c.next_action_at) if c.next_action_at else ""])
    for col, w in zip("ABCDEFGHIJKLMNOPQRSTUV", [28, 20, 12, 18, 26, 26, 26, 26, 24, 24, 34, 60, 22, 14, 30, 24,
                                                 50, 16, 18, 30, 26, 24]):
        ws.column_dimensions[col].width = w
    if rep.manual:
        wm = wb.create_sheet("عروض أرسلها الموظفون")
        wm.sheet_view.rightToLeft = True
        wm.append(["المنشأة", "النشاط", "المدينة", "الموظف الذي أرسل", "تاريخ الإرسال المسجّل (الرياض)", "العرض",
                   "الموظف المسؤول", "الحالة الحالية", "الهاتف", "البريد"])
        for c in rep.manual:
            ph, em = c.primary("phone"), c.primary("email")
            wm.append([c.name, c.activity, c.city, c.offer_sent_by.name if c.offer_sent_by else "", fmt(c.offer_sent_at),
                       c.offer_note or "", c.owner.name if c.owner else "", STAGE_LABELS.get(c.stage, c.stage),
                       format_phone_display(ph.value) if ph else "", em.value if em else ""])
        for col, w in zip("ABCDEFGHIJ", [30, 22, 12, 18, 24, 30, 18, 16, 18, 28]):
            wm.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=4):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top", horizontal="right")
    from .security import harden_sheet
    harden_sheet(wb)  # لا صيغ: بيانات المواقع الخارجية تُكتب نصًا
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def company_timeline(s: Session, company_id: int) -> list[Activity]:
    return list(s.scalars(select(Activity).where(Activity.company_id == company_id).order_by(Activity.at.desc())))


# ---------------------------------------------------------------- لوحة الإدارة

def dashboard(s: Session, user: User, day: date | None = None) -> dict:
    day = day or riyadh_today()
    start, end = riyadh_day_bounds_utc(day)
    cq = lambda *w: s.scalar(scope_companies(select(func.count(Company.id)), user)  # noqa: E731
                             .where(visible(s), *w)) or 0
    target = s.scalar(select(func.coalesce(func.sum(Campaign.daily_target), 0)).where(Campaign.status == "active")) or 0
    runs = list(s.scalars(select(DiscoveryRun).where(DiscoveryRun.run_date == day)))
    complete_today = cq(Company.counted_on == day, Company.data_status == "complete")
    msgs = lambda *w: s.scalar(select(func.count(OutboundMessage.id)).join(Company, Company.id == OutboundMessage.company_id)  # noqa: E731
                               .where(OutboundMessage.kind == "offer", visible(s), *w)
                               .where(Company.owner_id == user.id if user and user.role == "rep" else true())) or 0
    sent_today = msgs(OutboundMessage.sent_at >= start, OutboundMessage.sent_at < end)
    delivered_today = msgs(OutboundMessage.delivered_at >= start, OutboundMessage.delivered_at < end)
    bounced_today = msgs(OutboundMessage.bounced_at >= start, OutboundMessage.bounced_at < end)
    replies_today = s.scalar(select(func.count(InboundMessage.id)).where(
        InboundMessage.message_type == "reply", InboundMessage.received_at >= start,
        InboundMessage.received_at < end, InboundMessage.company_id.in_(visible_ids(s)))) or 0
    calls_answered = s.scalar(select(func.count(Call.id)).where(Call.answered.is_(True), Call.created_at >= start,
                                                                Call.created_at < end, Call.company_id.in_(visible_ids(s)))) or 0
    meetings_today = s.scalar(select(func.count(Meeting.id)).where(Meeting.created_at >= start, Meeting.created_at < end,
                                                                   Meeting.company_id.in_(visible_ids(s)))) or 0
    eligible = cq(Company.data_status == "complete", Company.stage == "new", Company.do_not_contact.is_(False))
    return {
        "day": day, "target": target, "complete_today": complete_today,
        "incomplete_total": cq(Company.data_status == "incomplete"),
        "incomplete_today": cq(Company.data_status == "incomplete", Company.first_found_at >= start,
                               Company.first_found_at < end),
        "duplicates_today": sum(r.duplicate_count or 0 for r in runs),
        "eligible": eligible, "sent_today": sent_today, "delivered_today": delivered_today,
        "bounced_today": bounced_today, "replies_today": replies_today, "calls_answered": calls_answered,
        "meetings_today": meetings_today,
        "won_total": cq(Company.stage == "won"),
        "totals": {
            "companies": cq(Company.data_status == "complete"),
            "sent": msgs(OutboundMessage.status.in_(["sent", "delivered", "bounced"])),
            "delivered": msgs(OutboundMessage.status == "delivered"),
            "replies": s.scalar(select(func.count(func.distinct(InboundMessage.company_id))).where(
                InboundMessage.message_type == "reply", InboundMessage.company_id.in_(visible_ids(s)))) or 0,
            "meetings": s.scalar(select(func.count(Meeting.id)).where(Meeting.company_id.in_(visible_ids(s)))) or 0,
            "won": cq(Company.stage == "won"),
        },
        "runs": runs,
        "tasks_today": len(needs_followup_today(s, user, day)),
    }


def daily_report(s: Session, day: date | None = None) -> dict:
    day = day or riyadh_today()
    start, end = riyadh_day_bounds_utc(day)
    # القطاعات والعروض التي جلبت اجتماعات وصفقات (تراكميًا مع أرقام اليوم)
    by_sector = {}
    for c in s.scalars(select(Company).where(visible(s), Company.stage.in_(["contacted", "offer_sent", "needs_followup", "replied",
                                                                            "interested", "meeting", "won", "lost", "do_not_contact"]))):
        key = SECTORS.get(c.sector_key, {}).get("label", c.sector_key or "—")
        d = by_sector.setdefault(key, {"contacted": 0, "replied": 0, "meetings": 0, "won": 0})
        d["contacted"] += 1
        if c.stage in ("replied", "interested", "meeting", "won"):
            d["replied"] += 1
        if c.stage in ("meeting", "won"):
            d["meetings"] += 1
        if c.stage == "won":
            d["won"] += 1
    by_offer = {}
    for m in s.scalars(select(OutboundMessage).where(OutboundMessage.kind == "offer", OutboundMessage.company_id.in_(visible_ids(s)),
                                                     OutboundMessage.status.in_(["sent", "delivered", "bounced"]))):
        key = m.offer.service_name if m.offer else "—"
        d = by_offer.setdefault(key, {"sent": 0, "replies": 0, "meetings": 0, "won": 0, "_companies": set()})
        d["sent"] += 1
        if m.replied_at:
            d["replies"] += 1
        if m.company.stage in ("meeting", "won") and m.company_id not in d["_companies"]:
            d["meetings"] += 1
            d["won"] += 1 if m.company.stage == "won" else 0
        d["_companies"].add(m.company_id)
    for d in by_offer.values():
        d.pop("_companies")
    # المصادر
    by_source: dict[str, dict] = {}
    for r in s.scalars(select(DiscoveryRun).where(DiscoveryRun.run_date == day)):
        for src, v in (r.per_source or {}).items():
            d = by_source.setdefault(src, {"candidates": 0, "complete_count": 0, "incomplete_count": 0,
                                           "duplicate_count": 0, "rejected_count": 0})
            for k in d:
                d[k] += v.get(k, 0)
    complete = s.scalar(select(func.count(Company.id)).where(Company.counted_on == day, visible(s),
                                                             Company.data_status == "complete")) or 0
    costs = {svc: round(v or 0, 2) for svc, v in s.execute(
        select(CostEntry.service, func.sum(CostEntry.cost_usd)).where(CostEntry.day == day)
        .group_by(CostEntry.service))}
    total_cost = round(sum(costs.values()), 2)
    tomorrow = day + timedelta(days=1)
    _, tend = riyadh_day_bounds_utc(tomorrow)
    actions = list(s.scalars(select(Task).where(Task.status == "open", Task.due_at < tend,
                                                Task.company_id.in_(visible_ids(s))).order_by(Task.due_at)))
    runs = list(s.scalars(select(DiscoveryRun).where(DiscoveryRun.run_date == day)))
    return {"day": day, "by_sector": by_sector, "by_offer": by_offer, "by_source": by_source,
            "complete": complete, "costs": costs, "total_cost": total_cost,
            "cost_per_lead": round(total_cost / complete, 3) if complete else None,
            "actions": actions, "runs": runs}


# ---------------------------------------------------------------- التقرير الأسبوعي للشرائح

INTERESTED_INTENTS = ("interested", "callback_request", "meeting_request", "quote_request", "details_request")


def week_bounds(day: date) -> tuple[date, date]:
    """الأسبوع من الأحد إلى السبت (بتوقيت الرياض)."""
    start = day - timedelta(days=(day.weekday() + 1) % 7)
    return start, start + timedelta(days=7)


def weekly_segments(s: Session, day: date | None = None) -> dict:
    """لكل شريحة: الشركات التي تواصلنا معها أول مرة هذا الأسبوع (دفعة الأسبوع)، وما نتج عنها حتى الآن."""
    from .leads import source_label
    day = day or riyadh_today()
    start, end = week_bounds(day)
    t0, _ = riyadh_day_bounds_utc(start)
    t1, _ = riyadh_day_bounds_utc(end)
    first_sent = (select(OutboundMessage.company_id, func.min(OutboundMessage.sent_at).label("first"))
                  .where(OutboundMessage.kind == "offer", OutboundMessage.sent_at.isnot(None))
                  .group_by(OutboundMessage.company_id).subquery())
    cohort = list(s.scalars(select(Company).join(first_sent, first_sent.c.company_id == Company.id)
                            .where(first_sent.c.first >= t0, first_sent.c.first < t1, visible(s))))
    rows: dict[str, dict] = {}
    for c in cohort:
        key = c.sector_key or "—"
        r = rows.setdefault(key, {"label": SECTORS.get(key, {}).get("label", key), "experiment": False,
                                  "contacted": 0, "interested": 0, "meetings": 0, "won": 0})
        r["contacted"] += 1
        if c.category is not None and c.category.is_experiment:
            r["experiment"] = True
        if s.scalar(select(InboundMessage.id).where(InboundMessage.company_id == c.id,
                                                    InboundMessage.intent.in_(INTERESTED_INTENTS)).limit(1)):
            r["interested"] += 1
        if s.scalar(select(Meeting.id).where(Meeting.company_id == c.id).limit(1)):
            r["meetings"] += 1
        if c.stage == "won":
            r["won"] += 1
    for r in rows.values():
        n = r["contacted"]
        r["meetings_per_100"] = round(100 * r["meetings"] / n, 1) if n else 0.0
        r["interested_per_100"] = round(100 * r["interested"] / n, 1) if n else 0.0
        r["small_sample"] = n < 30
    ordered = sorted(rows.items(), key=lambda kv: (-kv[1]["meetings_per_100"], -kv[1]["contacted"]))
    # الطلبات الواردة حسب المصدر
    from .models import InboundLead
    leads = {}
    for lead in s.scalars(select(InboundLead).where(InboundLead.created_at >= t0, InboundLead.created_at < t1)):
        lbl = source_label(lead.source)
        leads[lbl] = leads.get(lbl, 0) + 1
    # حصص الاختبار الحالية
    split = {"core": 0, "experiment": 0}
    for cat in s.scalars(select(Category).join(Campaign).where(Campaign.status == "active", Category.active.is_(True))):
        split["experiment" if cat.is_experiment else "core"] += cat.quota or 0
    totals = {k: sum(r[k] for r in rows.values()) for k in ("contacted", "interested", "meetings", "won")}
    totals["meetings_per_100"] = round(100 * totals["meetings"] / totals["contacted"], 1) if totals["contacted"] else 0.0
    return {"start": start, "end": end - timedelta(days=1), "rows": ordered, "leads": leads, "split": split,
            "totals": totals}



# ---------------------------------------------------------------- عملاء اليوم

def customers_in_period(s: Session, user: User, date_from: date, date_to: date,
                        campaign_id: int | None = None) -> dict:
    """«عملاء اليوم» لأي يوم أو فترة: العملاء الذين **جُمعت** بياناتهم بين بداية يوم البداية ونهاية يوم النهاية
    بتوقيت الرياض (شاملًا اليومين). استعراض لما جُمع سابقًا فقط — لا يشغّل بحثًا ولا يحتسب تكلفة جديدة."""
    from datetime import timedelta
    start, _ = riyadh_day_bounds_utc(date_from)
    _, end = riyadh_day_bounds_utc(date_to)
    q = scope_companies(select(Company), user).where(
        Company.first_found_at >= start, Company.first_found_at < end,
        Company.data_status.in_(["complete", "incomplete"]), visible(s))
    if campaign_id:
        q = q.where(Company.campaign_id == campaign_id)
    rows = []
    for c in s.scalars(q.order_by(Company.data_status, Company.fit_score.desc(), Company.id)):
        ph, em = c.primary("phone"), c.primary("email")
        links = []
        for u in [c.fit_evidence_url] + [ct.source_url for ct in c.contacts if ct.verified] + [x.url for x in c.sources]:
            if u and u.startswith("http") and u not in links:
                links.append(u)
        rows.append({"c": c, "phone": ph, "email": em, "links": links[:6],
                     "complete": c.data_status == "complete" and c.parent_id is None,
                     "branch": c.parent_id is not None})
    runs_q = select(DiscoveryRun).where(DiscoveryRun.run_date >= date_from, DiscoveryRun.run_date <= date_to)
    if campaign_id:
        runs_q = runs_q.where(DiscoveryRun.campaign_id == campaign_id)
    runs = list(s.scalars(runs_q.order_by(DiscoveryRun.run_date)))
    days = (date_to - date_from).days + 1
    target_q = select(func.coalesce(func.sum(Campaign.daily_target), 0)).where(Campaign.status == "active")
    if campaign_id:
        target_q = select(Campaign.daily_target).where(Campaign.id == campaign_id)
    complete = sum(1 for r in rows if r["complete"])
    cost_q = select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(CostEntry.day >= date_from,
                                                                          CostEntry.day <= date_to)
    if campaign_id:
        cost_q = cost_q.where(CostEntry.campaign_id == campaign_id)
    cost = round(float(s.scalar(cost_q) or 0), 2)
    unattributed = 0.0
    if campaign_id:  # تكلفة سُجلت قبل نسب التكلفة للحملات: تُعرض منفصلة بوضوح لا تُنسب لحملة بعينها
        unattributed = round(float(s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(
            CostEntry.day >= date_from, CostEntry.day <= date_to, CostEntry.campaign_id.is_(None))) or 0), 2)
    today = riyadh_today()
    return {"day": date_from, "date_from": date_from, "date_to": date_to, "days": days, "rows": rows,
            "target": (s.scalar(target_q) or 0) * days, "complete": complete,
            "incomplete": sum(1 for r in rows if r["c"].data_status == "incomplete"),
            "duplicates": sum(r.duplicate_count or 0 for r in runs), "rejected": sum(r.rejected_count or 0 for r in runs),
            "runs": runs, "running": date_from <= today <= date_to and any(r.status == "running" for r in runs),
            "cost": cost, "cost_unattributed": unattributed,
            "cost_per": round(cost / complete, 3) if complete else None}


def todays_customers(s: Session, user: User, day: date | None = None, campaign_id: int | None = None) -> dict:
    day = day or riyadh_today()
    return customers_in_period(s, user, day, day, campaign_id)


def export_today_xlsx(t: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "عملاء اليوم"
    ws.sheet_view.rightToLeft = True
    period = str(t["date_from"]) if t["date_from"] == t["date_to"] else f"من {t['date_from']} إلى {t['date_to']}"
    ws.append([f"العملاء حسب تاريخ الجمع — {period}", f"مكتملون: {t['complete']} من {t['target']}",
               f"قيد الاستكمال: {t['incomplete']}", f"مكررات مستبعدة: {t['duplicates']}"])
    ws.append([])
    ws.append(["اسم العميل", "نوع النشاط", "المدينة", "الهاتف", "مصدر الهاتف", "البريد", "مصدر البريد",
               "سبب الاستهداف", "رابط الدليل", "مشغّل المركبات", "الحالة", "روابط المصادر", "تاريخ الجمع", "المسؤول",
               "مؤشر استخدام المركبات", "نوع الجهة", "القطاع"])
    for cell in ws[3]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="6941C6")
    from .activities import ENTITY_TYPES
    from .companies import OPERATOR_LABELS
    for r in t["rows"]:
        c = r["c"]
        ws.append([c.name, c.activity, c.city, format_phone_display(r["phone"].value) if r["phone"] else "",
                   r["phone"].source_url if r["phone"] else "", r["email"].value if r["email"] else "",
                   r["email"].source_url if r["email"] else "", c.fit_reason, c.fit_evidence_url or "",
                   OPERATOR_LABELS.get(c.operator_status, ""), "مكتمل" if r["complete"] else
                   ("فرع" if r["branch"] else "قيد الاستكمال"), "\n".join(r["links"]),
                   fmt(c.first_found_at), c.owner.name if c.owner else "",
                   "مؤكد" if c.vehicle_indicator == "confirmed" or (not c.vehicle_indicator and c.fit_phrase) else "غير مؤكد",
                   ENTITY_TYPES.get(c.entity_type or "", c.entity_type or ""), c.category.label if c.category else ""])
    for col, w in zip("ABCDEFGHIJKLMNOPQ", [30, 22, 12, 18, 40, 30, 40, 60, 40, 20, 14, 60, 20, 16, 16, 16, 22]):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=4):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top", horizontal="right")
    from .security import harden_sheet
    harden_sheet(wb)  # لا صيغ: بيانات المواقع الخارجية تُكتب نصًا
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
