"""Overture Maps Places — مصدر اكتشاف مستقل يسمح بالحفظ.

- البيانات مفتوحة على s3://overturemaps-us-west-2 (GeoParquet) وتُقرأ بـDuckDB بلا مفتاح ولا تكلفة. تراخيص السجلات
  (CDLA-Permissive-2.0 / Apache-2.0 / CC0) تسمح بالحفظ مع نسبة المصدر — تُحفظ رخصة كل سجل ومصدره.
- نختار المنشآت السعودية بالمدينة (مربع إحداثيات) والنشاط (تصنيف Overture + كلمات الاسم)، ونستبعد المغلقة نهائيًا
  وما لا موقع رسميًا له (روابط واتساب/روابط مختصرة/حسابات تواصل ليست موقعًا).
- هذه مرحلة الأسماء فقط: الهاتف والبريد لا يُعتمدان من Overture، بل يُفتح الموقع الرسمي للمنشأة ويُحفظ ما يُنشر فيه
  مع رابط الصفحة (المرحلة ٢ في «اكتشاف عملاء جدد»).
"""
from __future__ import annotations

import logging
import re
import threading
import urllib.request

log = logging.getLogger(__name__)

BUCKET = "overturemaps-us-west-2"
LIST_URL = f"https://{BUCKET}.s3.us-west-2.amazonaws.com/?list-type=2&prefix=release/&delimiter=/"
ATTRIBUTION = "© Overture Maps Foundation — Places"

# مربعات المدن (خط الطول من–إلى، خط العرض من–إلى) بمعرّفات دليل الأماكن في المنصة
CITY_BBOX = {
    "SA-01/riyadh": (46.45, 47.05, 24.45, 25.05), "SA-02/jeddah": (39.05, 39.35, 21.30, 21.85),
    "SA-02/makkah": (39.70, 40.00, 21.30, 21.55), "SA-03/madinah": (39.45, 39.75, 24.35, 24.60),
    "SA-04/dammam": (49.95, 50.25, 26.20, 26.50), "SA-05/buraydah": (43.85, 44.10, 26.25, 26.45),
    "SA-14/abha": (42.45, 42.60, 18.15, 18.30), "SA-07/tabuk": (36.45, 36.65, 28.30, 28.45),
    "SA-06/hail": (41.60, 41.80, 27.45, 27.60), "SA-08/arar": (40.95, 41.10, 30.92, 31.02),
    "SA-09/jazan": (42.50, 42.65, 16.85, 16.95), "SA-10/najran": (44.05, 44.30, 17.45, 17.60),
    "SA-11/al-bahah": (41.40, 41.55, 19.95, 20.10), "SA-12/sakaka": (40.15, 40.25, 29.90, 30.00),
    "SA-02/taif": (40.30, 40.55, 21.15, 21.35), "SA-04/jubail": (49.55, 49.75, 26.90, 27.10),
}

# النشاط ← كلمات تصنيف Overture (taxonomy/basic_category) وكلمات الاسم العربية والإنجليزية
ACT_RX = {
    "transport.freight": r"freight|cargo|shipping_center|logistic|trucking|vehicle_shipping|forwarding|warehouse|نقل|شحن|لوجست",
    "transport.parcels": r"courier|delivery_service|shipping_center|post_office|توصيل|طرود",
    "transport.cold_chain": r"refrigerat|cold_storage|مبرد",
    "transport.tankers": r"water_supplier|fuel|tanker|صهاريج|وايت",
    "transport.passenger": r"bus_|shuttle|charter|limo|taxi|transportation_service|نقل الركاب|ليموزين",
    "transport.movers": r"moving|mover|relocation|نقل عفش|نقل اثاث|نقل أثاث",
    "industry.food_factories": r"food_manufact|food_processing|بقوليات|مصنع اغذية|مصنع أغذية",
    "industry.water_factories": r"water_manufact|bottled_water|مياه",
    "industry.plastic_factories": r"plastic|packaging|تغليف|بلاستيك",
    "industry.furniture_factories": r"furniture_manufact|cabinet|carpent|نجارة",
    "industry.pharma_factories": r"pharmaceutical|medical_supply_manufact",
    "industry.building_material_factories": r"concrete|cement|steel|brick|block|بلوك|خرسان|حديد",
    "industry.workshops_production": r"manufactur|factory|industrial|fabricat|مصنع|ورشة",
    "construction.contractors": r"contractor|construction|مقاول|مقاولات",
    "construction.developers": r"real_estate_develop|developer|تطوير عقاري",
    "construction.engineering_offices": r"engineer|architect|هندس",
    "construction.property_management": r"property_management|real_estate_service|أملاك|عقار",
    "construction.building_suppliers": r"building_supply|hardware|lumber|construction_equipment|equipment_rental|مواد بناء",
    "field_services.maintenance": r"handyman|maintenance|repair_service|صيانة",
    "field_services.hvac": r"hvac|air_condition|تكييف",
    "field_services.elevators": r"elevator|مصاعد",
    "field_services.plumbing_electrical": r"plumb|electric|سباكة|كهرباء",
    "field_services.cleaning": r"cleaning|janitor|نظافة",
    "field_services.pest_control": r"pest|مكافحة",
    "field_services.security": r"security_service|guard|حراسة|أمن",
    "field_services.landscaping": r"landscap|garden|تنسيق حدائق",
    "trade.wholesale": r"wholesale|جملة",
    "trade.supermarkets": r"supermarket|grocery|hypermarket|سوبر ماركت|هايبر",
    "trade.distributors": r"distribut|supplier|توزيع|موزع",
    "trade.ecommerce": r"e_commerce|online_shop|متجر إلكتروني",
    "trade.furniture_appliances": r"furniture_store|appliance|electronics_store|أثاث|اجهزة",
    "trade.flowers": r"florist|flower|gift_shop|زهور|ورد",
    "food.restaurants": r"restaurant|مطعم",
    "food.cafes": r"cafe|coffee|مقهى|كافيه",
    "food.bakeries": r"bakery|مخبز|مخابز",
    "food.sweets": r"dessert|sweet|candy|حلويات",
    "food.central_kitchens": r"catering|central_kitchen|مطبخ|إعاشة|اعاشة",
    "food.catering_events": r"catering|event_planning|تموين",
    "education.private_schools": r"school|مدارس|مدرسة",
    "education.international_schools": r"international_school|عالمية",
    "education.nurseries": r"preschool|kindergarten|nursery_school|child_care|حضانة|روضة",
    "education.universities": r"college|university|جامعة|كلية",
    "education.institutes": r"training|institute|educational_service|معهد|تدريب",
    "education.school_transport": r"school_bus|نقل مدرسي|نقل طالبات|نقل طلاب",
    "health.hospitals": r"hospital|مستشفى",
    "health.clinics": r"clinic|medical_center|doctor|dentist|عيادة|مجمع طبي",
    "health.labs": r"laborator|diagnostic|مختبر",
    "health.pharmacies": r"pharmacy|drug_store|صيدلية",
    "health.home_care": r"home_health|home_care|رعاية منزلية",
    "health.medical_suppliers": r"medical_supply|medical_equipment|مستلزمات طبية",
    "automotive.dealers": r"car_dealer|truck_dealer|automotive_dealer|معرض سيارات|وكالة",
    "automotive.car_rental": r"car_rental|truck_rental|equipment_rental|تأجير|تاجير",
    "automotive.workshops": r"auto_repair|automotive_service|car_repair|ورشة|صيانة سيارات",
    "automotive.mobile_service": r"mobile_mechanic|roadside",
    "automotive.tires": r"tire|tyre|battery|كفرات|إطارات|بطاريات",
    "automotive.towing": r"towing|سطحة|سطحات",
    "agriculture.farms": r"farm|مزرعة|مزارع",
    "agriculture.nurseries_plants": r"plant_nursery|garden_center|مشتل",
    "agriculture.poultry": r"poultry|دواجن",
    "agriculture.dairy": r"dairy|ألبان|البان",
    "agriculture.fisheries": r"fish|seafood|أسماك|اسماك",
    "agriculture.agri_distributors": r"agricultur|زراعي",
    "energy.energy_mining": r"energy|mining|quarry|oil|petroleum|طاقة|تعدين|كسارة",
    "energy.water_sewage": r"water_treatment|sewage|صرف",
    "energy.waste": r"waste|recycl|نفايات|تدوير",
    "hospitality.hotels": r"hotel|resort|serviced_apartment|فندق|شقق",
    "hospitality.tour_operators": r"tour_operator|travel_agen|travel_service|سياحة|سفر",
    "hospitality.hajj_umrah": r"hajj|umrah|حج|عمرة",
    "hospitality.event_halls": r"event_venue|banquet|wedding_venue|قاعة|قصر أفراح",
    "hospitality.sports_clubs": r"gym|sports_club|fitness|نادي",
    "tech_services.telecom": r"telecom|internet_service|اتصالات",
    "tech_services.cctv": r"security_system|cctv|كاميرات",
    "tech_services.it_field_support": r"it_service|computer_repair|تقنية",
    "tech_services.consulting": r"consult|professional_service|استشار",
    "tech_services.marketing_media": r"marketing|advertis|media|تسويق|إعلان",
    "community.laundries": r"laundr|dry_clean|مغسلة",
    "community.weddings": r"wedding|أفراح",
    "community.charities": r"charity|non_profit|جمعية",
    "community.meal_aid": r"food_bank|وجبات",
}
BAD_SITE = re.compile(r"(wa\.me|whatsapp|t\.co/|bit\.ly|linktr\.ee|facebook\.|instagram\.|twitter\.|x\.com/|tiktok\.|snapchat\.|"
                      r"linkedin\.|youtube\.|google\.|goo\.gl|maps\.app|bluepages|t\.me/|salla\.sa/|zid\.store/)", re.I)

_lock = threading.Lock()
_cache: dict = {}


def latest_release() -> str:
    if _cache.get("release"):
        return _cache["release"]
    x = urllib.request.urlopen(urllib.request.Request(LIST_URL, headers={"User-Agent": "MandoobSyarahApp/1.0"}),
                               timeout=30).read().decode()
    rels = sorted(re.findall(r"<Prefix>release/([^/]+)/</Prefix>", x))
    if not rels:
        raise RuntimeError("Overture: لا إصدارات منشورة")
    _cache["release"] = rels[-1]
    return rels[-1]


def _con():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2'; SET memory_limit='300MB'; SET threads=2;")
    return con


def city_rows(pid: str) -> list[dict]:
    """كل منشآت المدينة ذات الموقع الرسمي (مخزنة مؤقتًا في الذاكرة لدفعة اليوم)."""
    if pid not in CITY_BBOX:
        return []
    key = ("rows", pid)
    with _lock:
        if key in _cache:
            return _cache[key]
        rel = latest_release()
        x0, x1, y0, y1 = CITY_BBOX[pid]
        src = f"read_parquet('s3://{BUCKET}/release/{rel}/theme=places/type=place/*', hive_partitioning=1)"
        q = f"""select id, names.primary as name, basic_category as bc, taxonomy.primary as tp, websites, phones, emails,
                   addresses[1].locality as locality, operating_status as os, confidence,
                   sources[1].license as lic, sources[1].dataset as ds, (bbox.ymin+bbox.ymax)/2 as lat, (bbox.xmin+bbox.xmax)/2 as lon
                from {src}
                where bbox.xmin between {x0} and {x1} and bbox.ymin between {y0} and {y1}
                  and websites is not null and coalesce(operating_status,'open') <> 'permanently_closed'"""
        con = _con()
        try:
            cols = [d[0] for d in con.execute(q).description]
            rows = [dict(zip(cols, r)) for r in con.fetchall()]
        finally:
            con.close()
        for r in rows:
            r["release"] = rel
        _cache[key] = rows
        return rows


def _site(webs) -> str | None:
    for w in webs or []:
        w = (w or "").strip().strip("‎‏")
        if w and not BAD_SITE.search(w):
            return w if w.startswith("http") else "https://" + w
    return None


def search(pid: str, activity_key: str, limit: int = 200) -> list[dict]:
    """منشآت النشاط في المدينة بصيغة مرحلة الأسماء في «اكتشاف عملاء جدد»."""
    from .activities import ACTIVITIES
    from .geo import PLACES
    rx = re.compile(ACT_RX.get(activity_key) or re.escape(activity_key.split(".")[-1]), re.I)
    act = ACTIVITIES.get(activity_key, {})
    out = []
    for r in city_rows(pid):
        hay = f"{r.get('tp') or ''} {r.get('bc') or ''} {r.get('name') or ''}"
        if not rx.search(hay):
            continue
        site = _site(r.get("websites"))
        if not site or not (r.get("name") or "").strip():
            continue
        out.append({"name": r["name"].strip()[:300], "activity": act.get("label") or activity_key,
                    "city": PLACES.get(pid, {}).get("ar", ""), "website": site,
                    "found_url": f"https://explore.overturemaps.org/#17/{r['lat']:.5f}/{r['lon']:.5f}",
                    "maps_url": "", "social_url": "", "external_id": f"overture:{r['id']}",
                    "vehicle_hint": "", "license": r.get("lic") or "", "dataset": r.get("ds") or "",
                    "release": r.get("release")})
        if len(out) >= limit:
            break
    return out


def available() -> tuple[bool, str]:
    try:
        import duckdb  # noqa: F401
    except ImportError:
        return False, "مكتبة DuckDB غير مثبتة على الخادم"
    return True, "بيانات مفتوحة بلا مفتاح"
