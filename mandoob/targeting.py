"""تحويل طلب المدير المكتوب بالعربية إلى إعدادات حملة قابلة للمراجعة.

يستخدم Claude عند توفره، مع محلل حتمي احتياطي يعتمد على قاموس القطاعات والمدن أدناه.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from . import ai
from .normalize import normalize_arabic

log = logging.getLogger(__name__)

CITIES = {
    "الرياض": ["الرياض", "رياض", "riyadh"],
    "جدة": ["جده", "جدة", "jeddah", "jedda"],
    "الدمام": ["الدمام", "دمام", "dammam"],
    "الخبر": ["الخبر", "khobar"],
    "مكة المكرمة": ["مكه", "مكة", "makkah", "mecca"],
    "المدينة المنورة": ["المدينه المنوره", "المدينة المنورة", "madinah", "medina"],
    "الظهران": ["الظهران", "dhahran"],
    "الجبيل": ["الجبيل", "jubail"],
    "القصيم": ["القصيم", "بريده", "بريدة", "qassim", "buraydah"],
    "أبها": ["ابها", "أبها", "abha"],
    "تبوك": ["تبوك", "tabuk"],
    "الطائف": ["الطايف", "الطائف", "taif"],
    "حائل": ["حايل", "حائل", "hail"],
    "جازان": ["جازان", "جيزان", "jazan"],
    "ينبع": ["ينبع", "yanbu"],
    "الأحساء": ["الاحساء", "الأحساء", "hofuf", "ahsa"],
}
CITY_EN = {"الرياض": "Riyadh", "جدة": "Jeddah", "الدمام": "Dammam", "الخبر": "Khobar",
           "مكة المكرمة": "Makkah", "المدينة المنورة": "Madinah", "الظهران": "Dhahran", "الجبيل": "Jubail",
           "القصيم": "Qassim", "أبها": "Abha", "تبوك": "Tabuk", "الطائف": "Taif", "حائل": "Hail",
           "جازان": "Jazan", "ينبع": "Yanbu", "الأحساء": "Al Ahsa"}

SECTORS: dict[str, dict[str, Any]] = {
    "transport_logistics": {
        "label": "النقل والخدمات اللوجستية",
        "triggers": ["نقل", "لوجست", "شحن", "شاحنات", "logistics", "freight", "trucking"],
        "keywords_ar": ["شركة نقل بضائع", "شركة خدمات لوجستية", "شركة شحن بري", "نقل ثقيل وشاحنات",
                        "شركة نقل وتوزيع", "أسطول شاحنات"],
        "keywords_en": ["logistics company", "freight transport company", "trucking company",
                        "land transport company", "distribution company fleet"],
    },
    "refrigerated_transport": {
        "label": "النقل المبرّد",
        "triggers": ["مبرد", "تبريد", "مثلج", "cold chain", "refrigerat", "reefer"],
        "keywords_ar": ["شركة نقل مبرد", "نقل الأغذية المبردة", "سلاسل التبريد", "التوزيع المبرد",
                        "شاحنات مبردة", "نقل مجمد ومبرد"],
        "keywords_en": ["cold chain logistics", "refrigerated trucking", "reefer transport",
                        "temperature controlled distribution", "frozen food distribution"],
    },
    "delivery": {
        "label": "التوصيل",
        "triggers": ["توصيل", "طلبات", "مندوبي توصيل", "delivery", "courier", "last mile"],
        "keywords_ar": ["شركة توصيل طلبات", "خدمات توصيل", "توصيل الميل الأخير", "شركة توصيل شحنات",
                        "مندوبي توصيل"],
        "keywords_en": ["delivery company", "courier company", "last mile delivery", "parcel delivery"],
    },
    "car_rental": {
        "label": "تأجير السيارات",
        "triggers": ["تأجير السيارات", "تاجير السيارات", "تأجير سيارات", "تاجير سيارات", "ايجار سيارات",
                     "car rental", "leasing", "rent a car"],
        "keywords_ar": ["شركة تأجير سيارات", "تأجير سيارات للشركات", "تأجير سيارات طويل الأجل",
                        "تأجير أساطيل"],
        "keywords_en": ["car rental company", "fleet leasing", "corporate car rental", "rent a car"],
    },
    "staff_transport": {
        "label": "نقل الموظفين والطلاب",
        "triggers": ["نقل الموظفين", "نقل الطلاب", "نقل مدرسي", "حافلات", "staff transport", "bus"],
        "keywords_ar": ["شركة نقل موظفين", "نقل طلاب", "تأجير حافلات", "نقل جماعي"],
        "keywords_en": ["staff transportation company", "bus rental company", "school transport"],
    },
    "passenger_transport": {
        "label": "نقل الركاب والليموزين",
        "triggers": ["ليموزين", "أجرة", "تاكسي", "نقل الركاب", "limousine", "taxi"],
        "keywords_ar": ["شركة ليموزين", "نقل ركاب", "سيارات أجرة"],
        "keywords_en": ["limousine company", "taxi company", "passenger transport"],
    },
}

from .segments import (DEFAULT_DECISION_UNIT, INDUSTRIAL_HINTS, REGION_TRIGGERS, REGIONS, SECTOR_OVERRIDES,
                       USAGE_SEGMENTS)

_BASE_OPERATIONAL = {
    "transport_logistics": ["أسطول شاحنات", "أسطولنا", "شاحناتنا", "نقل بضائع بين المدن", "fleet of trucks", "our fleet"],
    "refrigerated_transport": ["شاحنات مبردة", "نقل مبرد", "سلاسل التبريد", "cold chain", "reefer trucks"],
    "delivery": ["توصيل الميل الأخير", "مندوبي توصيل", "أسطول التوصيل", "last-mile delivery", "delivery fleet"],
    "car_rental": ["أسطول", "سيارة", "تأجير طويل الأجل", "fleet", "corporate leasing"],
    "passenger_transport": ["أسطول", "سيارات ليموزين", "fleet"],
}
for _k, _v in SECTORS.items():
    _v.setdefault("operational", _BASE_OPERATIONAL.get(_k, []))
    _v.setdefault("decision_unit", DEFAULT_DECISION_UNIT.get(_k, "إدارة التشغيل"))
    _v.setdefault("kind", "vertical")
for _k, _v in USAGE_SEGMENTS.items():
    SECTORS[_k] = {**_v, "kind": "usage"}
for _k, _v in SECTOR_OVERRIDES.items():
    SECTORS[_k] = {**SECTORS.get(_k, {}), **_v, "kind": "usage"}
for _k, _v in SECTORS.items():
    # المحلل يقارن بنص موحّد (بدون همزات/تاء مربوطة)
    _v["triggers"] = [normalize_arabic(x).lower() for x in _v["triggers"]]

EXCLUSION_HINTS = {
    "brokers": (["وسيط", "وسطاء", "broker"], ["وسطاء الشحن والنقل", "منصات الحجز والمقارنة",
                                               "freight broker", "marketplace / aggregator"]),
}

PRIORITY_HINTS = {
    "fleet": (["اسطول", "أسطول", "fleet"], "تدير أسطول مركبات"),
}

_AR_NUM = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


_ZONE_WORDS = ("صناعي", "ميناء", "اللوجستية", "الاقتصادية", "للصناعات", "سدير")


def _find_cities(t: str) -> list[str]:
    found: list[str] = []
    for city, variants in CITIES.items():
        if any(normalize_arabic(v).lower() in t for v in variants):
            found.append(city)
    want_zones = any(normalize_arabic(h).lower() in t for h in INDUSTRIAL_HINTS)
    for region, trigs in REGION_TRIGGERS.items():
        if any(normalize_arabic(tr).lower() in t for tr in trigs):
            for place in REGIONS[region]:
                is_zone = any(w in place for w in _ZONE_WORDS)
                if (want_zones or not is_zone) and place not in found:
                    found.append(place)
    if want_zones:  # «المدن الصناعية والمناطق اللوجستية» للمدن المذكورة
        for city in list(found):
            for region, places in REGIONS.items():
                if city in places:
                    found.extend(p for p in places if any(w in p for w in _ZONE_WORDS) and p not in found)
    return found


def _find_sectors(t: str) -> list[str]:
    found = []
    for key, s in SECTORS.items():
        if any(normalize_arabic(tr).lower() in t for tr in s["triggers"]):
            found.append(key)
    # "النقل المبرد" يحتوي كلمة "نقل" — لا نضيف النقل العام إلا إذا ذُكر صراحةً بشكل مستقل
    if "refrigerated_transport" in found and "transport_logistics" in found:
        plain = re.sub(r"نقل\s*(ال)?(اغذيه\s*ال)?مبرد", "", t)
        if not any(normalize_arabic(tr).lower() in plain for tr in SECTORS["transport_logistics"]["triggers"]):
            found.remove("transport_logistics")
    return found


def _find_quotas(text: str, sectors: list[str]) -> dict[str, int]:
    """«٢٠٠ شركة نقل» → حصة لفئة. وإذا غطى رقم واحد عدة فئات («٨٠ لاختبار قطاعين») يُقسم بينها."""
    t = normalize_arabic(text.translate(_AR_NUM)).lower()
    quotas: dict[str, int] = {}
    for m in re.finditer(r"(\d{1,4})\s*(?:شركه|شركات|منشاه|منشآت)?\s*([^\d،,.؛]{2,160})", t):
        n, tail = int(m.group(1)), m.group(2)
        hit = [k for k in sectors if k not in quotas and any(tr in tail for tr in SECTORS[k]["triggers"])]
        for i, key in enumerate(hit):
            quotas[key] = n // len(hit) + (1 if i < n % len(hit) else 0)
    return quotas


def parse_request_rules(text: str, daily_target: int = 400) -> dict[str, Any]:
    """محلل حتمي احتياطي."""
    t = normalize_arabic(text).lower()
    cities = _find_cities(t) or ["الرياض"]
    sectors = _find_sectors(t) or ["transport_logistics"]
    exclusions: list[str] = []
    for _, (triggers, labels) in EXCLUSION_HINTS.items():
        if any(normalize_arabic(tr) in t for tr in triggers):
            exclusions.extend(labels)
    t_plain = re.sub(r"[«»\"'“”]", "", t)
    exclude_existing = any(k in t_plain for k in ["عملاء سياره اب", "عملائنا الحاليين", "العملاء الحاليين",
                                             "existing customers"])
    signals = [label for _, (trs, label) in PRIORITY_HINTS.items() if any(normalize_arabic(x) in t for x in trs)]
    quotas = _find_quotas(text, sectors)
    m = re.search(r"(\d{2,5})\s*شركه\s*(جديده)?\s*يوميا", normalize_arabic(text.translate(_AR_NUM)))
    if m and not quotas:
        daily_target = int(m.group(1))
    if quotas:
        daily_target = sum(quotas.values()) if len(quotas) == len(sectors) else daily_target
    # الحصص المتبقية توزع بالتساوي
    remaining = max(daily_target - sum(quotas.values()), 0)
    missing = [s for s in sectors if s not in quotas]
    for i, s in enumerate(missing):
        quotas[s] = remaining // len(missing) + (1 if i < remaining % len(missing) else 0)
    exp_idx = None
    for word in ("اختبار", "تجريبي", "تجربه"):
        j = t.find(word)
        if j >= 0:
            exp_idx = j if exp_idx is None else min(exp_idx, j)
    experimental = set()
    if exp_idx is not None:
        tail = t[exp_idx:]
        for key in sectors:
            if any(tr in tail for tr in SECTORS[key]["triggers"]):
                experimental.add(key)
    cats = []
    for i, key in enumerate(sectors):
        s = SECTORS[key]
        cats.append({
            "sector_key": key, "label": s["label"], "cities": cities,
            "keywords_ar": s["keywords_ar"], "keywords_en": s["keywords_en"],
            "priority": i + 1, "priority_signals": signals, "quota": quotas.get(key, 0),
            "is_experiment": key in experimental,
        })
    return {
        "name": "حملة " + " و".join(SECTORS[k]["label"] for k in sectors) + " — " + "، ".join(cities),
        "daily_target": sum(c["quota"] for c in cats),
        "exclusions": exclusions,
        "exclude_existing_customers": exclude_existing,
        "categories": cats,
        "parser": "rules",
    }


TARGETING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "daily_target", "exclusions", "exclude_existing_customers", "categories"],
    "properties": {
        "name": {"type": "string"},
        "daily_target": {"type": "integer"},
        "exclusions": {"type": "array", "items": {"type": "string"}},
        "exclude_existing_customers": {"type": "boolean"},
        "categories": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["sector_key", "label", "cities", "keywords_ar", "keywords_en", "priority",
                         "priority_signals", "quota", "is_experiment"],
            "properties": {
                "sector_key": {"type": "string"},
                "label": {"type": "string"},
                "cities": {"type": "array", "items": {"type": "string"}},
                "keywords_ar": {"type": "array", "items": {"type": "string"}},
                "keywords_en": {"type": "array", "items": {"type": "string"}},
                "priority": {"type": "integer"},
                "priority_signals": {"type": "array", "items": {"type": "string"}},
                "quota": {"type": "integer"},
                "is_experiment": {"type": "boolean"},
            }}},
    },
}

_SYSTEM = """أنت محلل استهداف مبيعات لشركة «سيارة آب» السعودية (حلول إدارة أساطيل المركبات).
حوّل طلب المدير إلى إعدادات حملة. القواعد:
- استخدم مفاتيح القطاعات المعرّفة إن طابقت: {keys}. يمكنك إنشاء مفتاح جديد بالإنجليزية بصيغة snake_case إن لم يطابق أي منها.
- المدن بأسمائها العربية الرسمية (الرياض، جدة، الدمام...).
- لكل فئة اكتب 4-8 عبارات بحث عربية و3-6 إنجليزية تشمل المرادفات والأنشطة القريبة (مثال: النقل المبرد يشمل نقل الأغذية المبردة وسلاسل التبريد والتوزيع المبرد).
- الحصص: إن ذكر المدير أرقامًا لكل فئة فالتزم بها، وإلا وزّع الهدف اليومي ({target}) حسب الأولوية. مجموع الحصص = الهدف اليومي.
- exclusions: الأنشطة المستبعدة بوضوح (مثل الوسطاء ومنصات الحجز).
- إن وصف المدير طريقة تشغيل المركبات بدل القطاع (مثل «فرق ميدانية تزور العملاء») فاختر الشريحة المناسبة عبر القطاعات
  (field_services, distribution_supply, contractors_operators, own_delivery, staff_transport, fleet_partners)
  واكتب كلمات بحث تشغيلية (مثل «فرق صيانة متنقلة»، «أسطول التوزيع»، field service fleet، last-mile delivery).
- المناطق تتحول إلى مدنها ومحافظاتها، وأضف المدن الصناعية والمناطق اللوجستية إذا طلبها المدير.
- is_experiment = true للفئات التي طلب المدير «اختبارها» كشريحة جديدة.
- priority 1 هي الأعلى."""


def parse_request(text: str, daily_target: int = 400) -> dict[str, Any]:
    if ai.settings.ai_enabled:
        try:
            out = ai.ai_json(_SYSTEM.format(keys=", ".join(SECTORS), target=daily_target),
                             f"طلب المدير:\n{text}", TARGETING_SCHEMA, note="targeting")
            out["parser"] = "ai"
            out["daily_target"] = sum(int(c.get("quota") or 0) for c in out["categories"]) or daily_target
            return out
        except ai.AIUnavailable as e:
            log.warning("AI targeting failed, using rules: %s", e)
    return parse_request_rules(text, daily_target)
