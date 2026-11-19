"""مزامنة صندوق الردود: حالات التسليم والارتداد، ربط الرد بالعرض الأصلي، إيقاف المتابعة الآلية،
تسجيل طلبات الإيقاف فورًا، وإنشاء مهمة وتنبيه للمندوب المسؤول بالموعد المطلوب."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .assignment import assign_owner
from .companies import log_activity
from .db import session_scope
from .mail import get_provider
from .mail.base import InboundEmail
from .models import (Company, Consent, Contact, InboundMessage, MailAccount, Notification, OutboundMessage,
                     Suppression, Task, User)
from .normalize import format_phone_display, normalize_email
from .replyparse import parse_reply, strip_quoted
from .timeutil import fmt, now_utc, riyadh_to_utc, to_riyadh

log = logging.getLogger(__name__)


def sync_all() -> dict:
    stats = {"processed": 0, "replies": 0, "delivered": 0, "bounced": 0, "errors": 0}
    with session_scope() as s:
        accounts = [a.id for a in s.scalars(select(MailAccount).where(MailAccount.active.is_(True)))]
    for aid in accounts:
        with session_scope() as s:
            acct = s.get(MailAccount, aid)
            try:
                items, cursor = get_provider(acct).fetch_new(acct.sync_cursor or {})
            except Exception as e:
                log.exception("inbox sync failed")
                stats["errors"] += 1
                continue
            acct.sync_cursor = cursor
            acct.last_sync_at = now_utc()
        for item in items:
            with session_scope() as s:
                r = process_inbound(s, item)
                stats["processed"] += 1
                if r in stats:
                    stats[r] += 1
    return stats


def _find_outbound(s: Session, ids: list[str], thread_id: str | None) -> OutboundMessage | None:
    ids = [i for i in ids if i]
    if ids:
        m = s.scalar(select(OutboundMessage).where(OutboundMessage.internet_message_id.in_(ids))
                     .order_by(OutboundMessage.id.desc()))
        if m:
            return m
    if thread_id:
        return s.scalar(select(OutboundMessage).where(OutboundMessage.thread_id == thread_id)
                        .order_by(OutboundMessage.id.desc()))
    return None


def process_inbound(s: Session, item: InboundEmail) -> str:
    if s.scalar(select(InboundMessage.id).where(InboundMessage.provider_message_id == item.provider_id)):
        return "duplicate"
    rec = InboundMessage(provider_message_id=item.provider_id, internet_message_id=item.internet_message_id,
                         in_reply_to=item.in_reply_to, thread_id=item.thread_id, from_address=item.from_addr,
                         subject=item.subject[:300], body_text=item.text[:20000], received_at=item.received_at)
    s.add(rec)

    # 1) تقارير التسليم والارتداد
    if item.report_type:
        rec.message_type = "delivery_receipt" if item.report_type == "delivered" else "bounce"
        out = _find_outbound(s, [item.report_original_message_id, item.in_reply_to], item.thread_id)
        rec.outbound_id = out.id if out else None
        rec.company_id = out.company_id if out else None
        rec.processed = True
        if not out:
            return "processed"
        if item.report_type == "delivered":
            if out.status in ("sent", "queued", "sending"):
                out.status = "delivered"
                out.delivered_at = item.received_at
                log_activity(s, out.company_id, "email_delivered", f"تم التسليم: {out.subject}",
                             ref_type="outbound", ref_id=out.id, at=item.received_at)
            return "delivered"
        out.status = "bounced"
        out.bounced_at = item.received_at
        out.bounce_reason = item.report_reason or "ارتد البريد دون سبب محدد"
        email = normalize_email(out.to_address)
        if email and out.to_address == out.intended_to:
            s.add(Suppression(kind="email", value=email, channel="email", reason="ارتداد البريد",
                              source="bounce", company_id=out.company_id))
            ct = s.scalar(select(Contact).where(Contact.kind == "email", Contact.value == email))
            if ct:
                ct.verified = False
                ct.preferred = False
        log_activity(s, out.company_id, "email_bounced", f"ارتد البريد: {out.subject}",
                     out.bounce_reason, ref_type="outbound", ref_id=out.id, at=item.received_at)
        return "bounced"

    # 2) الردود
    out = _find_outbound(s, [item.in_reply_to, *item.references], item.thread_id)
    if out is None:
        sender = normalize_email(item.from_addr)
        ct = s.scalar(select(Contact).where(Contact.kind == "email", Contact.value == sender)) if sender else None
        if ct:
            out = s.scalar(select(OutboundMessage).where(OutboundMessage.company_id == ct.company_id)
                           .order_by(OutboundMessage.id.desc()))
    if out is None:
        rec.message_type = "unrelated"
        rec.processed = True
        return "processed"
    company = out.company
    rec.outbound_id, rec.company_id = out.id, company.id
    if item.auto_reply:
        rec.message_type, rec.intent, rec.intent_label = "auto_reply", "out_of_office", "رد تلقائي"
        rec.processed = True
        log_activity(s, company.id, "auto_reply", "رد تلقائي من العميل", strip_quoted(item.text)[:500],
                     ref_type="inbound", ref_id=None, at=item.received_at)
        return "processed"

    rec.message_type = "reply"
    parsed = parse_reply(item.text, item.received_at)
    rec.intent, rec.intent_label = parsed.intent, parsed.label
    rec.requested_at, rec.requested_window, rec.summary = parsed.requested_at, parsed.window_label, parsed.summary
    s.flush()

    # إيقاف المتابعة الآلية لكل رسائل هذه الشركة
    for m in s.scalars(select(OutboundMessage).where(OutboundMessage.company_id == company.id)):
        m.followups_stopped = True
        if m.id == out.id and not m.replied_at:
            m.replied_at = item.received_at
        if m.status == "queued" and m.kind == "followup":
            m.status, m.status_detail = "skipped", "توقفت المتابعة الآلية بعد رد العميل"
    reply_text = strip_quoted(item.text)
    log_activity(s, company.id, "reply", f"رد العميل ({parsed.label})", reply_text[:2000],
                 ref_type="inbound", ref_id=rec.id, at=item.received_at)
    company.last_action, company.last_action_at = f"رد العميل: {parsed.label}", item.received_at

    if parsed.intent == "unsubscribe":
        apply_opt_out(s, company, source="email_reply", detail=reply_text[:300], email=normalize_email(item.from_addr))
        rec.processed = True
        return "replies"
    if parsed.intent == "not_interested":
        company.stage = "lost"
        company.next_action, company.next_action_at = None, None
        rec.processed = True
        return "replies"

    owner = company.owner or assign_owner(s, company)
    company.stage = "interested" if parsed.intent != "other" else "replied"
    due = parsed.requested_at or _next_business_morning(item.received_at)
    action = {
        "callback_request": "الاتصال بالعميل", "meeting_request": "حجز اجتماع مع العميل",
        "quote_request": "إعداد وإرسال عرض سعر", "details_request": "إرسال التفاصيل المطلوبة",
        "interested": "الاتصال بالعميل المهتم", "out_of_office": "إعادة المحاولة بعد عودته",
    }.get(parsed.intent, "مراجعة رد العميل")
    phone = company.primary("phone")
    task = Task(company_id=company.id, owner_id=owner.id if owner else None,
                kind="meeting" if parsed.intent == "meeting_request" else
                "quote" if parsed.intent == "quote_request" else "call",
                title=f"{action} — {company.name}",
                details=(f"الهاتف: {format_phone_display(phone.value) if phone else 'غير متوفر'}\n"
                         f"الموعد المطلوب: {parsed.window_label or 'لم يحدد (أقرب وقت)'}\n"
                         f"العرض المرسل: {out.subject}\nنص الرد:\n{reply_text[:1500]}"),
                due_at=due, window_label=parsed.window_label, source_inbound_id=rec.id)
    s.add(task)
    company.next_action, company.next_action_at = action, due
    s.flush()
    log_activity(s, company.id, "task", f"أُنشئت مهمة: {task.title}",
                 f"الموعد: {fmt(due)}" + (f" — المسؤول: {owner.name}" if owner else ""), ref_type="task",
                 ref_id=task.id)
    if owner:
        notify(s, owner, company, f"عميل يحتاج متابعة: {company.name}",
               f"الشركة: {company.name}\nالهاتف: {format_phone_display(phone.value) if phone else '—'}\n"
               f"نوع الرد: {parsed.label}\nالموعد المطلوب: {parsed.window_label or fmt(due)}\n\nنص الرد:\n{reply_text[:800]}")
    rec.processed = True
    return "replies"


def _next_business_morning(received: datetime) -> datetime:
    r = to_riyadh(received) + timedelta(days=1)
    while r.weekday() in (4, 5):
        r += timedelta(days=1)
    return riyadh_to_utc(datetime(r.year, r.month, r.day, 10, 0))


def apply_opt_out(s: Session, company: Company, *, source: str, detail: str = "", email: str | None = None,
                  channel: str = "all") -> None:
    """يسجل طلب إيقاف التواصل فورًا ويستبعد بيانات الشركة من الحملات اللاحقة."""
    company.do_not_contact = channel == "all" or company.do_not_contact
    if channel == "all":
        company.stage = "do_not_contact"
    for kind, value in [("company", str(company.id)), ("email", email)] + \
            [(c.kind, c.value) for c in company.contacts if c.verified]:
        if not value:
            continue
        exists = s.scalar(select(Suppression.id).where(Suppression.kind == kind, Suppression.value == value,
                                                       Suppression.channel == channel))
        if not exists:
            s.add(Suppression(kind=kind, value=value, channel=channel, reason="طلب العميل إيقاف التواصل",
                              source=source, company_id=company.id))
    s.add(Consent(company_id=company.id, channel="email" if channel == "email" else "call" if channel == "call" else "email",
                  basis="opt_out", granted=False, evidence=detail[:500]))
    if channel == "all":
        s.add(Consent(company_id=company.id, channel="call", basis="opt_out", granted=False, evidence=detail[:500]))
    for m in s.scalars(select(OutboundMessage).where(OutboundMessage.company_id == company.id,
                                                     OutboundMessage.status == "queued")):
        m.status, m.status_detail = "skipped", "طلب العميل إيقاف التواصل"
    for t in s.scalars(select(Task).where(Task.company_id == company.id, Task.status == "open")):
        t.status = "cancelled"
    company.next_action, company.next_action_at = None, None
    log_activity(s, company.id, "opt_out", "سُجّل طلب إيقاف التواصل", detail)


def notify(s: Session, user: User, company: Company | None, title: str, body: str) -> None:
    n = Notification(user_id=user.id, company_id=company.id if company else None, title=title, body=body)
    s.add(n)
    s.flush()
    # نسخة بالبريد للمندوب من بريد الشركة (أفضل جهد)
    from .mail.base import OutgoingEmail
    from .outreach import active_account
    acct = active_account(s)
    if acct and user.email and not user.email.endswith(".local"):
        try:
            import html as _h
            get_provider(acct).send(OutgoingEmail(
                from_addr=acct.sender_address, from_name="مندوب سيارة اب الذكي", to=user.email,
                subject=title, text=body,
                html=f"<div dir='rtl' style='font-family:Tahoma'>{_h.escape(body).replace(chr(10), '<br>')}</div>"))
            n.emailed = True
        except Exception:
            log.warning("notify email failed", exc_info=True)
