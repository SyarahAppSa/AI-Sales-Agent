"""إرسال العروض: الأهلية، كتابة الرسالة، الجدولة ضمن النافذة والحد اليومي، ومنع التكرار."""
from __future__ import annotations

import hashlib
import html
import logging
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import ai
from .companies import log_activity
from .config import settings
from .db import session_scope
from .mail import get_provider
from .mail.base import OutgoingEmail
from .models import (Campaign, Category, Company, Consent, Contact, MailAccount, Offer, OutboundMessage,
                     Suppression)
from .normalize import domain_of, is_offer_suitable_email
from .timeutil import now_riyadh, now_utc, riyadh_day_bounds_utc, riyadh_today

log = logging.getLogger(__name__)

OPT_OUT_LINE = "إذا لم ترغبوا في استلام رسائل منا، يكفي الرد بكلمة «إيقاف» وسنحترم طلبكم فورًا."


def dedupe_key(company_id: int, offer_key: str, kind: str = "offer") -> str:
    return f"company:{company_id}|offer:{offer_key}" + ("" if kind == "offer" else f"|{kind}")


# ---------------------------------------------------------------- الأهلية

def is_suppressed(s: Session, company: Company, email: str | None, channel: str = "email") -> str | None:
    conds = [(Suppression.kind == "company") & (Suppression.value == str(company.id))]
    if company.domain:
        conds.append((Suppression.kind == "domain") & (Suppression.value == company.domain))
    if email:
        conds.append((Suppression.kind == "email") & (Suppression.value == email))
        conds.append((Suppression.kind == "domain") & (Suppression.value == domain_of(email)))
    row = s.scalar(select(Suppression).where(or_(*conds), Suppression.channel.in_(["all", channel])).limit(1))
    return row.reason or "ضمن قائمة إيقاف التواصل" if row else None


def ensure_email_basis(s: Session, company: Company, contact: Contact) -> None:
    """يسجل أساس التواصل بالبريد: بريد أعمال منشور علنًا للتواصل التجاري، مع رابط المصدر."""
    exists = s.scalar(select(Consent.id).where(Consent.company_id == company.id, Consent.channel == "email"))
    if not exists:
        s.add(Consent(company_id=company.id, channel="email", basis="published_business_contact",
                      evidence=f"بريد أعمال منشور: {contact.value} — المصدر: {contact.source_url}"))


def check_eligibility(s: Session, company: Company, offer: Offer) -> tuple[bool, str]:
    if company.data_status != "complete":
        return False, "بيانات الشركة غير مكتملة"
    if company.do_not_contact or company.stage == "do_not_contact":
        return False, "طلبت الشركة إيقاف التواصل"
    if company.is_existing_customer:
        return False, "عميل حالي"
    if company.stage in ("replied", "interested", "meeting", "won", "lost"):
        return False, "الشركة في مرحلة متابعة يدوية"
    if not offer.approved:
        return False, "العرض غير معتمد"
    email = company.primary("email")
    if not email or not is_offer_suitable_email(email.value):
        return False, "لا يوجد بريد مناسب للعروض"
    why = is_suppressed(s, company, email.value, "email")
    if why:
        return False, why
    denied = s.scalar(select(Consent.id).where(Consent.company_id == company.id, Consent.channel == "email",
                                               Consent.granted.is_(False)))
    if denied:
        return False, "سُحبت الموافقة على التواصل بالبريد"
    if s.scalar(select(OutboundMessage.id).where(OutboundMessage.dedupe_key == dedupe_key(company.id, offer.key))):
        return False, "أُرسل هذا العرض للشركة سابقًا"
    return True, ""


# ---------------------------------------------------------------- كتابة الرسالة

COMPOSE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["subject", "body"],
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
}


def compose(company: Company, offer: Offer, sender_name: str) -> tuple[str, str]:
    """يعيد (العنوان، النص). يكتب رسالة قصيرة باسم الشركة وخدمة واحدة وخطوة واضحة."""
    subject = offer.subject_template.format(company=company.name, city=company.city, service=offer.service_name)
    if ai.settings.ai_enabled:
        prompt = (
            f"اكتب رسالة بريد عربية قصيرة (80–120 كلمة) من فريق {sender_name} إلى شركة «{company.name}» "
            f"({company.activity} في {company.city}).\n"
            f"الخدمة الوحيدة المعروضة: {offer.service_name}.\nالفوائد المعتمدة (لا تضف غيرها):\n{offer.value_points}\n"
            f"الأسعار المعتمدة (اذكرها فقط إن وُجدت): {offer.pricing_text or 'لا تذكر أسعارًا'}\n"
            f"سبب ترشيح الشركة: {company.fit_reason}\n"
            f"اربط فائدة واحدة بنشاطهم تحديدًا، واختم بهذه الخطوة حرفيًا: «{offer.call_to_action}».\n"
            "لا تخترع أرقامًا أو عملاء أو ادعاءات. لا تكتب التوقيع. ابدأ بتحية مهنية موجهة لفريق الشركة.\n"
            f"العنوان المقترح: {subject} (يمكنك تحسينه مع إبقاء اسم الشركة والخدمة).")
        try:
            out = ai.ai_json("أنت كاتب مبيعات B2B سعودي، أسلوبك مهني وواضح ومختصر.", prompt, COMPOSE_SCHEMA,
                             campaign_id=company.campaign_id, note="compose", max_tokens=2000)
            return out["subject"].strip()[:250], out["body"].strip()
        except ai.AIUnavailable as e:
            log.warning("compose fallback: %s", e)
    body = (offer.approved_body or "").format(company=company.name, city=company.city, activity=company.activity,
                                              service=offer.service_name)
    if offer.call_to_action and offer.call_to_action not in body:
        body = body.rstrip() + "\n\n" + offer.call_to_action
    return subject, body


def render_html(body: str, signature_html: str) -> str:
    paras = "".join(f"<p style='margin:0 0 12px'>{html.escape(p).replace(chr(10), '<br>')}</p>"
                    for p in body.split("\n\n") if p.strip())
    return (f"<div dir='rtl' style='font-family:Tahoma,Arial,sans-serif;font-size:15px;line-height:1.8;"
            f"color:#1f2937;text-align:right'>{paras}<div style='margin-top:18px'>{signature_html}</div>"
            f"<p style='margin-top:24px;font-size:12px;color:#6b7280'>{OPT_OUT_LINE}</p></div>")


def render_text(body: str, signature_html: str) -> str:
    import re
    sig = re.sub(r"<br\s*/?>", "\n", signature_html)
    sig = re.sub(r"<[^>]+>", "", sig)
    return f"{body}\n\n{html.unescape(sig).strip()}\n\n{OPT_OUT_LINE}"


# ---------------------------------------------------------------- الجدولة

def active_account(s: Session) -> MailAccount | None:
    return s.scalar(select(MailAccount).where(MailAccount.active.is_(True)).order_by(MailAccount.id))


def in_send_window(camp: Campaign) -> bool:
    now = now_riyadh()
    return now.weekday() in (camp.send_days or []) and camp.send_start_hour <= now.hour < camp.send_end_hour


def sent_today(s: Session, campaign_id: int) -> int:
    start, end = riyadh_day_bounds_utc(riyadh_today())
    return s.scalar(select(func.count(OutboundMessage.id)).where(
        OutboundMessage.campaign_id == campaign_id, OutboundMessage.kind == "offer",
        OutboundMessage.sent_at >= start, OutboundMessage.sent_at < end)) or 0


def offer_for(company: Company) -> Offer | None:
    if company.category and company.category.offer_id:
        return company.category.offer
    return None


def queue_offers(campaign_id: int, limit: int = 500) -> dict:
    """يجهّز رسائل العروض للشركات المؤهلة (تُكتب الرسالة الآن وتُحفظ كما ستُرسل)."""
    stats = {"queued": 0, "skipped": {}}
    with session_scope() as s:
        camp = s.get(Campaign, campaign_id)
        acct = active_account(s)
        if not camp or camp.status != "active" or not camp.send_enabled or not acct:
            return stats
        ids = [cid for (cid,) in s.execute(
            select(Company.id).where(Company.campaign_id == campaign_id, Company.data_status == "complete",
                                     Company.stage == "new").order_by(Company.fit_score.desc(), Company.id)
            .limit(limit * 3))]
    for cid in ids:
        if stats["queued"] >= limit:
            break
        with session_scope() as s:
            company = s.get(Company, cid)
            offer = offer_for(company)
            if offer is None:
                stats["skipped"]["لا يوجد عرض للفئة"] = stats["skipped"].get("لا يوجد عرض للفئة", 0) + 1
                continue
            ok, why = check_eligibility(s, company, offer)
            if not ok:
                stats["skipped"][why] = stats["skipped"].get(why, 0) + 1
                continue
            acct = active_account(s)
            camp = s.get(Campaign, campaign_id)
            email = company.primary("email")
            ensure_email_basis(s, company, email)
            subject, body = compose(company, offer, acct.sender_name)
            to = camp.test_recipient or email.value
            msg = OutboundMessage(
                company_id=company.id, campaign_id=campaign_id, category_id=company.category_id, offer_id=offer.id,
                kind="offer", dedupe_key=dedupe_key(company.id, offer.key), mail_account_id=acct.id,
                from_address=acct.sender_address, to_address=to, intended_to=email.value, subject=subject,
                body_text=render_text(body, acct.signature_html), body_html=render_html(body, acct.signature_html),
                attachment_name=offer.attachment.filename if offer.attachment else None,
                status="queued")
            s.add(msg)
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                continue  # أُضيف بالتوازي — لا تكرار
            stats["queued"] += 1
    return stats


def _snapshot_attachment(msg: OutboundMessage, offer: Offer) -> tuple[bytes | None, str | None]:
    """يربط الرسالة بنسخة المرفق الثابتة (المرفقات لا تُعدّل بعد رفعها) ويسجل بصمتها."""
    if not offer or not offer.attachment:
        return None, None
    data = offer.attachment.read_bytes()
    msg.attachment_id = offer.attachment.id
    msg.attachment_sha256 = hashlib.sha256(data).hexdigest()
    return data, offer.attachment.mime_type


def send_due(campaign_id: int | None = None, *, ignore_window: bool = False, max_batch: int = 50) -> dict:
    """يرسل الرسائل المجهزة ضمن الأيام والساعات والحد اليومي لكل حملة."""
    stats = {"sent": 0, "failed": 0, "held": 0}
    with session_scope() as s:
        q = select(OutboundMessage.id, OutboundMessage.campaign_id).where(OutboundMessage.status == "queued")
        if campaign_id:
            q = q.where(OutboundMessage.campaign_id == campaign_id)
        rows = list(s.execute(q.order_by(OutboundMessage.id).limit(max_batch)))
    for mid, cid in rows:
        with session_scope() as s:
            msg = s.get(OutboundMessage, mid)
            camp = s.get(Campaign, cid) if cid else None
            if msg.status != "queued":
                continue
            if camp is not None and msg.kind != "confirmation":
                if camp.status != "active" or not camp.send_enabled:
                    stats["held"] += 1
                    continue
                if not ignore_window and not in_send_window(camp):
                    stats["held"] += 1
                    continue
                if msg.kind == "offer" and sent_today(s, camp.id) >= camp.daily_send_cap:
                    stats["held"] += 1
                    continue
            # إعادة التحقق لحظة الإرسال (قد يكون العميل طلب الإيقاف بعد التجهيز)
            company = msg.company
            why = is_suppressed(s, company, msg.intended_to or msg.to_address)
            if company.do_not_contact or why:
                msg.status = "skipped"
                msg.status_detail = why or "طلبت الشركة إيقاف التواصل"
                continue
            msg.status = "sending"  # يمنع الإرسال المزدوج إن أعيد تشغيل المهمة في الأثناء
        with session_scope() as s:
            msg = s.get(OutboundMessage, mid)
            acct = s.get(MailAccount, msg.mail_account_id)
            offer = s.get(Offer, msg.offer_id) if msg.offer_id else None
            data, mime = _snapshot_attachment(msg, offer) if msg.kind == "offer" else (None, None)
            parent = s.get(OutboundMessage, msg.parent_id) if msg.parent_id else None
            out = OutgoingEmail(
                from_addr=acct.sender_address, from_name=acct.sender_name, to=msg.to_address, subject=msg.subject,
                text=msg.body_text, html=msg.body_html, reply_to=acct.reply_mailbox or None,
                attachment_name=msg.attachment_name if data else None, attachment_bytes=data,
                attachment_mime=mime or "application/pdf",
                in_reply_to=parent.internet_message_id if parent else None,
                thread_id=parent.thread_id if parent else None,
                headers={"X-Mandoob-Ref": f"msg-{msg.id}"})
            err = ""
            try:
                res = get_provider(acct).send(out)
            except Exception as e:  # خطأ شبكة/صلاحيات
                res = None
                err = str(e)[:300]
            if res and res.accepted:
                msg.status = "sent"
                msg.sent_at = now_utc()
                msg.provider_message_id = res.provider_message_id
                msg.internet_message_id = res.internet_message_id or out.message_id
                msg.thread_id = res.thread_id
                msg.status_detail = res.detail
                c = msg.company
                if msg.kind == "offer":
                    c.stage = "contacted" if c.stage == "new" else c.stage
                    c.last_action, c.last_action_at = f"أُرسل عرض: {offer.service_name if offer else msg.subject}", now_utc()
                    c.next_action = "انتظار رد العميل"
                    c.next_action_at = now_utc() + timedelta(days=(msg.campaign.followup_days if msg.campaign and msg.campaign.followup_days else 4))
                kind_label = {"offer": "أُرسل عرض", "followup": "أُرسلت رسالة متابعة",
                              "confirmation": "أُرسل تأكيد الموعد"}.get(msg.kind, "أُرسلت رسالة")
                log_activity(s, c.id, "email_sent", f"{kind_label}: {msg.subject}",
                             f"من {msg.from_address} إلى {msg.to_address}"
                             + (f" (بريد الشركة: {msg.intended_to} — وضع التجربة)" if msg.intended_to and
                                msg.intended_to != msg.to_address else "")
                             + (f"\nالمرفق: {msg.attachment_name}" if msg.attachment_id else ""),
                             ref_type="outbound", ref_id=msg.id)
                stats["sent"] += 1
            else:
                msg.status = "failed"
                msg.status_detail = (res.detail if res else err)
                stats["failed"] += 1
    return stats


def queue_followups() -> int:
    """متابعة آلية واحدة لمن لم يرد بعد المدة المحددة، في نفس المحادثة."""
    n = 0
    with session_scope() as s:
        rows = list(s.scalars(select(OutboundMessage).where(
            OutboundMessage.kind == "offer", OutboundMessage.status.in_(["sent", "delivered"]),
            OutboundMessage.replied_at.is_(None), OutboundMessage.followups_stopped.is_(False))))
        for m in rows:
            camp = m.campaign
            if not camp or not camp.followup_days or not m.sent_at:
                continue
            if now_utc() < m.sent_at + timedelta(days=camp.followup_days):
                continue
            c = m.company
            if c.do_not_contact or c.stage not in ("contacted",):
                continue
            key = dedupe_key(c.id, m.offer.key if m.offer else str(m.id), "followup1")
            if s.scalar(select(OutboundMessage.id).where(OutboundMessage.dedupe_key == key)):
                continue
            acct = s.get(MailAccount, m.mail_account_id)
            body = (f"السلام عليكم فريق {c.name}،\n\nأتابع معكم بخصوص رسالتي السابقة حول "
                    f"{m.offer.service_name if m.offer else 'خدمتنا'}. "
                    f"{m.offer.call_to_action if m.offer else 'هل يناسبكم اتصال قصير؟'}")
            s.add(OutboundMessage(
                company_id=c.id, campaign_id=m.campaign_id, category_id=m.category_id, offer_id=m.offer_id,
                kind="followup", parent_id=m.id, dedupe_key=key, mail_account_id=acct.id,
                from_address=acct.sender_address, to_address=m.to_address, intended_to=m.intended_to,
                subject=m.subject if m.subject.startswith("RE") else f"RE: {m.subject}",
                body_text=render_text(body, acct.signature_html), body_html=render_html(body, acct.signature_html)))
            n += 1
    return n
