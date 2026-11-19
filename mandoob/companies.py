"""سجل الشركات: كشف التكرار، دمج المصادر، التحقق من المطابقة، وقواعد الاحتساب ضمن الهدف."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import ai
from .discovery.base import Candidate, FoundContact
from .discovery.crawler import SiteResult
from .models import (Activity, Category, Company, CompanyRelation, CompanySignal, CompanySource, Contact,
                     ExistingCustomer, Suppression)
from .normalize import (domain_of, email_label, is_offer_suitable_email, names_similar, normalize_arabic,
                        normalize_email, normalize_name, normalize_phone)
from .targeting import SECTORS
from .segments import (GENERIC_CARRIER_WORDS, SIGNAL_PATTERNS, department_of_email, detect_operator,
                       detect_signals, find_operational_phrase)
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
        q = select(Company).where(Company.name_norm == nn, Company.is_test.is_(False))
        for c in s.scalars(q):
            if not cand.city or not c.city or c.city == cand.city:
                return c
        # تطابق تقريبي داخل المدينة نفسها
        first = nn.split()[0]
        for c in s.scalars(select(Company).where(Company.name_norm.like(f"%{first}%"), Company.is_test.is_(False),
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
    "required": ["fits", "is_broker", "operates_fleet", "activity", "reason", "evidence_url", "score",
                 "operational_phrase", "operator_status", "carrier_name", "carrier_evidence", "decision_unit",
                 "hq_name", "signals"],
    "properties": {
        "fits": {"type": "boolean"}, "is_broker": {"type": "boolean"},
        "operates_fleet": {"type": "boolean"}, "activity": {"type": "string"},
        "reason": {"type": "string"}, "evidence_url": {"type": "string"},
        "score": {"type": "number"},
        "operational_phrase": {"type": "string", "description": "اقتباس حرفي قصير من النص يثبت تشغيل المركبات، أو فارغ"},
        "operator_status": {"type": "string", "enum": ["self", "contracted", "unknown"]},
        "carrier_name": {"type": "string", "description": "اسم الناقل المتعاقد إن ذُكر صراحة، أو فارغ"},
        "carrier_evidence": {"type": "string"},
        "decision_unit": {"type": "string", "description": "الإدارة الأقرب لقرار شراء خدمات الأسطول"},
        "hq_name": {"type": "string", "description": "اسم المقر الرئيسي إن كانت الصفحة لفرع، أو فارغ"},
        "signals": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "title", "announced_on", "url"],
            "properties": {
                "kind": {"type": "string", "enum": list(SIGNAL_PATTERNS)},
                "title": {"type": "string"}, "announced_on": {"type": "string", "description": "YYYY-MM-DD أو فارغ"},
                "url": {"type": "string"}}}},
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
    phrase: str | None = None
    operator: dict = field(default_factory=lambda: {"status": "unknown", "carrier": None, "evidence": "", "url": None})
    signals: list = field(default_factory=list)
    decision_unit: str | None = None
    hq_name: str | None = None


def _rules_extras(site: SiteResult | None, category: Category) -> tuple:
    pages = site.pages if site else []
    sector = SECTORS.get(category.sector_key, {})
    phrase = find_operational_phrase(pages, list(sector.get("operational", [])) + list(category.keywords_ar or []))
    return phrase, detect_operator(pages), detect_signals(pages), sector.get("decision_unit")


def assess_fit(cand: Candidate, site: SiteResult | None, category: Category, exclusions: list[str],
               campaign_id: int | None = None) -> FitResult:
    text = " ".join([cand.name, cand.activity, cand.snippet, cand.fit_hint,
                     site.summary_text if site else ""])
    tn = normalize_arabic(text).lower()
    phrase, operator, signals, unit = _rules_extras(site, category)
    sector = SECTORS.get(category.sector_key, {})
    if any(normalize_arabic(w) in tn for w in BROKER_WORDS) and "وسطاء الشحن والنقل" in " ".join(exclusions):
        # فحص سريع؛ الذكاء الاصطناعي (إن توفر) يحسم الحالات الملتبسة
        if not ai.settings.ai_enabled:
            return FitResult(False, "", None, 0, cand.activity, "نشاط وساطة (مستبعد)")
    if ai.settings.ai_enabled and (site and site.pages or cand.fit_hint or cand.snippet):
        prompt = (
            f"الفئة المستهدفة: {category.label} في {', '.join(category.cities)}.\n"
            f"وصف الشريحة: {sector.get('examples', '')}\n"
            f"عبارات التشغيل التي نبحث عنها: {', '.join(sector.get('operational', []))}\n"
            f"إشارات الأولوية: {', '.join(category.priority_signals) or 'لا يوجد'}.\n"
            f"الأنشطة المستبعدة: {', '.join(exclusions) or 'لا يوجد'}.\n\n"
            f"الشركة: {cand.name}\nالمدينة: {cand.city}\nالمصدر: {cand.source_url}\n"
            f"ملاحظة المصدر: {cand.fit_hint or cand.snippet}\n\nمحتوى موقعها:\n"
            f"{site.summary_text if site else '(لا يوجد)'}\n\n"
            "قرر: هل تشغّل الشركة مركبات بحكم طبيعة عملها بما يطابق الفئة؟ نشاطها الأساسي قد يكون بعيدًا عن النقل. "
            "reason جملة عربية محددة تستند إلى دليل من النص. operational_phrase اقتباس حرفي من النص. "
            "operator_status: self إن كانت تشغّل مركباتها، contracted إن ذكرت ناقلًا تتعاقد معه (واكتب اسمه في carrier_name)، "
            "unknown إن لم تتضح العلاقة. signals: إعلانات افتتاح مستودع أو توسع لمدينة جديدة أو فوز بعقد تشغيل أو توظيف "
            "سائقين/مشرفي أسطول، مع التاريخ والرابط إن وُجدا. لا تخترع معلومات غير موجودة.")
        try:
            out = ai.ai_json("أنت محلل تأهيل عملاء B2B دقيق ومتحفظ.", prompt, FIT_SCHEMA,
                             campaign_id=campaign_id, note="fit", max_tokens=3000)
            if out["is_broker"] and exclusions:
                return FitResult(False, out["reason"], out["evidence_url"], 0, out["activity"], "نشاط وساطة (مستبعد)")
            score = float(out["score"]) + (0.1 if out["operates_fleet"] else 0)
            op = {"status": out["operator_status"], "carrier": out["carrier_name"] or None,
                  "evidence": out["carrier_evidence"], "url": out["evidence_url"] or None}
            if op["status"] == "unknown" and operator["status"] != "unknown":
                op = operator
            ai_signals = [{"kind": x["kind"], "label": SIGNAL_PATTERNS[x["kind"]][0], "title": x["title"],
                           "announced_on": _parse_date(x["announced_on"]), "url": x["url"]} for x in out["signals"]]
            return FitResult(bool(out["fits"]), out["reason"], out["evidence_url"] or cand.source_url,
                             min(score, 1.0), out["activity"] or category.label,
                             "" if out["fits"] else "لا تطابق الفئة المستهدفة",
                             phrase=out["operational_phrase"] or (phrase[0] if phrase else None), operator=op,
                             signals=ai_signals or signals, decision_unit=out["decision_unit"] or unit,
                             hq_name=out["hq_name"] or None)
        except ai.AIUnavailable as e:
            log.warning("AI fit failed, falling back to rules: %s", e)
    # بديل حتمي: كلمات القطاع + عبارات التشغيل
    triggers = list(sector.get("triggers", [])) + list(category.keywords_ar or []) + list(category.keywords_en or [])
    hits = [t for t in triggers if normalize_arabic(t).lower() in tn]
    if not hits:
        return FitResult(False, "", None, 0, cand.activity, "لا توجد دلائل على نشاط الفئة")
    if sector.get("kind") == "usage" and not phrase and not cand.fit_hint and operator["status"] == "unknown":
        # شرائح التشغيل تتطلب دليلًا على تشغيل المركبات، لا مجرد اسم النشاط
        return FitResult(False, "", None, 0, cand.activity, "لا يوجد دليل على تشغيل مركبات")
    fleet = bool(phrase) or any(w in tn for w in ["اسطول", "fleet", "شاحنات", "مركبه", "مركبات"])
    where = phrase[1] if phrase else (site.pages[0].url if site and site.pages else (cand.evidence_url or cand.source_url))
    if cand.fit_hint:
        reason = cand.fit_hint
    elif not phrase and operator["status"] == "contracted":
        reason = f"تعلن عن {category.label} في {cand.city}، ويتولى النقل ناقل متعاقد: {operator['carrier']}"
    elif phrase:
        reason = f"تذكر صفحتها «{phrase[0]}» ضمن نشاط {category.label} في {cand.city}"
    else:
        reason = (f"يذكر {'موقعها' if site and site.pages else 'المصدر'} نشاط «{hits[0]}» في {cand.city}"
                  + ("، مع إشارة إلى تشغيل أسطول مركبات" if fleet else ""))
    return FitResult(True, reason, cand.evidence_url or where, 0.6 + (0.2 if fleet else 0),
                     cand.activity or category.label, phrase=phrase[0] if phrase else None, operator=operator,
                     signals=signals, decision_unit=unit)


def _parse_date(v: str | None):
    from datetime import date as _d
    try:
        return _d.fromisoformat(v[:10]) if v else None
    except ValueError:
        return None


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
    unit = normalize_arabic(company.decision_unit or "")

    def rank(c):
        dept = department_of_email(c.value)
        if dept:
            c.label = dept
        matches_unit = bool(dept and unit and any(w in unit for w in normalize_arabic(dept).split()))
        return (0 if matches_unit else 1, 0 if dept else 1, email_label(c.value)[0])
    emails.sort(key=rank)
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
        if fit is not None and fit.signals:
            _save_signals(s, existing, fit.signals)
        if not was_complete and existing.data_status != "rejected" and existing.parent_id is None \
                and is_complete(existing):
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
        return UpsertResult("rejected", None, "عميل حالي لسيارة آب")
    if _suppressed(s, cand, contacts):
        return UpsertResult("rejected", None, "ضمن قائمة إيقاف التواصل")

    city = cand.city or (category.cities[0] if category.cities else "")
    hq = find_hq(s, cand.name, city, fit.hq_name)
    company = Company(
        name=cand.name.strip()[:300], name_en=cand.name_en, name_norm=normalize_name(cand.name),
        activity=fit.activity or cand.activity or category.label, sector_key=category.sector_key,
        city=city, website=cand.website,
        domain=domain_of(cand.website) if cand.website else None, fit_reason=fit.reason,
        fit_evidence_url=fit.evidence_url, fit_score=fit.score, campaign_id=category.campaign_id,
        category_id=category.id, first_found_at=now, last_seen_at=now,
        fit_phrase=(fit.phrase or None) and fit.phrase[:300], operator_status=fit.operator.get("status") or "unknown",
        operator_evidence=fit.operator.get("evidence") or None, operator_evidence_url=fit.operator.get("url"),
        decision_unit=fit.decision_unit or SECTORS.get(category.sector_key, {}).get("decision_unit"),
        parent_id=hq.id if hq else None, lead_source=cand.lead_source or "discovery",
    )
    s.add(company)
    s.flush()
    _merge_source(s, company, cand, run_id)
    for fc in contacts:
        _add_contact(s, company, fc)
    _refresh_preferred(company)
    _save_signals(s, company, fit.signals)
    details = f"الحملة: {category.campaign.name} — الفئة: {category.label}\nسبب الترشيح: {fit.reason}"
    if fit.phrase:
        details += f"\nعبارة التشغيل: «{fit.phrase}»"
    details += f"\nمشغّل المركبات: {OPERATOR_LABELS[company.operator_status]}"
    log_activity(s, company.id, "discovered", f"اكتُشفت الشركة عبر {SOURCE_LABELS.get(cand.source_type, cand.source_type)}",
                 details)
    if fit.operator.get("status") == "contracted" and fit.operator.get("carrier"):
        _add_carrier_opportunity(s, company, fit.operator, category, run_id)
    if hq is not None:
        s.add(CompanyRelation(company_id=company.id, related_id=hq.id, kind="branch",
                              evidence=f"الاسم يطابق «{hq.name}» في {hq.city}", source_url=cand.source_url))
        company.data_status = "complete" if is_complete(company) else "incomplete"
        log_activity(s, company.id, "branch", f"رُبطت كفرع للمقر الرئيسي: {hq.name}",
                     "لا تُحتسب شركة جديدة ولا يُرسل لها عرض منفصل؛ يُوجَّه العرض للمقر الرئيسي.")
        log_activity(s, hq.id, "branch", f"أضيف فرع: {company.name} — {company.city}")
        return UpsertResult("branch", company, "فرع لشركة موجودة")
    if is_complete(company):
        company.data_status = "complete"
        company.completed_at = now
        company.counted_on = day
        return UpsertResult("new_complete", company)
    company.data_status = "incomplete"
    company.retry_enrich_at = now
    return UpsertResult("new_incomplete", company, "ينقصها: " + "، ".join(missing_fields(company)))


OPERATOR_LABELS = {"self": "تشغّل مركباتها بنفسها", "contracted": "تتعاقد مع ناقل", "unknown": "مشغّل المركبات غير مؤكد"}
SIGNAL_RECENT_DAYS = 180


def _save_signals(s: Session, company: Company, signals: list[dict]) -> None:
    from datetime import timedelta
    added = 0
    for sig in signals or []:
        url = (sig.get("url") or "")[:500]
        if s.scalar(select(CompanySignal.id).where(CompanySignal.company_id == company.id,
                                                   CompanySignal.kind == sig["kind"], CompanySignal.url == url)):
            continue
        s.add(CompanySignal(company_id=company.id, kind=sig["kind"], label=sig.get("label") or sig["kind"],
                            title=(sig.get("title") or "")[:1000], announced_on=sig.get("announced_on"), url=url))
        added += 1
        log_activity(s, company.id, "signal", f"إشارة توقيت: {sig.get('label')}",
                     f"{sig.get('title', '')[:300]}\nتاريخ الإعلان: {sig.get('announced_on') or 'غير مذكور'}\n{url}")
    if added:
        s.flush()
        recent = [x for x in s.scalars(select(CompanySignal).where(CompanySignal.company_id == company.id))
                  if x.announced_on is None or x.announced_on >= riyadh_today() - timedelta(days=SIGNAL_RECENT_DAYS)]
        boost = min(0.15 * len(recent), 0.3)  # أولوية إضافية للشركات التي تغيّر احتياجها مؤخرًا
        company.fit_score = (company.fit_score or 0) - (company.signal_boost or 0) + boost
        company.signal_boost = boost


def base_name(name: str) -> str:
    """الاسم بدون كلمة «فرع» وأسماء المدن — لربط الفروع بالمقر الرئيسي."""
    from .targeting import CITIES
    words = normalize_name(name).split()
    city_words = {normalize_name(v) for vs in CITIES.values() for v in vs} | {normalize_name(c) for c in CITIES}
    return " ".join(w for w in words if w not in {"فرع", "branch", "بالرياض", "بجده", "بالدمام", "في"}
                    and w not in city_words)


def find_hq(s: Session, name: str, city: str, hq_name: str | None = None) -> Optional[Company]:
    """مقر رئيسي محتمل: شركة بالاسم الأساسي نفسه في مدينة أخرى (أو الاسم الذي ذكره الموقع)."""
    for nm in [hq_name, name]:
        if not nm:
            continue
        b = base_name(nm)
        if not b or len(b) < 3:
            continue
        first = b.split()[0]
        for c in s.scalars(select(Company).where(Company.name_norm.like(f"%{first}%"), Company.parent_id.is_(None),
                                                 Company.is_test.is_(False))
                           .limit(50)):
            if base_name(c.name) == b and (c.city != city or nm == hq_name):
                return c
    return None


def _add_carrier_opportunity(s: Session, company: Company, operator: dict, category: Category, run_id) -> None:
    """الناقل الذي يشغّل توزيع الشركة يصبح فرصة مستقلة مع رابط المصدر."""
    name = operator["carrier"].strip()
    if normalize_arabic(name).lower() in {normalize_arabic(x).lower() for x in GENERIC_CARRIER_WORDS} or len(name) < 3:
        log_activity(s, company.id, "operator", f"تتعاقد مع ناقل وطني معروف: {name}", operator.get("evidence", ""))
        return
    existing = find_existing(s, Candidate(name=name, source_type="website", city=company.city), [])
    if existing is None:
        transport_cat = next((c for c in category.campaign.categories
                              if c.sector_key in ("transport_logistics", "refrigerated_transport")), None)
        existing = Company(name=name[:300], name_norm=normalize_name(name), activity="نقل وتوزيع (ناقل متعاقد)",
                           sector_key="transport_logistics", city=company.city, data_status="incomplete",
                           fit_reason=f"ناقل يشغّل توزيع «{company.name}» حسب ما ورد في صفحتها",
                           fit_evidence_url=operator.get("url"), fit_score=0.7, operator_status="self",
                           campaign_id=company.campaign_id, category_id=transport_cat.id if transport_cat else None,
                           lead_source=f"carrier_of:{company.id}", decision_unit="إدارة الأسطول والتشغيل",
                           retry_enrich_at=now_utc())
        s.add(existing)
        s.flush()
        existing.sources.append(CompanySource(source_type="website", url=(operator.get("url") or "")[:500],
                                              snippet=(operator.get("evidence") or "")[:1000], run_id=run_id))
        log_activity(s, existing.id, "discovered", f"اكتُشفت كناقل متعاقد مع {company.name}",
                     f"الدليل: {operator.get('evidence', '')}\n{operator.get('url') or ''}")
    s.add(CompanyRelation(company_id=company.id, related_id=existing.id, kind="carrier",
                          evidence=(operator.get("evidence") or "")[:1000], source_url=operator.get("url")))
    log_activity(s, company.id, "operator", f"تتعاقد مع ناقل: {existing.name}",
                 f"أضيف الناقل كفرصة مستقلة.\n{operator.get('evidence', '')}")
    s.flush()


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
