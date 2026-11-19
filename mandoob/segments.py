"""شرائح الاستهداف حسب طريقة تشغيل المركبات، والنطاق الجغرافي، وإشارات التوقيت، وجهة القرار.

هذا الملف هو «قاموس» المنصة: يستخدمه المحلل الحتمي والبحث والتحقق من المطابقة.
"""
from __future__ import annotations

import re
from datetime import date

from .normalize import normalize_arabic

# ---------------------------------------------------------------- الشرائح حسب تشغيل المركبات
# operational: عبارات التشغيل التي تُبحث وتُحفظ كدليل ترشيح
USAGE_SEGMENTS: dict[str, dict] = {
    "vehicle_users": {
        "label": "أي نشاط يستخدم مركبات",
        "examples": "كل شركة أو مؤسسة أو مصنع أو مكتب نقليات أو صاحب نشاط يملك مركبة أو يشغّلها أو يتحمّل مصاريفها، ولو مركبة واحدة",
        "triggers": ["يستخدمون سيارات", "يستخدم سيارات", "يستخدمون مركبات", "سيارات في عملهم", "مركبات في عملهم",
                     "عنده سيارات", "عندهم سيارات", "يملك مركبه", "يملكون مركبات", "يشغل مركبات", "اي نشاط عنده",
                     "اصحاب الانشطه", "use vehicles", "operate vehicles"],
        "keywords_ar": ["صيانة متنقلة", "توصيل وتركيب", "توزيع مبرد", "نقل موظفين", "تأجير معدات مع النقل",
                        "سطحة لنقل السيارات", "نقل عفش", "صهاريج مياه", "مكتب نقليات", "توزيع مواد غذائية",
                        "مكافحة حشرات", "مطلوب مشرف أسطول {city}", "دليل شركات النقل {city}",
                        "قائمة العارضين معرض الأغذية {city}"],
        "keywords_en": ["mobile maintenance service", "delivery and installation", "refrigerated distribution",
                        "staff transportation", "equipment rental with transport", "tow truck service"],
        "operational": ["سياراتنا", "أسطول", "اسطول", "مركباتنا", "توصيل", "صيانة متنقلة", "نصل إليك", "سطحة", "ونش",
                        "شاحنة", "شاحنات", "باص", "حافلات", "مندوبينا", "fleet", "our vehicles", "delivery", "mobile service"],
        "decision_unit": "المالك أو إدارة التشغيل",
    },
    "field_services": {
        "label": "شركات الخدمات الميدانية",
        "examples": "التكييف، المصاعد، مكافحة الآفات، الاتصالات والأمن والسلامة",
        "triggers": ["خدمات ميدانيه", "الخدمات الميدانيه", "فرق ميدانيه", "فرق صيانه", "تزور العملاء", "صيانه متنقله", "تكييف", "مصاعد",
                     "مكافحه الافات", "مكافحه الحشرات", "انظمه الامن", "السلامه والحريق", "field service"],
        "keywords_ar": ["شركة صيانة تكييف فرق متنقلة", "شركة صيانة مصاعد", "شركة مكافحة حشرات", "شركة أنظمة أمن وسلامة تركيب وصيانة",
                        "فرق صيانة متنقلة", "عقود صيانة دورية", "خدمة طوارئ على مدار الساعة"],
        "keywords_en": ["HVAC maintenance company", "elevator maintenance company", "pest control company",
                        "field service fleet", "mobile maintenance teams"],
        "operational": ["فرق صيانة متنقلة", "فرق ميدانية", "فنيين متنقلين", "زيارات دورية", "عقود صيانة دورية",
                        "خدمة طوارئ", "على مدار الساعة", "نصل إليك", "زيارة موقع", "mobile technicians",
                        "field service", "field team", "emergency service", "preventive maintenance visits"],
        "decision_unit": "إدارة التشغيل والصيانة",
    },
    "distribution_supply": {
        "label": "شركات التوزيع والتوريد",
        "examples": "الأغذية والمشروبات والمياه والأدوية والمخابز المركزية",
        "triggers": ["توزيع", "توريد", "موزع", "مياه", "مخابز", "ادويه", "اغذيه", "مشروبات", "distribution", "distributor"],
        "keywords_ar": ["شركة توزيع مواد غذائية", "شركة مياه شرب توصيل", "شركة توزيع أدوية", "مخابز مركزية توزيع",
                        "أسطول التوزيع", "توزيع يومي على المتاجر", "مستودعات توزيع"],
        "keywords_en": ["food distribution company", "water delivery company", "pharmaceutical distributor",
                        "distribution fleet", "route sales vans"],
        "operational": ["أسطول التوزيع", "اسطول توزيع", "سيارات التوزيع", "توزيع يومي", "مندوبي التوزيع", "مندوبي مبيعات",
                        "شاحنات مبردة", "مستودعات", "مراكز توزيع", "نغطي", "distribution fleet", "route sales",
                        "van sales", "cold chain", "distribution centers"],
        "decision_unit": "إدارة الأسطول واللوجستيات",
    },
    "contractors_operators": {
        "label": "المقاولون ومشغّلو المواقع",
        "examples": "المقاولات، الطرق، التشغيل والصيانة، النظافة وإدارة المرافق",
        "triggers": ["مقاولات", "مقاول", "طرق", "تشغيل وصيانه", "نظافه", "اداره المرافق", "اداره مرافق",
                     "معدات ثقيله", "construction", "contractor", "facility management", "o&m"],
        "keywords_ar": ["شركة مقاولات", "مقاولات طرق", "شركة تشغيل وصيانة", "شركة نظافة وإدارة مرافق",
                        "تأجير معدات ثقيلة", "مشاريع في عدة مدن"],
        "keywords_en": ["construction company", "road contractor", "operation and maintenance contractor",
                        "facility management company", "heavy equipment fleet"],
        "operational": ["مشاريع في", "مواقع المشاريع", "معدات", "مركبات خدمة", "أسطول المعدات", "عقود تشغيل وصيانة",
                        "فرق النظافة", "site vehicles", "equipment fleet", "project sites", "o&m contract"],
        "decision_unit": "إدارة المشتريات والمعدات",
    },
    "own_delivery": {
        "label": "الشركات التي تدير توصيلها بنفسها",
        "examples": "المتاجر الإلكترونية، الأثاث، الأجهزة المنزلية والمطابخ",
        "triggers": ["توصيل وتركيب", "متجر الكتروني", "اثاث", "اجهزه منزليه", "مطابخ", "توصيل مجاني", "own delivery"],
        "keywords_ar": ["معرض أثاث توصيل وتركيب", "أجهزة منزلية توصيل وتركيب", "شركة مطابخ تركيب", "متجر إلكتروني توصيل خاص"],
        "keywords_en": ["furniture delivery and installation", "appliances delivery installation", "kitchen installation company"],
        "operational": ["توصيل وتركيب", "فريق التوصيل", "فريق التركيب", "سياراتنا", "توصيل مجاني", "مندوبينا",
                        "delivery and installation", "our delivery team", "in-house delivery", "last-mile delivery"],
        "decision_unit": "إدارة العمليات والتوصيل",
    },
}

# الشرائح الموجودة سابقًا التي نوسّعها (تُدمج في SECTORS)
SECTOR_OVERRIDES = {
    "staff_transport": {
        "label": "مشغّلو نقل الركاب",
        "examples": "النقل المدرسي والجامعي، نقل الموظفين، والنقل السياحي",
        "triggers": ["نقل الموظفين", "نقل موظفين", "نقل الطلاب", "نقل مدرسي", "نقل جامعي", "نقل سياحي", "حافلات",
                     "نقل الركاب", "staff transport", "school bus"],
        "keywords_ar": ["شركة نقل موظفين", "نقل مدرسي", "نقل طالبات وجامعات", "نقل سياحي حافلات", "عقود تشغيل حافلات"],
        "keywords_en": ["staff transportation company", "school bus operator", "tour bus company", "bus operation contracts"],
        "operational": ["نقل موظفين", "عقود تشغيل حافلات", "مسارات", "حافلات", "باصات", "bus fleet", "routes", "shuttle"],
        "decision_unit": "إدارة التشغيل والحركة",
    },
    "fleet_partners": {
        "label": "شركاء يخدمون أساطيل متعددة",
        "examples": "شركات تتبع المركبات، تأجير الأساطيل، ومزوّدو أنظمة إدارة النقل",
        "triggers": ["تتبع المركبات", "تتبع الاساطيل", "انظمه اداره النقل", "تاجير الاساطيل", "gps", "telematics", "tms"],
        "keywords_ar": ["شركة تتبع مركبات", "أنظمة إدارة الأساطيل", "تأجير أساطيل للشركات", "نظام إدارة النقل"],
        "keywords_en": ["vehicle tracking company", "fleet telematics provider", "fleet leasing", "transport management system"],
        "operational": ["عملاؤنا من الأساطيل", "تتبع", "أساطيل الشركات", "fleet customers", "telematics", "fleet management"],
        "decision_unit": "تطوير الأعمال والشراكات",
        "partner": True,
    },
}

DEFAULT_DECISION_UNIT = {
    "transport_logistics": "إدارة الأسطول والتشغيل", "refrigerated_transport": "إدارة الأسطول والتشغيل",
    "delivery": "إدارة العمليات", "car_rental": "إدارة الأسطول", "passenger_transport": "إدارة التشغيل",
}

# ---------------------------------------------------------------- الجغرافيا
REGIONS = {
    "المنطقة الشرقية": ["الدمام", "الخبر", "الظهران", "الجبيل", "الأحساء", "القطيف", "حفر الباطن", "رأس تنورة", "بقيق",
                        "الجبيل الصناعية", "المدينة الصناعية الثانية بالدمام", "المدينة الصناعية الثالثة بالدمام",
                        "ميناء الملك عبدالعزيز بالدمام"],
    "منطقة الرياض": ["الرياض", "الخرج", "المجمعة", "الدوادمي", "القويعية", "سدير للصناعة والأعمال",
                     "المدينة الصناعية الثانية بالرياض", "المدينة الصناعية الثالثة بالرياض",
                     "المنطقة اللوجستية الخاصة بالرياض"],
    "منطقة مكة المكرمة": ["جدة", "مكة المكرمة", "الطائف", "رابغ", "المدينة الصناعية الأولى بجدة",
                          "المنطقة اللوجستية بالخمرة", "ميناء جدة الإسلامي", "مدينة الملك عبدالله الاقتصادية"],
    "منطقة المدينة المنورة": ["المدينة المنورة", "ينبع", "ينبع الصناعية"],
    "منطقة القصيم": ["بريدة", "عنيزة", "الرس"],
    "منطقة عسير": ["أبها", "خميس مشيط"],
    "منطقة تبوك": ["تبوك"], "منطقة حائل": ["حائل"], "منطقة جازان": ["جازان", "مدينة جازان للصناعات الأساسية"],
}
REGION_TRIGGERS = {
    "المنطقة الشرقية": ["المنطقه الشرقيه", "الشرقيه", "eastern province"],
    "منطقة الرياض": ["منطقه الرياض"], "منطقة مكة المكرمة": ["منطقه مكه", "المنطقه الغربيه"],
    "منطقة المدينة المنورة": ["منطقه المدينه"], "منطقة القصيم": ["القصيم"], "منطقة عسير": ["عسير"],
    "منطقة تبوك": ["منطقه تبوك"], "منطقة حائل": ["منطقه حائل"], "منطقة جازان": ["منطقه جازان"],
}
INDUSTRIAL_HINTS = ["المدن الصناعيه", "المناطق اللوجستيه", "المدن الصناعية", "المناطق اللوجستية", "industrial"]


def place_region(place: str) -> str | None:
    for region, places in REGIONS.items():
        if place in places or place == region:
            return region
    return None


def in_coverage(place: str, coverage: list[str]) -> bool:
    """هل المكان داخل نطاق الخدمة؟ النطاق قائمة مدن/مناطق/مدن صناعية (فارغة = كل المملكة)."""
    if not coverage:
        return True
    p = normalize_arabic(place or "")
    region = place_region(place)
    for c in coverage:
        cn = normalize_arabic(c)
        if cn == p or (region and normalize_arabic(region) == cn) or (cn and cn in p):
            return True
        if c in REGIONS and place in REGIONS[c]:
            return True
    return False


# ---------------------------------------------------------------- إشارات التوقيت
SIGNAL_PATTERNS = {
    "warehouse_opening": ("افتتاح مستودع", ["افتتاح مستودع", "افتتاح مركز توزيع", "مستودع جديد", "مركز توزيع جديد",
                                           "new warehouse", "new distribution center", "opens warehouse"]),
    "new_city": ("توسع إلى مدينة جديدة", ["افتتاح فرع", "فرعنا الجديد", "التوسع في", "توسعنا", "نتوسع",
                                          "new branch", "expands to", "expansion into"]),
    "contract_win": ("فوز بعقد تشغيل", ["ترسيه", "ترسية", "الفوز بعقد", "توقيع عقد", "فازت", "awarded", "contract win",
                                        "signed a contract"]),
    "hiring_drivers": ("توظيف سائقين", ["مطلوب سائقين", "وظائف سائقين", "توظيف سائقين", "مطلوب سائق", "نبحث عن سائقين",
                                        "hiring drivers", "driver vacancies", "delivery drivers wanted"]),
    "hiring_fleet": ("توظيف مشرف أسطول", ["مشرف اسطول", "مشرف أسطول", "مدير اسطول", "مدير أسطول", "مشرف حركة",
                                          "fleet supervisor", "fleet manager vacancy", "transport supervisor"]),
}
_DATE_PATTERNS = [
    (re.compile(r"(20\d\d)[-/.](\d{1,2})[-/.](\d{1,2})"), "ymd"),
    (re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](20\d\d)"), "dmy"),
]
_AR_MONTHS = {"يناير": 1, "فبراير": 2, "مارس": 3, "ابريل": 4, "أبريل": 4, "مايو": 5, "يونيو": 6, "يوليو": 7,
              "اغسطس": 8, "أغسطس": 8, "سبتمبر": 9, "اكتوبر": 10, "أكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
              "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
              "september": 9, "october": 10, "november": 11, "december": 12}


def find_date(text: str) -> date | None:
    t = (text or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    for rx, kind in _DATE_PATTERNS:
        m = rx.search(t)
        if m:
            try:
                if kind == "ymd":
                    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                continue
    m = re.search(r"(\d{1,2})\s+([^\s\d]+)\s+(20\d\d)", t)
    if m and m.group(2).lower() in _AR_MONTHS:
        try:
            return date(int(m.group(3)), _AR_MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
    return None


def detect_signals(pages) -> list[dict]:
    """يرصد إعلانات التوسع/التوظيف/العقود في صفحات الموقع مع التاريخ (إن وُجد) والرابط."""
    out, seen = [], set()
    for pg in pages:
        text = pg.text or ""
        tn = normalize_arabic(text).lower()
        for kind, (label, phrases) in SIGNAL_PATTERNS.items():
            if kind in seen:
                continue
            for ph in phrases:
                i = tn.find(normalize_arabic(ph).lower())
                if i >= 0:
                    window = text[max(0, i - 160): i + 220]
                    out.append({"kind": kind, "label": label, "title": window.strip()[:220],
                                "announced_on": find_date(window), "url": pg.url})
                    seen.add(kind)
                    break
    return out


# ---------------------------------------------------------------- من يشغّل المركبات فعليًا؟
SELF_OPERATED = ["اسطولنا", "أسطولنا", "سياراتنا", "شاحناتنا", "فريق التوصيل الخاص", "فريق التوصيل لدينا", "مندوبينا",
                 "نمتلك اسطول", "نمتلك أسطول", "لدينا اسطول", "لدينا أسطول", "اسطول خاص", "أسطول خاص", "فريق التركيب",
                 "our fleet", "our own fleet", "our delivery team", "in-house delivery"]
CONTRACTED = [r"(?:بالتعاون|بالشراكه|بالشراكة|بالتعاقد)\s+مع\s+(?:شركه|شركة)?\s*([^\.،,\n]{3,50})",
              r"(?:شريك|شريكنا)\s+(?:الشحن|النقل|التوصيل)[:\s]+(?:شركه|شركة)?\s*([^\.،,\n]{3,50})",
              r"(?:يتم|تتم)\s+(?:الشحن|التوصيل)\s+(?:عن طريق|عبر|بواسطه|بواسطة)\s+(?:شركه|شركة)?\s*([^\.،,\n]{3,50})",
              r"(?:delivered|shipped|fulfilled)\s+by\s+([A-Z][\w&\- ]{2,40})",
              r"(?:logistics|delivery|shipping)\s+partner[:\s]+([A-Z][\w&\- ]{2,40})"]
GENERIC_CARRIER_WORDS = {"ارامكس", "aramex", "smsa", "سمسا", "dhl", "fedex", "ups", "البريد السعودي", "سبل", "spl",
                         "ناقل", "زاجل", "j&t", "imile"}


def detect_operator(pages) -> dict:
    """يعيد {'status': self/contracted/unknown, 'carrier': اسم الناقل أو None, 'evidence': نص، 'url': رابط}."""
    for pg in pages:
        text = pg.text or ""
        for rx in CONTRACTED:
            m = re.search(rx, text, flags=re.I)
            if m:
                name = m.group(1).strip(" :-–")
                return {"status": "contracted", "carrier": name, "evidence": text[max(0, m.start() - 40): m.end() + 40].strip(),
                        "url": pg.url}
    for pg in pages:
        tn = normalize_arabic(pg.text or "").lower()
        for ph in SELF_OPERATED:
            i = tn.find(normalize_arabic(ph).lower())
            if i >= 0:
                return {"status": "self", "carrier": None, "evidence": (pg.text or "")[max(0, i - 40): i + 120].strip(),
                        "url": pg.url}
    return {"status": "unknown", "carrier": None, "evidence": "", "url": None}


def find_operational_phrase(pages, phrases: list[str]) -> tuple[str, str] | None:
    """أول عبارة تشغيل وردت في الصفحات، مع رابط الصفحة."""
    for pg in pages:
        tn = normalize_arabic(pg.text or "").lower()
        for ph in phrases:
            if normalize_arabic(ph).lower() in tn:
                return ph, pg.url
    return None


# ---------------------------------------------------------------- جهة القرار والبريد الوظيفي
DEPARTMENT_EMAILS = [
    ("fleet", "إدارة الأسطول"), ("logistics", "اللوجستيات"), ("transport", "إدارة النقل"),
    ("operations", "التشغيل"), ("ops", "التشغيل"), ("procurement", "المشتريات"), ("purchasing", "المشتريات"),
    ("purchase", "المشتريات"), ("maintenance", "الصيانة"), ("facilities", "إدارة المرافق"),
]


def department_of_email(email: str) -> str | None:
    local = email.split("@", 1)[0].lower()
    for prefix, label in DEPARTMENT_EMAILS:
        if local == prefix or local.startswith(prefix + ".") or local.startswith(prefix + "_") \
                or local.startswith(prefix + "-") or local == prefix + "s":
            return label
    return None
