"""حجز الاجتماعات: التحقق من توفر الموعد في تقويم المندوب، تسجيل الحجز، وإرسال التأكيد للعميل."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .companies import log_activity
from .mail import get_provider
from .models import Company, Meeting, OutboundMessage, Task, User
from .outreach import active_account, render_html, render_text
from .timeutil import fmt, now_utc, riyadh_to_utc, to_riyadh


class SlotUnavailable(Exception):
    def __init__(self, suggestions: list[datetime]):
        super().__init__("الموعد غير متاح في تقويم المندوب")
        self.suggestions = suggestions


def _overlaps(start, end, busy):
    return any(b0 < end and b1 > start for b0, b1 in busy)


def suggest_slots(provider, cal: str, around: datetime, duration: int = 30, n: int = 3) -> list[datetime]:
    out = []
    local = to_riyadh(around)
    day = local.replace(hour=9, minute=0, second=0, microsecond=0)
    busy = provider.busy_slots(cal, riyadh_to_utc(day.replace(tzinfo=None)),
                               riyadh_to_utc((day + timedelta(days=7)).replace(tzinfo=None)))
    cur = max(local.replace(tzinfo=None), day.replace(tzinfo=None))
    for _ in range(7 * 18):
        if cur.weekday() not in (4, 5) and 9 <= cur.hour < 17:
            s = riyadh_to_utc(cur)
            e = s + timedelta(minutes=duration)
            if s > now_utc() and not _overlaps(s, e, busy):
                out.append(s)
                if len(out) >= n:
                    break
        cur += timedelta(minutes=30)
    return out


def book_meeting(s: Session, company: Company, owner: User, start_utc: datetime, *, duration: int = 30,
                 attendee_email: str | None = None, booked_by: User | None = None) -> Meeting:
    acct = active_account(s)
    if acct is None:
        raise RuntimeError("اربط بريد الشركة أولًا")
    provider = get_provider(acct)
    cal = owner.calendar_email or owner.email
    end = start_utc + timedelta(minutes=duration)
    busy = provider.busy_slots(cal, start_utc - timedelta(hours=1), end + timedelta(hours=1))
    if _overlaps(start_utc, end, busy):
        raise SlotUnavailable(suggest_slots(provider, cal, start_utc, duration))
    attendee = attendee_email or (company.primary("email").value if company.primary("email") else None)
    subject = f"اجتماع تعريفي: سيارة اب × {company.name}"
    event_id = provider.create_event(cal, attendee, start_utc, end, subject,
                                     f"<p>اجتماع تعريفي مع {owner.name} من سيارة اب.</p>")
    mtg = Meeting(company_id=company.id, owner_id=owner.id, starts_at=start_utc, ends_at=end,
                  attendee_email=attendee, calendar_event_id=event_id)
    s.add(mtg)
    s.flush()
    if attendee:
        body = (f"السلام عليكم فريق {company.name}،\n\nنؤكد موعد الاجتماع التعريفي مع {owner.name} "
                f"يوم {fmt(start_utc)} بتوقيت الرياض لمدة {duration} دقيقة.\n\n"
                f"للتواصل المباشر: {owner.phone or acct.sender_address}")
        last = s.scalar(select(OutboundMessage).where(OutboundMessage.company_id == company.id,
                                                      OutboundMessage.kind == "offer")
                        .order_by(OutboundMessage.id.desc()))
        conf = OutboundMessage(company_id=company.id, campaign_id=company.campaign_id, kind="confirmation",
                               parent_id=last.id if last else None, dedupe_key=f"meeting:{mtg.id}",
                               mail_account_id=acct.id, from_address=acct.sender_address, to_address=attendee,
                               intended_to=attendee, subject=subject,
                               body_text=render_text(body, acct.signature_html),
                               body_html=render_html(body, acct.signature_html))
        s.add(conf)
        s.flush()
        mtg.confirmation_message_id = conf.id
    company.stage = "meeting"
    company.next_action, company.next_action_at = "عقد الاجتماع التعريفي", start_utc
    for t in s.scalars(select(Task).where(Task.company_id == company.id, Task.status == "open",
                                          Task.kind.in_(["meeting", "call"]))):
        t.status, t.done_at = "done", now_utc()
    log_activity(s, company.id, "meeting", f"حُجز اجتماع: {fmt(start_utc)}",
                 f"المندوب: {owner.name} — حدث التقويم: {event_id}",
                 user_id=booked_by.id if booked_by else None, ref_type="meeting", ref_id=mtg.id)
    return mtg
