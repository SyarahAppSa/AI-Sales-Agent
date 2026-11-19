"""طلبات «اطلب عرضًا لأسطولك»: تُسجَّل، تُربط بالشركة (أو تُنشأ)، وتُسند للمندوب المختص بمهمة وتنبيه."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .assignment import assign_owner
from .companies import find_existing, log_activity
from .discovery.base import Candidate, FoundContact
from .inbox import notify
from .models import Company, Consent, Contact, InboundLead, Offer, Task
from .normalize import domain_of, normalize_email, normalize_name, normalize_phone
from .timeutil import now_riyadh, now_utc, riyadh_to_utc

SOURCE_LABELS = {"website": "موقع الشركة", "campaign": "حملة", "partner": "إحالة شريك", "direct": "مباشر"}


def source_label(source: str | None) -> str:
    if not source or source == "discovery":
        return "اكتشاف المنصة"
    if source == "test_seed":
        return "بيانات تجريبية"
    if source == "training_demo":
        return "بيانات تدريبية"
    if source.startswith("carrier_of:"):
        return "ناقل لعميل"
    kind, _, rest = source.partition(":")
    base = {"website_form": "نموذج اطلب عرضًا", "website": "موقع الشركة", "campaign": "حملة",
            "partner": "إحالة شريك"}.get(kind, kind)
    return f"{base}: {rest}" if rest else base


def normalize_source(src: str | None, ref: str | None, utm_campaign: str | None) -> str:
    """website_form:<موقع/حملة/شريك>"""
    clean = lambda v: "".join(ch for ch in (v or "") if ch.isalnum() or ch in "-_")[:40]  # noqa: E731
    if ref:
        return f"partner:{clean(ref)}"
    if utm_campaign:
        return f"campaign:{clean(utm_campaign)}"
    if src:
        return f"website_form:{clean(src)}"
    return "website_form:website"


def too_many(s: Session, ip_hash: str, limit: int = 5) -> bool:
    since = now_utc() - timedelta(hours=1)
    return (s.scalar(select(func.count(InboundLead.id)).where(InboundLead.ip_hash == ip_hash,
                                                             InboundLead.created_at >= since)) or 0) >= limit


def hash_ip(ip: str | None, secret: str) -> str:
    return hashlib.sha256(f"{secret}:{ip or ''}".encode()).hexdigest()


def _first_contact_time() -> datetime:
    """خلال ساعة في أوقات العمل، وإلا صباح يوم العمل التالي."""
    now = now_riyadh().replace(tzinfo=None)
    t = now + timedelta(hours=1)
    if t.weekday() in (4, 5) or t.hour >= 17 or t.hour < 9:
        d = t if t.hour < 9 and t.weekday() not in (4, 5) else t + timedelta(days=1)
        while d.weekday() in (4, 5):
            d += timedelta(days=1)
        t = d.replace(hour=9, minute=30, second=0, microsecond=0)
    return riyadh_to_utc(t)


def create_lead(s: Session, *, company_name: str, city: str, vehicles: int | None, activity: str | None,
                offer_id: int | None, service_text: str, contact_name: str, phone: str | None, email: str | None,
                preferred_channel: str, source: str, notes: str = "", ip_hash: str | None = None) -> InboundLead:
    phone_n, email_n = normalize_phone(phone), normalize_email(email)
    lead = InboundLead(company_name=company_name.strip()[:300], city=city.strip()[:120], vehicles=vehicles,
                       activity=activity, offer_id=offer_id, service_text=service_text[:200],
                       contact_name=contact_name.strip()[:200], phone=phone_n or (phone or "")[:40] or None,
                       email=email_n, preferred_channel=preferred_channel, source=source, notes=notes[:2000],
                       ip_hash=ip_hash)
    s.add(lead)
    s.flush()
    ref = f"form:lead-{lead.id}"
    contacts = []
    if phone_n:
        contacts.append(FoundContact("phone", phone_n, ref, "inbound_form", "طالب العرض"))
    if email_n:
        contacts.append(FoundContact("email", email_n, ref, "inbound_form", "طالب العرض"))
    company = find_existing(s, Candidate(name=company_name, source_type="inbound_form", city=city,
                                         website=f"https://{domain_of(email_n)}" if email_n and domain_of(email_n) not in
                                         _generic() else None), contacts)
    offer = s.get(Offer, offer_id) if offer_id else None
    if company is None:
        company = Company(name=company_name.strip()[:300], name_norm=normalize_name(company_name),
                          activity=activity_label(activity), sector_key=activity, city=city,
                          data_status="complete" if (phone_n or email_n) else "incomplete",
                          fit_reason=f"طلب عرضًا لأسطوله ({vehicles or 'غير محدد'} مركبة) عبر {source_label(source)}",
                          fit_score=1.0, lead_source=source, vehicles_estimate=vehicles, stage="interested",
                          operator_status="self")
        s.add(company)
        s.flush()
        log_activity(s, company.id, "inbound", "شركة جديدة من طلب «اطلب عرضًا لأسطولك»", f"المصدر: {source_label(source)}")
    else:
        company.vehicles_estimate = vehicles or company.vehicles_estimate
        if company.stage in ("new", "contacted", "replied", "lost"):
            company.stage = "interested"
    for fc in contacts:
        if not s.scalar(select(Contact.id).where(Contact.kind == fc.kind, Contact.value == fc.raw)):
            company.contacts.append(Contact(kind=fc.kind, value=fc.raw, raw_value=fc.raw, source_url=ref,
                                            source_type="inbound_form", verified=True, preferred=True,
                                            label=f"{contact_name} (طالب العرض)"[:80]))
    s.flush()
    lead.company_id = company.id
    evidence = f"طلب عرض عبر النموذج بتاريخ {now_riyadh():%Y-%m-%d %H:%M} — {source_label(source)}"
    for ch in ("email", "call"):
        s.add(Consent(company_id=company.id, channel=ch, basis="explicit", granted=True, evidence=evidence))
    owner = assign_owner(s, company)
    due = _first_contact_time()
    channel = {"phone": "اتصال هاتفي", "email": "بريد إلكتروني", "whatsapp": "واتساب"}.get(preferred_channel, preferred_channel)
    details = (f"الاسم: {contact_name}\nالهاتف: {phone_n or phone or '—'}\nالبريد: {email_n or '—'}\n"
               f"المدينة: {city}\nعدد المركبات التقريبي: {vehicles or '—'}\n"
               f"الخدمة المطلوبة: {offer.service_name if offer else service_text or '—'}\n"
               f"طريقة التواصل المفضلة: {channel}\nالمصدر: {source_label(source)}\n{notes}")
    task = Task(company_id=company.id, owner_id=owner.id if owner else None, kind="call",
                title=f"التواصل مع طالب عرض ({channel}) — {company.name}", details=details, due_at=due,
                window_label="خلال ساعة عمل")
    s.add(task)
    company.next_action, company.next_action_at = f"التواصل مع طالب العرض ({channel})", due
    company.last_action, company.last_action_at = "وصل طلب «اطلب عرضًا لأسطولك»", now_utc()
    log_activity(s, company.id, "inbound", "طلب عرض لأسطوله", details, ref_type="lead", ref_id=lead.id)
    if owner:
        notify(s, owner, company, f"طلب عرض جديد: {company.name}", details)
    return lead


def activity_label(key: str | None) -> str:
    from .targeting import SECTORS
    return SECTORS.get(key or "", {}).get("label", "غير محدد")


def _generic():
    from .normalize import GENERIC_EMAIL_DOMAINS
    return GENERIC_EMAIL_DOMAINS
