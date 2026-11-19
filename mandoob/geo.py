"""دليل الأماكن السعودية لأداة «البحث عن العملاء»: ١٣ منطقة إدارية ← المحافظات والمدن والمدن الصناعية.

- لكل منطقة معرّف ثابت هو رمزها في ISO 3166-2:SA (مثل SA-01 لمنطقة الرياض)، ولكل مكان معرّف ثابت
  «رمز المنطقة/الاسم اللاتيني»، ونوعه (محافظة/مقر الإمارة/مدينة صناعية) وتبعيته ومرادفاته العربية والإنجليزية.
- المصدر: التقسيم الإداري وفق نظام المناطق (المناطق الثلاث عشرة ومقارّها ومحافظاتها كما تعرضها المنصة الوطنية my.gov.sa)،
  والمدن الصناعية من قائمة «مدن» والهيئة الملكية للجبيل وينبع. أُعدّت القائمة في ٢٠٢٦-١١-١٩.
- فجوات معروفة (تُذكر في تقرير التسليم): المراكز والقرى غير مدرجة بعد؛ وتحتاج القائمة مطابقة دورية مع بيانات
  «المسميات الجغرافية» في الهيئة العامة للمساحة والمعلومات الجيومكانية (geosa.gov.sa) لإضافة المراكز وتحديث التبعية.
"""
from __future__ import annotations

import re

SOURCE = {"name": "التقسيم الإداري للمناطق والمحافظات (my.gov.sa) + قوائم المدن الصناعية (modon.gov.sa، الهيئة الملكية)",
          "fetched": "2026-11-19",
          "gaps": "المراكز والقرى غير مدرجة؛ مطلوب استيراد «المسميات الجغرافية» من geosa.gov.sa لاستكمالها"}

# (رمز المنطقة، الاسم، الاسم الإنجليزي، المقر، [(اسم المكان، الإنجليزي، النوع، [مرادفات])])
# النوع: capital = مقر الإمارة، gov = محافظة، industrial = مدينة صناعية
_RAW = [
    ("SA-01", "منطقة الرياض", "Riyadh Region", "الرياض", [
        ("الرياض", "Riyadh", "capital", ["مدينة الرياض", "Riyadh City", "Ar Riyadh"]),
        ("الدرعية", "Diriyah", "gov", ["Ad Diriyah"]), ("الخرج", "Al Kharj", "gov", ["Kharj"]),
        ("الدوادمي", "Ad Dawadmi", "gov", ["Dawadmi"]), ("المجمعة", "Al Majmaah", "gov", ["Majmaah"]),
        ("القويعية", "Al Quwayiyah", "gov", ["Quwayiyah"]), ("وادي الدواسر", "Wadi ad-Dawasir", "gov", ["Wadi Al Dawasir"]),
        ("الأفلاج", "Al Aflaj", "gov", ["Layla", "ليلى"]), ("الزلفي", "Az Zulfi", "gov", ["Zulfi"]),
        ("شقراء", "Shaqra", "gov", []), ("حوطة بني تميم", "Hotat Bani Tamim", "gov", []),
        ("عفيف", "Afif", "gov", []), ("السليل", "As Sulayyil", "gov", ["Sulayyil"]), ("ضرما", "Dhurma", "gov", []),
        ("المزاحمية", "Al Muzahimiyah", "gov", ["Muzahmiyah"]), ("رماح", "Rumah", "gov", []),
        ("ثادق", "Thadiq", "gov", []), ("حريملاء", "Huraymila", "gov", []), ("الحريق", "Al Hariq", "gov", []),
        ("الغاط", "Al Ghat", "gov", []), ("مرات", "Marat", "gov", []), ("الدلم", "Ad Dilam", "gov", ["Dilam"]),
        ("الرين", "Ar Rayn", "gov", []),
        ("المدينة الصناعية الثانية بالرياض", "Riyadh 2nd Industrial City", "industrial", ["صناعية الرياض الثانية"]),
        ("المدينة الصناعية الثالثة بالرياض", "Riyadh 3rd Industrial City", "industrial", []),
        ("مدينة سدير للصناعة والأعمال", "Sudair City for Industry and Business", "industrial", ["سدير الصناعية"]),
        ("المدينة الصناعية بالخرج", "Al Kharj Industrial City", "industrial", []),
    ]),
    ("SA-02", "منطقة مكة المكرمة", "Makkah Region", "مكة المكرمة", [
        ("مكة المكرمة", "Makkah", "capital", ["مكة", "Mecca"]), ("جدة", "Jeddah", "gov", ["جده", "Jiddah"]),
        ("الطائف", "Taif", "gov", ["At Taif"]), ("القنفذة", "Al Qunfudhah", "gov", []), ("الليث", "Al Lith", "gov", []),
        ("رابغ", "Rabigh", "gov", []), ("خليص", "Khulais", "gov", []), ("الخرمة", "Al Khurmah", "gov", []),
        ("رنية", "Ranyah", "gov", []), ("تربة", "Turabah", "gov", []), ("الجموم", "Al Jumum", "gov", []),
        ("الكامل", "Al Kamil", "gov", []), ("المويه", "Al Muwayh", "gov", []), ("ميسان", "Maysan", "gov", []),
        ("أضم", "Adham", "gov", []), ("العرضيات", "Al Ardiyat", "gov", []), ("بحرة", "Bahrah", "gov", []),
        ("المدينة الصناعية الأولى بجدة", "Jeddah 1st Industrial City", "industrial", ["صناعية جدة"]),
        ("المدينة الصناعية بمكة المكرمة", "Makkah Industrial City", "industrial", []),
    ]),
    ("SA-03", "منطقة المدينة المنورة", "Madinah Region", "المدينة المنورة", [
        ("المدينة المنورة", "Madinah", "capital", ["المدينة", "Medina", "Al Madinah"]), ("ينبع", "Yanbu", "gov", []),
        ("العلا", "AlUla", "gov", ["Al Ula"]), ("المهد", "Mahd adh Dhahab", "gov", ["مهد الذهب"]),
        ("الحناكية", "Al Hinakiyah", "gov", []), ("بدر", "Badr", "gov", []), ("خيبر", "Khaybar", "gov", []),
        ("العيص", "Al Ais", "gov", []), ("وادي الفرع", "Wadi Al Fara", "gov", []),
        ("ينبع الصناعية", "Yanbu Industrial City", "industrial", ["مدينة ينبع الصناعية"]),
        ("المدينة الصناعية بالمدينة المنورة", "Madinah Industrial City", "industrial", []),
    ]),
    ("SA-05", "منطقة القصيم", "Qassim Region", "بريدة", [
        ("بريدة", "Buraydah", "capital", ["Buraidah"]), ("عنيزة", "Unaizah", "gov", ["Unayzah"]),
        ("الرس", "Ar Rass", "gov", []), ("المذنب", "Al Mithnab", "gov", []), ("البكيرية", "Al Bukayriyah", "gov", []),
        ("البدائع", "Al Badai", "gov", []), ("الأسياح", "Al Asyah", "gov", []), ("النبهانية", "An Nabhaniyah", "gov", []),
        ("الشماسية", "Ash Shimasiyah", "gov", []), ("عيون الجواء", "Uyun al Jiwa", "gov", []),
        ("رياض الخبراء", "Riyadh Al Khabra", "gov", []), ("عقلة الصقور", "Uqlat as Suqur", "gov", []),
        ("ضرية", "Dariyah", "gov", []),
        ("المدينة الصناعية الأولى بالقصيم", "Qassim 1st Industrial City", "industrial", ["صناعية بريدة"]),
    ]),
    ("SA-04", "المنطقة الشرقية", "Eastern Province", "الدمام", [
        ("الدمام", "Dammam", "capital", []), ("الأحساء", "Al Ahsa", "gov", ["الهفوف", "Hofuf", "Al Hasa"]),
        ("حفر الباطن", "Hafar Al Batin", "gov", []), ("الجبيل", "Jubail", "gov", ["Al Jubail"]),
        ("القطيف", "Qatif", "gov", ["Al Qatif"]), ("الخبر", "Khobar", "gov", ["Al Khobar"]),
        ("الظهران", "Dhahran", "gov", []), ("الخفجي", "Khafji", "gov", []), ("رأس تنورة", "Ras Tanura", "gov", []),
        ("بقيق", "Abqaiq", "gov", ["Buqayq"]), ("النعيرية", "An Nuayriyah", "gov", []),
        ("قرية العليا", "Qaryat al Ulya", "gov", []), ("العديد", "Al Udayd", "gov", []),
        ("الجبيل الصناعية", "Jubail Industrial City", "industrial", ["مدينة الجبيل الصناعية"]),
        ("المدينة الصناعية الثانية بالدمام", "Dammam 2nd Industrial City", "industrial", []),
        ("المدينة الصناعية الثالثة بالدمام", "Dammam 3rd Industrial City", "industrial", []),
        ("المدينة الصناعية بالأحساء", "Al Ahsa Industrial City", "industrial", []),
        ("رأس الخير", "Ras Al Khair", "industrial", ["مدينة رأس الخير للصناعات التعدينية"]),
    ]),
    ("SA-14", "منطقة عسير", "Asir Region", "أبها", [
        ("أبها", "Abha", "capital", []), ("خميس مشيط", "Khamis Mushait", "gov", []), ("بيشة", "Bisha", "gov", []),
        ("النماص", "An Namas", "gov", []), ("محايل عسير", "Muhayil Asir", "gov", ["محايل"]),
        ("ظهران الجنوب", "Dhahran Al Janub", "gov", []), ("تثليث", "Tathlith", "gov", []),
        ("سراة عبيدة", "Sarat Abidah", "gov", []), ("رجال ألمع", "Rijal Alma", "gov", []),
        ("بلقرن", "Balqarn", "gov", []), ("أحد رفيدة", "Ahad Rafidah", "gov", []), ("المجاردة", "Al Majaridah", "gov", []),
        ("البرك", "Al Birk", "gov", []), ("بارق", "Bariq", "gov", []), ("تنومة", "Tanomah", "gov", []),
        ("طريب", "Tarib", "gov", []), ("الحرجة", "Al Harjah", "gov", []),
    ]),
    ("SA-07", "منطقة تبوك", "Tabuk Region", "تبوك", [
        ("تبوك", "Tabuk", "capital", []), ("الوجه", "Al Wajh", "gov", []),
        ("ضباء", "Duba", "gov", ["ضبا", "Dhuba"]), ("تيماء", "Tayma", "gov", []), ("أملج", "Umluj", "gov", []),
        ("حقل", "Haql", "gov", []), ("البدع", "Al Bad", "gov", []),
        ("المدينة الصناعية بتبوك", "Tabuk Industrial City", "industrial", []),
    ]),
    ("SA-06", "منطقة حائل", "Hail Region", "حائل", [
        ("حائل", "Hail", "capital", ["Ha'il"]), ("بقعاء", "Baqaa", "gov", []), ("الغزالة", "Al Ghazalah", "gov", []),
        ("الشنان", "Ash Shinan", "gov", []), ("الحائط", "Al Hait", "gov", []), ("السليمي", "As Sulaymi", "gov", []),
        ("الشملي", "Ash Shamli", "gov", []), ("موقق", "Mawqaq", "gov", []), ("سميراء", "Samira", "gov", []),
        ("المدينة الصناعية بحائل", "Hail Industrial City", "industrial", []),
    ]),
    ("SA-08", "منطقة الحدود الشمالية", "Northern Borders Region", "عرعر", [
        ("عرعر", "Arar", "capital", []), ("رفحاء", "Rafha", "gov", []), ("طريف", "Turaif", "gov", []),
        ("العويقيلة", "Al Uwayqilah", "gov", []),
        ("مدينة وعد الشمال للصناعات التعدينية", "Waad Al Shamal", "industrial", ["وعد الشمال"]),
    ]),
    ("SA-09", "منطقة جازان", "Jazan Region", "جازان", [
        ("جازان", "Jazan", "capital", ["جيزان", "Jizan", "Gizan"]), ("صبيا", "Sabya", "gov", []),
        ("أبو عريش", "Abu Arish", "gov", []), ("صامطة", "Samtah", "gov", []), ("بيش", "Baish", "gov", []),
        ("الدرب", "Ad Darb", "gov", []), ("الحرث", "Al Harth", "gov", []), ("ضمد", "Damad", "gov", []),
        ("الريث", "Ar Rayth", "gov", []), ("فرسان", "Farasan", "gov", []), ("الدائر", "Ad Dair", "gov", []),
        ("العارضة", "Al Aridhah", "gov", []), ("أحد المسارحة", "Ahad al Musarihah", "gov", []),
        ("العيدابي", "Al Aydabi", "gov", []), ("فيفاء", "Fayfa", "gov", []), ("الطوال", "At Tuwal", "gov", []),
        ("هروب", "Harub", "gov", []),
        ("مدينة جازان للصناعات الأساسية", "Jazan City for Primary and Downstream Industries", "industrial", []),
    ]),
    ("SA-10", "منطقة نجران", "Najran Region", "نجران", [
        ("نجران", "Najran", "capital", []), ("شرورة", "Sharurah", "gov", []), ("حبونا", "Hubuna", "gov", []),
        ("بدر الجنوب", "Badr Al Janub", "gov", []), ("يدمة", "Yadamah", "gov", []), ("ثار", "Thar", "gov", []),
        ("خباش", "Khabash", "gov", []), ("الخرخير", "Al Kharkhir", "gov", []),
        ("المدينة الصناعية بنجران", "Najran Industrial City", "industrial", []),
    ]),
    ("SA-11", "منطقة الباحة", "Al Bahah Region", "الباحة", [
        ("الباحة", "Al Bahah", "capital", ["Baha"]), ("بلجرشي", "Baljurashi", "gov", []), ("المندق", "Al Mandaq", "gov", []),
        ("المخواة", "Al Makhwah", "gov", []), ("قلوة", "Qilwah", "gov", []), ("العقيق", "Al Aqiq", "gov", []),
        ("القرى", "Al Qura", "gov", []), ("غامد الزناد", "Ghamid Al Zinad", "gov", []), ("الحجرة", "Al Hajrah", "gov", []),
        ("بني حسن", "Bani Hasan", "gov", []),
    ]),
    ("SA-12", "منطقة الجوف", "Al Jawf Region", "سكاكا", [
        ("سكاكا", "Sakaka", "capital", []), ("القريات", "Al Qurayyat", "gov", []),
        ("دومة الجندل", "Dumat Al Jandal", "gov", []), ("طبرجل", "Tabarjal", "gov", []),
        ("المدينة الصناعية بالجوف", "Al Jawf Industrial City", "industrial", []),
    ]),
]

TYPE_LABELS = {"capital": "مقر الإمارة", "gov": "محافظة", "industrial": "مدينة صناعية"}


def _slug(en: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", en.lower()).strip("-")


REGIONS: dict[str, dict] = {}
PLACES: dict[str, dict] = {}
for _rid, _ar, _en, _cap, _places in _RAW:
    REGIONS[_rid] = {"id": _rid, "ar": _ar, "en": _en, "capital": _cap, "places": []}
    for _pa, _pe, _pt, _syn in _places:
        pid = f"{_rid}/{_slug(_pe)}"
        PLACES[pid] = {"id": pid, "ar": _pa, "en": _pe, "type": _pt, "region": _rid, "syn": _syn}
        REGIONS[_rid]["places"].append(pid)


def _norm(t: str) -> str:
    t = (t or "").strip().lower()
    t = re.sub("[إأآا]", "ا", t).replace("ة", "ه").replace("ى", "ي")
    t = re.sub(r"^(مدينه|محافظه)\s+", "", t)
    t = re.sub(r"[ً-ْـ'’]", "", t)
    return re.sub(r"\s+", " ", t)


_INDEX: dict[str, list[str]] = {}
for _pid, _p in PLACES.items():
    for _n in [_p["ar"], _p["en"], *_p["syn"]]:
        _INDEX.setdefault(_norm(_n), []).append(_pid)
for _k in _INDEX:
    _INDEX[_k] = list(dict.fromkeys(_INDEX[_k]))
# أسماء أجنبية تشبه أسماء سعودية ولا تعني مكانًا سعوديًا (لا تُطابق أبدًا)
FOREIGN = {_norm(x) for x in ["Dubai", "دبي", "Abu Dhabi", "أبوظبي", "Doha", "الدوحة", "Manama", "المنامة", "Kuwait",
                               "الكويت", "Muscat", "مسقط", "Cairo", "القاهرة", "Amman", "عمّان", "Sharjah", "الشارقة"]}


def label(pid: str, with_region: bool = False) -> str:
    p = PLACES[pid]
    if not with_region:
        return p["ar"]
    return f"{p['ar']} — {REGIONS[p['region']]['ar']}"


def matches(text: str) -> list[str]:
    """كل الأماكن المطابقة لاسم (عربي أو إنجليزي أو مرادف). قد تكون أكثر من واحدة."""
    k = _norm(text)
    if not k or k in FOREIGN:
        return []
    return list(_INDEX.get(k, []))


def resolve_city(text: str) -> str | None:
    """يحوّل مدينة من عنوان منشور (مثل «حي العليا، الرياض» أو «Duba, Tabuk») إلى معرّف مكان سعودي، أو None.
    «Dubai» لا تطابق «ضباء»: المقارنة بالكلمة الكاملة، والأسماء الأجنبية مستبعدة صراحة."""
    if not text:
        return None
    parts = [x for x in re.split(r"[،,\-–—/|()]+", text) if x.strip()]
    for part in [text] + parts:
        k = _norm(part)
        if k in FOREIGN:
            return None
        hits = _INDEX.get(k)
        if hits:
            return hits[0]
    words = _norm(text)
    if any(f in words.split() for f in FOREIGN if " " not in f):
        return None
    best = None
    for k, pids in _INDEX.items():  # اسم المكان كلمات كاملة داخل العنوان
        if len(k) >= 3 and re.search(rf"(^|\s){re.escape(k)}($|\s)", words):
            if best is None or len(k) > len(best[0]):
                best = (k, pids[0])
    return best[1] if best else None


def region_of(pid: str | None) -> str | None:
    return PLACES[pid]["region"] if pid and pid in PLACES else None


# ---------------------------------------------------------------- النطاق المختار

def normalize_scope(scope: dict | None) -> dict:
    """النطاق: {"all": bool, "regions": [كاملة], "places": [محددة], "exclude": [مستثناة من منطقة كاملة]}"""
    scope = dict(scope or {})
    return {"all": bool(scope.get("all")),
            "regions": [r for r in scope.get("regions", []) if r in REGIONS],
            "places": [p for p in scope.get("places", []) if p in PLACES],
            "exclude": [p for p in scope.get("exclude", []) if p in PLACES]}


def scope_places(scope: dict) -> list[str]:
    """معرّفات الأماكن داخل النطاق، مرتبة بالتناوب بين المناطق (مكان من كل منطقة ثم التالي)،
    حتى يتوزع البحث على المناطق كلها منذ أول دفعة."""
    sc = normalize_scope(scope)
    regs = list(REGIONS) if sc["all"] else sc["regions"]
    excl = set(sc["exclude"])
    per_region: dict[str, list[str]] = {}
    for r in regs:
        per_region[r] = [p for p in REGIONS[r]["places"] if p not in excl]
    for p in sc["places"]:
        r = PLACES[p]["region"]
        if p not in per_region.setdefault(r, []) and p not in excl:
            per_region[r].append(p)
    order, i = [], 0
    lists = [per_region[r] for r in REGIONS if r in per_region]
    while any(i < len(x) for x in lists):
        for x in lists:
            if i < len(x):
                order.append(x[i])
        i += 1
    return order


def scope_regions(scope: dict) -> list[str]:
    return list(dict.fromkeys(PLACES[p]["region"] for p in scope_places(scope)))


def scope_summary(scope: dict) -> str:
    sc = normalize_scope(scope)
    if sc["all"]:
        n = len(scope_places(sc))
        return f"جميع مناطق المملكة (١٣) — {n} موقعًا" + (f"، مع استثناء {len(sc['exclude'])}" if sc["exclude"] else "")
    parts = []
    for r in sc["regions"]:
        ex = [p for p in sc["exclude"] if PLACES[p]["region"] == r]
        parts.append(REGIONS[r]["ar"] + (" كاملة" if not ex else f" (جزئي: دون {'، '.join(label(p) for p in ex)})"))
    singles = [p for p in sc["places"] if PLACES[p]["region"] not in sc["regions"]]
    if singles:
        parts.append("، ".join(f"{label(p)} فقط" for p in singles))
    return " + ".join(parts) + f" — {len(scope_places(sc))} موقعًا" if parts else ""


def legacy_scope(cities: list[str], regions: list[str]) -> dict:
    """تحويل إعداد قديم (أسماء مدن ومناطق نصية) إلى نطاق بالمعرّفات، مع الحفاظ على معناه: مدينة وحدها تبقى وحدها."""
    by_name = {v["ar"]: k for k, v in REGIONS.items()}
    regs = [by_name[r] for r in regions or [] if r in by_name]
    places = []
    for c in cities or []:
        m = matches(c)
        if m:
            places.append(m[0])
    return {"all": False, "regions": regs, "places": places, "exclude": []}


def in_scope(pid: str | None, scope: dict) -> bool:
    return bool(pid) and pid in set(scope_places(scope))
