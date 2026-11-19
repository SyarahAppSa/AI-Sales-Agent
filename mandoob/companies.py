"""سجل الشركات: كشف التكرار، دمج المصادر، التحقق من المطابقة، وقواعد الاحتساب ضمن الهدف."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import ai
from .discovery.base import Candidate, FoundContact
from .discovery.crawler import SiteResult
from .models import (Activity, Category, Company, CompanySource, Contact, ExistingCustomer, Suppression)
from .normalize import (domain_of, email_label, is_offer_suitable_email, names_similar, normalize_arabic,
                        normalize_email, normalize_name, normalize_phone)
from .targeting import SECTORS
from .timeutil import now_utc, riyadh_today

log = logging.getLogger(__name__)

BROKER_WORDS = ["وسيط", "وساطه", "وسطاء", "broker", "brokerage", "marketplace", "منصه حجز", "مقارنه اسعار",
                "aggregator", "booking platform"]


# ---------------------------------------------------------------- كشف التكرار

def find_existing(s: Session, cand: Candidate, contacts: list[FoundContact]) -> Optional[Company]:
    dom = domain_of(cand.website) if cand.website else None
    if dom:
        c = s.scalar(select(Company).where(Company.domain == dom))
        if c:
            return c
    if cand.external_id:
        src = s.scalar(select(CompanySource).where(CompanySource.external_id == cand.external_id))
        if src:
            return src.company
    values = []
    for fc in contacts:
        v = normalize_phone(fc.raw) if fc.kind == "phone" else normalize_email(fc.raw)
        if v:
            values.append((fc.kind, v))
    for kind, v in values:
        ct = s.scalar(select(Contact).where(Contact.kind == kind, Contact.value == v))
        if ct:
            return ct.company
        if kind == "email":
            ed = domain_of(v)
            c = s.scalar(select(Company).where(Company.domain == ed))
            if c:
                return c
    nn = normalize_name(cand.name)
    if nn:
        q = select(Company).where(Company.name_norm == nn)
        for c in s.scalars(q):
            if not cand.city or not c.city or c.city == cand.city:
                return c
        # تطابق تقريبي داخل المدينة نفسها
        first = nn.split()[0]
        for c in s.scalars(select(Company).where(Company.name_norm.like(f"%{first}%"),
                                                 Company.city == cand.city).limit(50)):
            if names_similar(c.name, cand.name):
                return c
    return None


def is_existing_customer(s: Session, cand: Candidate, contacts: list[FoundContact]) -> bool:
    dom = domain_of(cand.website) if cand.website else None
    nn = normalize_name(cand.name)
    conds = [ExistingCustomer.name_norm == nn]
    if dom:
        conds.append(ExistingCustomer.domain == dom)
    for fc in contacts:
        if fc.kind == "phone" and (p := normalize_phone(fc.raw)):
            conds.append(ExistingCustomer.phone == p)
        if fc.kind == "email" and (e := normalize_email(fc.raw)):
            conds.append(ExistingCustomer.email == e)
    return s.scalar(select(ExistingCustomer.id).where(or_(*conds)).limit(1)) is not None


# ---------------------------------------------------------------- المطابقة

FIT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["fits", "is_broker", "operates_fleet", "activity", "reason", "evidence_url", "score"],
    "properties": {
        "fits": {"type": "boolean"}, "is_broker": {"type": "boolean"},
        "operates_fleet": {"type": "boolean"}, "activity": {"type": "string"},
        "reason": {"type": "string"}, "evidence_url": {"type": "string"},
        "score": {"type": "number"},
    },
}


@dataclass
class FitResult:
    fits: bool
    reason: str
    evidence_url: str | None
    score: float
    activity: str
    rejected_reason: str = ""


def assess_fit(cand: Candidate, site: SiteResult | None, category: Category, exclusions: list[str],
               campaign_id: int | None = None) -> FitResult:
    text = " ".join([cand.name, cand.activity, cand.snippet, cand.fit_hint,
                     site.summary_text if site else ""])
    tn = normalize_arabic(text).lower()
    if any(normalize_arabic(w) in tn for w in BROKER_WORDS) and "وسطاء الشحن والنقل" in " ".join(exclusions):
        # فحص سريع؛ الذكاء الاصطناعي (إن توفر) يحسم الحالات الملتبسة
        if not ai.settings.ai_enabled:
            return FitResult(False, "", None, 0, cand.activity, "نشاط وساطة (مستبعد)")
    if ai.settings.ai_enabled and (site and site.pages or cand.fit_hint or cand.snippet):
        prompt = (
            f"الفئة المستهدفة: {category.label} في {', '.join(category.cities)}.\n"
            f"إشارات الأولوية: {', '.join(category.priority_signals) or 'لا يوجد'}.\n"
            f"الأنشطة المستبعدة: {', '.join(exclusions) or 'لا يوجد'}.\n\n"
            f"الشركة: {cand.name}\nالمدينة: {cand.city}\nالمصدر: {cand.source_url}\n"
            f"ملاحظة المصدر: {cand.fit_hint or cand.snippet}\n\nمحتوى موقعها:\n"
            f"{site.summary_text if site else '(لا يوجد)'}\n\n"
            "قرر: هل تطابق الشركة الفئة؟ اكتب reason كجملة عربية محددة واحدة تشرح سبب المطابقة "
            "مستندة إلى دليل من النص (مثل: «تدير أسطولًا من 300 شاحنة مبردة ولها فرع في الرياض»). "
            "evidence_url رابط الصفحة التي فيها الدليل. score من 0 إلى 1. لا تخترع معلومات غير موجودة.")
        try:
            out = ai.ai_json("أنت محلل تأهيل عملاء B2B دقيق ومتحفظ.", prompt, FIT_SCHEMA,
                             campaign_id=campaign_id, note="fit", max_tokens=2000)
            if out["is_broker"] and exclusions:
                return FitResult(False, out["reason"], out["evidence_url"], 0, out["activity"], "نشاط وساطة (مستبعد)")
            score = float(out["score"]) + (0.1 if out["operates_fleet"] else 0)
            return FitResult(bool(out["fits"]), out["reason"], out["evidence_url"] or cand.source_url,
                             min(score, 1.0), out["activity"] or category.label,
                             "" if out["fits"] else "لا تطابق الفئة المستهدفة")
        except ai.AIUnavailable as e:
            log.warning("AI fit failed, falling back to rules: %s", e)
    # بديل حتمي: كلمات القطاع
    triggers = SECTORS.get(category.sector_key, {}).get("triggers", []) + category.keywords_ar + category.keywords_en
    hits = [t for t in triggers if normalize_arabic(t).lower() in tn]
    if not hits:
        return FitResult(False, "", None, 0, cand.activity, "لا توجد دلائل على نشاط الفئة")
    fleet = any(w in tn for w in ["اسطول", "fleet", "شاحنات", "مركبه", "مركبات"])
    where = site.pages[0].url if site and site.pages else (cand.evidence_url or cand.source_url)
    reason = cand.fit_hint or (
        f"يذكر {'موقعها' if site and site.pages else 'المصدر'} نشاط «{hits[0]}» في {cand.city}"
        + ("، مع إشارة إلى تشغيل أسطول مركبات" if fleet else ""))
    return FitResult(True, reason, cand.evidence_url or where, 0.6 + (0.2 if fleet else 0),
                     cand.activity or category.label)


# ---------------------------------------------------------------- الحفظ والدمج

def _add_contact(s: Session, company: Company, fc: FoundContact) -> bool:
    if fc.kind == "phone":
        value = normalize_phone(fc.raw)
    else:
        value = normalize_email(fc.raw)
        if value and not is_offer_suitable_email(value):
            # نحفظه للمرجعية لكن لا يُعد بريدًا مناسبًا للعروض
            pass
    if not value or not fc.source_url:
        return False
    existing = s.scalar(select(Contact).where(Contact.kind == fc.kind, Contact.value == value))
    if existing:
        return False
    label = email_label(value)[1] if fc.kind == "email" else fc.label
    ct = Contact(kind=fc.kind, value=value, raw_value=fc.raw[:200], source_url=fc.source_url[:500],
                 source_type=fc.source_type, verified=True, label=label)
    company.contacts.append(ct)
    s.flush()
    return True


def _refresh_preferred(company: Company) -> None:
    emails = [c for c in company.contacts if c.kind == "email" and c.verified]
    emails.sort(key=lambda c: email_label(c.value)[0])
    for c in emails:
        c.preferred = False
    if emails and is_offer_suitable_email(emails[0].value):
        emails[0].preferred = True
    phones = [c for c in company.contacts if c.kind == "phone" and c.verified]
    for c in phones:
        c.preferred = False
    if phones:
        # تفضيل الأرضي/الموحد على الجوال للتواصل التجاري
        phones.sort(key=lambda c: (c.value.startswith("+9665"), c.id or 0))
        phones[0].preferred = True


def is_complete(company: Company) -> bool:
    has_phone = any(c.kind == "phone" and c.verified for c in company.contacts)
    has_email = any(c.kind == "email" and c.verified and is_offer_suitable_email(c.value) for c in company.contacts)
    return has_phone and has_email


def missing_fields(company: Company) -> list[str]:
    out = []
    if not any(c.kind == "phone" and c.verified for c in company.contacts):
        out.append("الهاتف")
    if not any(c.kind == "email" and c.verified and is_offer_suitable_email(c.value) for c in company.contacts):
        out.append("البريد الإلكتروني")
    return out


@dataclass
class UpsertResult:
    outcome: str  # new_complete / new_incomplete / completed_now / duplicate / rejected
    company: Optional[Company]
    note: str = ""


def log_activity(s: Session, company_id: int, kind: str, title: str, details: str = "", user_id=None,
                 ref_type=None, ref_id=None, at=None) -> None:
    s.add(Activity(company_id=company_id, kind=kind, title=title, details=details, user_id=user_id,
                   ref_type=ref_type, ref_id=ref_id, at=at or now_utc()))


def upsert_candidate(s: Session, cand: Candidate, site: SiteResult | None, category: Category, fit: FitResult,
                     run_id: int | None = None, day=None) -> UpsertResult:
    day = day or riyadh_today()
    contacts = list(cand.contacts) + (site.contacts if site else [])
    existing = find_existing(s, cand, contacts)
    now = now_utc()
    if existing is not None:
        was_complete = existing.data_status == "complete"
        existing.last_seen_at = now
        existing.times_seen += 1
        _merge_source(s, existing, cand, run_id)
        added = sum(_add_contact(s, existing, fc) for fc in contacts)
        if not existing.website and cand.website:
            existing.website = cand.website
            existing.domain = domain_of(cand.website)
        _refresh_preferred(existing)
        if not was_complete and existing.data_status != "rejected" and is_complete(existing):
            # كانت قيد الاستكمال وعثرنا على البيان الناقص: تُحتسب اليوم لأنها لم تُحتسب من قبل
            existing.data_status = "complete"
            existing.completed_at = now
            existing.counted_on = day
            log_activity(s, existing.id, "data", "اكتملت بيانات الشركة",
                         "عُثر على البيان الناقص من مصدر جديد")
            return UpsertResult("completed_now", existing)
        if added:
            log_activity(s, existing.id, "data", "تحديث بيانات الشركة", f"أضيف {added} بيان اتصال جديد")
        return UpsertResult("duplicate", existing, "موجودة مسبقًا")

    if not fit.fits:
        return UpsertResult("rejected", None, fit.rejected_reason)
    if is_existing_customer(s, cand, contacts):
        return UpsertResult("rejected", None, "عميل حالي لسيارة اب")
    if _suppressed(s, cand, contacts):
        return UpsertResult("rejected", None, "ضمن قائمة إيقاف التواصل")

    company = Company(
        name=cand.name.strip()[:300], name_en=cand.name_en, name_norm=normalize_name(cand.name),
        activity=fit.activity or cand.activity or category.label, sector_key=category.sector_key,
        city=cand.city or (category.cities[0] if category.cities else ""), website=cand.website,
        domain=domain_of(cand.website) if cand.website else None, fit_reason=fit.reason,
        fit_evidence_url=fit.evidence_url, fit_score=fit.score, campaign_id=category.campaign_id,
        category_id=category.id, first_found_at=now, last_seen_at=now,
    )
    s.add(company)
    s.flush()
    _merge_source(s, company, cand, run_id)
    for fc in contacts:
        _add_contact(s, company, fc)
    _refresh_preferred(company)
    log_activity(s, company.id, "discovered", f"اكتُشفت الشركة عبر {SOURCE_LABELS.get(cand.source_type, cand.source_type)}",
                 f"الحملة: {category.campaign.name} — الفئة: {category.label}\nسبب الترشيح: {fit.reason}")
    if is_complete(company):
        company.data_status = "complete"
        company.completed_at = now
        company.counted_on = day
        return UpsertResult("new_complete", company)
    company.data_status = "incomplete"
    company.retry_enrich_at = now
    return UpsertResult("new_incomplete", company, "ينقصها: " + "، ".join(missing_fields(company)))


SOURCE_LABELS = {"google_places": "خرائط جوجل", "web_search": "محرك البحث", "ai_search": "البحث بالذكاء الاصطناعي",
                 "directory": "دليل أعمال", "website": "موقع الشركة", "recorded": "مصدر موثّق"}


def _merge_source(s: Session, company: Company, cand: Candidate, run_id) -> None:
    url = (cand.source_url or "")[:500]
    exists = s.scalar(select(CompanySource).where(CompanySource.company_id == company.id,
                                                  CompanySource.source_type == cand.source_type,
                                                  CompanySource.url == url))
    if exists:
        return
    company.sources.append(CompanySource(source_type=cand.source_type, url=url, external_id=cand.external_id,
                                         snippet=(cand.snippet or cand.fit_hint)[:1000], run_id=run_id))
    s.flush()


def _suppressed(s: Session, cand: Candidate, contacts: list[FoundContact]) -> bool:
    conds = []
    dom = domain_of(cand.website) if cand.website else None
    if dom:
        conds.append((Suppression.kind == "domain") & (Suppression.value == dom))
    for fc in contacts:
        v = normalize_phone(fc.raw) if fc.kind == "phone" else normalize_email(fc.raw)
        if v:
            conds.append((Suppression.kind == fc.kind) & (Suppression.value == v))
    if not conds:
        return False
    return s.scalar(select(Suppression.id).where(or_(*conds), Suppression.channel == "all").limit(1)) is not None
