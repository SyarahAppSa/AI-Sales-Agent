"""«العملاء المحتملون»: قائمة يومية لمنشآت سعودية جديدة قابلة للتواصل، مرتبة حسب ملاءمتها لسيارة آب.

المسار لكل منشأة:
١) الجمع من دليل عام يسمح بالزحف (الصفحات الزرقاء bluepages.com.sa — robots.txt: Allow /): الاسم، النشاط، المدينة،
   الموقع، وقنوات التواصل المنشورة في بطاقة المنشأة، مع رابط صفحة الدليل.
٢) منع التكرار قبل أي عمل إضافي: الرقم الوطني الموحد ← النطاق ← الهاتف/البريد ← الاسم والمدينة، ومع عملائنا الحاليين.
٣) التحقق: زيارة موقع المنشأة نفسها (دون تكلفة ذكاء اصطناعي) لتأكيد هاتف الأعمال/البريد الرسمي، والتقاط دليل
   تشغيل المركبات وعدد الأسطول (فقط إن نُشر رقم) والرقم الوطني الموحد إن ظهر.
٤) المنشأة **قابلة للتواصل** إن كانت لها وسيلة مؤسسية واحدة على الأقل منشورة باسمها:
   - «موثّقة من موقعها»: ظهرت في موقع المنشأة نفسها.
   - «منشورة في الدليل»: في بطاقة المنشأة بالدليل (والبريد بنطاق موقعها أو بلا موقع).
   ومن لا وسيلة لها تُحفظ في «تحتاج استكمال».
٥) الأولوية: كل القطاعات مقبولة، والأعلى لمن ينشر دليلًا على تشغيل مركبات/أسطول/فرق ميدانية — مع الجملة ورابطها.
"""
from __future__ import annotations

import html as _html
import json
import logging
import random
import re
import time
from datetime import date, timedelta
from urllib.parse import quote, unquote

import httpx
from sqlalchemy import func, or_, select

from .db import session_scope
from .models import Company, Contact, ProspectRun, Setting, User
from .normalize import domain_of, normalize_email, normalize_name, normalize_phone
from .timeutil import now_utc, riyadh_day_bounds_utc, riyadh_today

log = logging.getLogger(__name__)

DAILY_TARGET = 1000
BP = "https://bluepages.com.sa"
UA = "Mozilla/5.0 (compatible; SyarahAppSalesAgent/1.0; +https://mandoob.live)"
DELAY = 1.2  # ثانية بين طلبات الدليل (زحف مهذب)
RETRY_WAIT = 20  # ثانية × رقم المحاولة قبل إعادة صفحة تعذّرت (انقطاع المصدر)
PAGE_TRIES = 3   # صفحة تتعذر ٣ مرات تُترك لدفعة اليوم التالي
SOURCE_LABEL = {"import": "ملف مزوّد بيانات مرخّص",
                "google_places": "خرائط Google (الواجهة الرسمية) — الاكتشاف فقط، والتواصل من موقع المنشأة",
                "bluepages": "دليل الصفحات الزرقاء (bluepages.com.sa)",
                "earabicmarket": "دليل الشركات السعودية — السوق العربي (earabicmarket.com)"}
EAM = "https://www.earabicmarket.com"
EAM_START = EAM + "/ar/companies/saudi-arabia/all"
LEVELS = {"high": "عالية", "medium": "متوسطة", "low": "منخفضة"}
CONTACT_LEVELS = {"website": "موثّقة من موقع المنشأة", "directory": "منشورة في الدليل باسم المنشأة",
                  "provider": "من ملف المزوّد المرخّص"}
AUTO_KEY = "prospects_auto"
AUTO_DEFAULT = {"enabled": False, "time": "05:30", "target": DAILY_TARGET, "google_budget_usd": 10.0,
                "sources": "import,google_places,bluepages,earabicmarket"}

# تصنيف نشاط الدليل إلى قطاعات المنصة (كل القطاعات مقبولة؛ هذا للتصفية والترتيب فقط)
SECTOR_WORDS = [
    ("transport", ["لوجست", "نقل", "شحن", "تخليص", "شاحن", "ليموزين", "تأجير سيارات", "تاجير سيارات", "سيارات", "حافلات"]),
    ("industry", ["مصانع", "مصنع", "صناع", "انتاج", "إنتاج", "طباعة", "تغليف", "بلاستيك", "حديد", "اسمنت", "أسمنت"]),
    ("construction", ["مقاول", "انشاء", "إنشاء", "عقار", "هندسية", "مصاعد", "خرسان", "طرق"]),
    ("trade", ["توزيع", "جملة", "تجارة", "استيراد", "تصدير", "مواد", "معدات", "اجهزة", "أجهزة", "قطع غيار", "كيميائية"]),
    ("food", ["أغذية", "اغذية", "مطاعم", "مخابز", "حلويات", "قهوة", "مياه", "ألبان", "البان", "لحوم", "تموين", "ضيافة"]),
    ("health", ["مستشفى", "مستشفيات", "عيادات", "طبي", "صيدل", "مختبر", "صحة", "تجميل"]),
    ("education", ["مدارس", "تعليم", "معاهد", "تدريب", "جامعة", "حضانة"]),
    ("field_services", ["صيانة", "تشغيل", "نظافة", "مغاسل", "مكافحة", "امن", "أمن", "حراسة", "تكييف", "سباكة", "كهرباء"]),
    ("hospitality", ["فنادق", "سياحة", "سفر", "منتجعات", "فعاليات", "معارض", "مؤتمرات"]),
    ("tech_services", ["تقنية", "برمجيات", "اتصالات", "استشارات", "محاسب", "محامون", "تسويق", "دعاية", "اعلان", "إعلان"]),
]
SECTOR_BOOST = {"transport": 18, "trade": 14, "food": 12, "construction": 12, "industry": 12, "field_services": 12,
                "health": 6, "education": 6, "hospitality": 4, "tech_services": 0}
STRONG = ["أسطول", "اسطول", "شاحنات", "شاحنة", "مركبات", "سيارات نقل", "سيارات توصيل", "سيارات مبردة", "برادات",
          "دينات", "حافلات", "صهاريج", "سطحات", "fleet", "trucks", "vehicles", "lorries", "vans"]
WEAK = ["توصيل", "خدمة التوصيل", "نوصل", "فرق ميدانية", "فريق ميداني", "فنيين", "فنيون", "زيارات ميدانية", "مندوبين",
        "تركيب", "delivery", "field team", "technicians", "logistics"]
FLEET_RX = re.compile(r"(\d{2,5})\s*(?:\+\s*)?(?:شاحنة|شاحنات|مركبة|مركبات|سيارة|سيارات|حافلة|حافلات|trucks?|vehicles?|vans?)",
                      re.I)
UNIFIED_RX = re.compile(r"(?:الموحد|الوطني الموحد|unified(?:\s+national)?\s+number)[^0-9]{0,30}(7\d{9})", re.I)
CR_RX = re.compile(r"(?:سجل تجاري|السجل التجاري|C\.?\s?R\.?(?:\s*No\.?)?)[^0-9]{0,20}([1-7]\d{9})", re.I)


# ---------------------------------------------------------------- الدليل

def _get(url: str, client: httpx.Client) -> str:
    time.sleep(DELAY)
    r = client.get(url, timeout=40, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _flight(page: str) -> str:
    """نص بيانات الصفحة (Next.js) بعد فك الترميز."""
    out = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', page, re.S):
        try:
            out.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            continue
    return "".join(out)


def parse_listing(page: str, url: str) -> list[dict]:
    """بطاقات المنشآت المنشورة في صفحة نشاط/مدينة بالدليل."""
    data = _flight(page)
    i = data.find('"initialData":{"cards":{"items":')
    if i < 0:
        return []
    j = data.find("[", i)
    try:
        items, _ = json.JSONDecoder().raw_decode(data[j:])
    except ValueError:
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = (it.get("nameAr") or it.get("nameEn") or "").strip()
        if not name:
            continue
        phones = [it.get("standardPhone"), it.get("hotline"), it.get("whatsapp")]
        for p in it.get("phones") or []:
            phones.append(p.get("phone") if isinstance(p, dict) else p)
        desc = re.sub(r"<[^>]+>", " ", _html.unescape(it.get("descriptionAr") or it.get("descriptionEn") or ""))
        cats = [c.get("nameAr") or c.get("nameEn") for c in it.get("categories") or [] if isinstance(c, dict)]
        out.append({"external_id": f"bluepages:{it.get('id')}", "name": name[:300], "name_en": it.get("nameEn"),
                    "website": (it.get("website") or "").strip() or None, "email": (it.get("email") or "").strip() or None,
                    "phones": [p for p in phones if p], "city": ((it.get("city") or {}).get("nameAr") or ""),
                    "activity": "، ".join([c for c in cats if c]) or "", "description": re.sub(r"\s+", " ", desc).strip()[:2000],
                    "listing_url": url, "verified_by_directory": bool(it.get("verified"))})
    return out


def listing_pages(client: httpx.Client) -> list[str]:
    """صفحات «مدينة/نشاط» من خريطة الموقع العامة (sitemap.xml)."""
    sm = _get(f"{BP}/sitemap.xml", client)
    urls = []
    for loc in re.findall(r"<loc>([^<]+)</loc>", sm):
        path = unquote(loc.replace(BP, "")).strip("/")
        parts = path.split("/")
        if len(parts) == 2 and parts[0] not in ("en", "blog", "services") and parts[1] != "categories":
            urls.append(loc)
    return urls


def _plan(urls: list[str], day: date) -> list[str]:
    """تنويع المدن والأنشطة: ترتيب ثابت ليوم معيّن ويتغير يوميًا، بالتناوب بين المدن."""
    rnd = random.Random(day.toordinal())
    by_city: dict[str, list[str]] = {}
    for u in urls:
        by_city.setdefault(unquote(u.replace(BP, "")).strip("/").split("/")[0], []).append(u)
    for v in by_city.values():
        rnd.shuffle(v)
    cities = list(by_city)
    rnd.shuffle(cities)
    out = []
    while any(by_city.values()):
        for c in cities:
            if by_city[c]:
                out.append(by_city[c].pop())
    return out


def parse_eam(page: str, url: str) -> tuple[list[dict], list[str]]:
    """بطاقات «دليل الشركات السعودية» في السوق العربي + روابط الصفحات التالية."""
    fields: dict[int, dict] = {}
    for m in re.finditer(r'id="dlCompanies_(\w+?)_(\d+)"[^>]*>([^<]*)<', page):
        fields.setdefault(int(m.group(2)), {})[m.group(1)] = _html.unescape(m.group(3)).strip()
    for m in re.finditer(r'id="dlCompanies_lnkWebSite_(\d+)"[^>]*href="([^"]+)"', page):
        fields.setdefault(int(m.group(1)), {})["_web"] = _html.unescape(m.group(2))
    out = []
    for i, f in sorted(fields.items()):
        name = next((v for k, v in f.items() if "name" in k.lower() and v), "")
        if not name or "شركتك هنا" in name:
            continue
        web = f.get("_web")
        out.append({"external_id": f"eam:{normalize_name(name)}:{f.get('lblCity', '')}", "name": name[:300], "name_en": None,
                    "website": web if web and web.startswith("http") else (("http://" + web) if web else None),
                    "email": None, "phones": [f["lblTelephone"]] if f.get("lblTelephone") else [],
                    "city": f.get("lblCity", ""), "activity": next((v for k, v in f.items() if "activ" in k.lower() or "categ" in k.lower()), ""),
                    "description": f.get("lblAddress", ""), "listing_url": url, "verified_by_directory": False})
    nxt = [_html.unescape(h) for h in re.findall(r'href="(https://www\.earabicmarket\.com/ar/companies/saudi-arabia/all/search\?page=\d+[^"]*)"', page)]
    return out, nxt


# ---------------------------------------------------------------- ملف مزوّد مرخّص

COLS = {"name": ["name", "company", "company_name", "trade_name", "الاسم", "اسم المنشأة", "الاسم التجاري", "اسم الشركة", "المنشأة"],
        "name_en": ["name_en", "english_name", "الاسم بالإنجليزية", "الاسم الانجليزي"],
        "activity": ["activity", "sector", "industry", "category", "النشاط", "القطاع", "التصنيف"],
        "city": ["city", "المدينة", "location", "الموقع الجغرافي"],
        "website": ["website", "web", "url", "site", "الموقع", "الموقع الإلكتروني", "الموقع الالكتروني"],
        "phone": ["phone", "telephone", "tel", "mobile", "الهاتف", "رقم الهاتف", "الجوال", "هاتف"],
        "email": ["email", "e-mail", "mail", "البريد", "البريد الإلكتروني", "البريد الالكتروني"],
        "unified": ["unified_number", "unified", "national_number", "الرقم الموحد", "الرقم الوطني الموحد"],
        "cr": ["cr", "cr_number", "commercial_registration", "السجل التجاري", "رقم السجل", "رقم السجل التجاري"],
        "source_url": ["source_url", "source", "link", "رابط المصدر", "المصدر"]}


def _map_headers(headers: list[str], partial: bool = True) -> dict[str, int]:
    norm = [re.sub(r"\s+", " ", (h or "").strip().lower()) for h in headers]
    out = {}
    for key, names in COLS.items():
        for i, h in enumerate(norm):
            if h in [n.lower() for n in names]:
                out[key] = i
                break
    # تطابق جزئي للعناوين غير المعتادة (مثل «رقم الجوال» أو «اسم المؤسسة/الشركة»)
    PART = {"name": ["اسم", "name", "company", "المنشأه", "المنشأة", "الشركة", "الشركه", "المؤسسة"],
            "phone": ["جوال", "هاتف", "تلفون", "phone", "mobile", "tel", "واتس", "contact no"],
            "email": ["بريد", "email", "e-mail", "ايميل", "إيميل"], "website": ["موقع الكتروني", "موقع إلكتروني", "website", "web"],
            "city": ["مدينة", "المدينه", "city"], "activity": ["نشاط", "قطاع", "activity", "sector", "category"],
            "unified": ["موحد", "unified"], "cr": ["سجل", "c.r", "cr no"]}
    for key, words in (PART.items() if partial else []):
        if key in out:
            continue
        for i, h in enumerate(norm):
            if i in out.values() or not h:
                continue
            if any(w in h for w in words) and not (key == "name" and any(x in h for x in ("انجليز", "english", "مسؤول", "المالك"))):
                out[key] = i
                break
    return out


class UploadError(ValueError):
    """سبب واضح بالعربية لتعذّر قراءة الملف."""


def _sheets(filename: str, data: bytes) -> list[list[list[str]]]:
    """كل أوراق الملف كصفوف نصية: xlsx/xlsm، xls القديم، CSV/TXT (UTF-8 أو ترميز ويندوز العربي، فاصلة أو ; أو Tab)."""
    import io
    low = filename.lower()
    if low.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:  # noqa: BLE001
            raise UploadError("ملف Excel تالف أو محمي بكلمة مرور — افتحه واحفظه من جديد بصيغة xlsx") from e
        return [[[("" if v is None else str(v)).strip() for v in r] for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets]
    if low.endswith(".xls") or data[:4] == b"\xd0\xcf\x11\xe0":
        import xlrd
        try:
            book = xlrd.open_workbook(file_contents=data)
        except Exception as e:  # noqa: BLE001
            raise UploadError("ملف Excel القديم (xls) لم يُقرأ — احفظه بصيغة xlsx وأعد رفعه") from e
        return [[[str(sh.cell_value(i, j)).strip() for j in range(sh.ncols)] for i in range(sh.nrows)] for sh in book.sheets()]
    if low.endswith((".numbers", ".pages", ".docx", ".doc")):
        raise UploadError("هذه الصيغة غير مدعومة — من Numbers أو Word: «ملف ← تصدير إلى ← Excel أو CSV» ثم ارفعه")
    import csv
    for enc in ("utf-8-sig", "cp1256", "utf-16"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UploadError("ترميز الملف غير معروف — احفظه بصيغة CSV UTF-8")
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [[[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]]


def _guess_columns(rows: list[list[str]]) -> dict[str, int]:
    """بلا صف عناوين معروف: الهاتف = العمود الأكثر أرقامًا هاتفية، البريد = الأكثر عناوين بريد، الاسم = أطول نص عربي/لاتيني."""
    width = max((len(r) for r in rows[:200]), default=0)
    sample = rows[:200]
    def score(j, fn):
        return sum(1 for r in sample if j < len(r) and fn(r[j]))
    m = {}
    ph = max(range(width), key=lambda j: score(j, lambda v: bool(PHONE_RX.search(normalize_arabic_digits(v)))), default=None)
    if ph is not None and score(ph, lambda v: bool(PHONE_RX.search(normalize_arabic_digits(v)))) >= 2:
        m["phone"] = ph
    em = max(range(width), key=lambda j: score(j, lambda v: bool(EMAIL_RX.search(v))), default=None)
    if em is not None and score(em, lambda v: bool(EMAIL_RX.search(v))) >= 2:
        m["email"] = em
    textual = [j for j in range(width) if j not in m.values()]
    if textual:
        nm = max(textual, key=lambda j: score(j, lambda v: bool(re.search(r"[\u0600-\u06FFA-Za-z]{3,}", v)) and not EMAIL_RX.search(v)
                                              and not v.startswith("http")))
        if score(nm, lambda v: bool(re.search(r"[\u0600-\u06FFA-Za-z]{3,}", v))) >= 2:
            m["name"] = nm
    return m


def parse_upload(filename: str, data: bytes) -> tuple[list[dict], dict]:
    """CSV أو Excel (كل الأوراق) بعناوين عربية أو إنجليزية — صف العناوين في أي من أول ١٥ صفًا، أو تخمين الأعمدة
    إن لم توجد عناوين. يعيد الصفوف الموحدة والأعمدة التي تعرّف عليها."""
    out, used = [], {}
    for rows in _sheets(filename, data):
        rows = [r for r in rows if any(c for c in r)]
        if not rows:
            continue
        hdr_i, m = None, {}
        for i, r in enumerate(rows[:15]):
            mm = _map_headers(r)
            if "name" in mm and len(mm) >= 2 and not any(PHONE_RX.search(normalize_arabic_digits(c)) for c in r):
                hdr_i, m = i, mm
                break
        if hdr_i is None:
            m = _guess_columns(rows)
            if "name" not in m or not ({"phone", "email"} & set(m)):
                continue
            body = rows
        else:
            body = rows[hdr_i + 1:]
        used = used or m
        for r in body:
            get = lambda k: (r[m[k]].strip() if k in m and m[k] < len(r) and r[m[k]] else "")  # noqa: E731
            if not get("name") or _map_headers(r, partial=False).get("name") is not None:
                continue
            out.append({k: get(k) for k in COLS})
    return out, used


PHONE_RX = re.compile(r"(?:\+?\s?966|00966)?[\s\-]*\(?0?(?:5\d|1\d|9200|800)\)?[\d\s\-]{6,11}\d")
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def parse_pdf(data: bytes) -> list[dict]:
    """قائمة منشآت من ملف PDF: كل سطر فيه هاتف أو بريد صفٌّ؛ الاسم بقية السطر أو السطر الذي قبله."""
    import io
    import unicodedata
    from pypdf import PdfReader
    rows, prev, chars = [], "", 0
    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            raise UploadError("ملف PDF محمي بكلمة مرور — احفظ نسخة بلا حماية")
        pages = reader.pages
    except UploadError:
        raise
    except Exception as e:  # noqa: BLE001
        raise UploadError("ملف PDF تالف أو غير مكتمل التنزيل — نزّله مرة أخرى") from e
    for page in pages:
        text = unicodedata.normalize("NFKC", page.extract_text() or "")
        chars += len(text.strip())
        for line in text.splitlines():
            line = normalize_arabic_digits(re.sub(r"\s+", " ", line).strip())
            if not line:
                continue
            phones = [p.strip() for p in PHONE_RX.findall(line)]
            emails = EMAIL_RX.findall(line)
            if not phones and not emails:
                prev = line
                continue
            rest = EMAIL_RX.sub(" ", PHONE_RX.sub(" ", line))
            rest = re.sub(r"[\d|:;,\-–—]+", " ", rest)
            rest = re.sub(r"(?i)\b(?:الهاتف|هاتف|جوال|الجوال|تليفون|فاكس|البريد|بريد|tel|phone|mobile|fax|email|e-mail)\b", " ", rest)
            name = re.sub(r"\s+", " ", rest).strip() or prev
            if len(name) < 3:
                name = prev
            rows.append({"name": name[:300], "phone": ";".join(phones), "email": emails[0] if emails else "",
                         "city": "", "activity": "", "website": "", "unified": "", "cr": "", "name_en": "", "source_url": ""})
            prev = ""
    if not rows and chars < 20:
        raise UploadError("ملف PDF مصوَّر (صور ممسوحة بلا نص) — لا يمكن قراءة الأرقام منه آليًا. "
                          "إن كان لديك نسخة Excel أو Word منه فارفعها")
    return rows


def queue_upload(s, user, filename: str, data: bytes, license_note: str, kind: str = "provider",
                 purchase_id: int | None = None) -> tuple[int, dict]:
    from .models import ProspectPool
    if filename.lower().endswith(".pdf"):
        rows, m = parse_pdf(data), {"name": 0, "phone": 1, "email": 2}
    else:
        rows, m = parse_upload(filename, data)
    from .normalize import normalize_phone
    seen = set()
    for (row,) in s.execute(select(ProspectPool.row)):
        for p in str((row or {}).get("phone") or "").split(";"):
            if (pp := normalize_phone(normalize_arabic_digits(p))):
                seen.add(pp)
        if (row or {}).get("name") and not (row or {}).get("phone"):
            seen.add("n:" + normalize_name(row["name"]))
    kept = []
    for r in rows:
        ps = {pp for p in str(r.get("phone") or "").split(";") if (pp := normalize_phone(normalize_arabic_digits(p)))}
        key = ps or {"n:" + normalize_name(r.get("name") or "")}
        if key & seen:
            continue
        seen |= key
        kept.append(r)
    if purchase_id:
        from .models import DataPurchase
        dp = s.get(DataPurchase, purchase_id)
        dp.rows, dp.queued, dp.duplicate_in_file = len(rows), len(kept), len(rows) - len(kept)
    rows = kept
    for r in rows:
        s.add(ProspectPool(file_name=filename[:200], license_note=license_note[:500], row=r, kind=kind,
                           status="random" if kind == "random" else "queued", uploaded_by=user.id if user else None,
                           purchase_id=purchase_id))
    return len(rows), m


def purchases_report(s) -> list[dict]:
    """عيّنات البيانات المشتراة: الجديد والمكرر والناقص، ثم ما وصل إليه الفريق والاجتماعات وتكلفتها."""
    from .models import CompanySource, DataPurchase, Meeting, ProspectPool
    out = []
    for dp in s.scalars(select(DataPurchase).order_by(DataPurchase.id.desc()).limit(30)):
        pools = list(s.execute(select(ProspectPool.id, ProspectPool.result, ProspectPool.status)
                               .where(ProspectPool.purchase_id == dp.id)))
        res = {}
        for _, r, st in pools:
            res[r or ("queued" if st == "queued" else "other")] = res.get(r or ("queued" if st == "queued" else "other"), 0) + 1
        ext = [f"import:{pid}" for pid, *_ in pools]
        cids = [c for (c,) in s.execute(select(CompanySource.company_id).where(CompanySource.external_id.in_(ext or ["-"])))] \
            if ext else []
        cids = list(dict.fromkeys(cids))
        reached = s.scalar(select(func.count(Company.id)).where(Company.id.in_(cids or [0]),
                                                                Company.reached_dm_at.isnot(None))) or 0
        meetings = s.scalar(select(func.count(Meeting.id)).where(Meeting.company_id.in_(cids or [0]),
                                                                 Meeting.status != "cancelled")) or 0
        out.append({"p": dp, "new_ready": res.get("ready", 0), "needs": res.get("needs", 0),
                    "duplicate": res.get("duplicate", 0) + res.get("existing_customer", 0) + (dp.duplicate_in_file or 0),
                    "queued": res.get("queued", 0), "companies": len(cids), "reached": reached, "meetings": meetings,
                    "cost_per_meeting": round(dp.cost_usd / meetings, 2) if meetings else None})
    return out


def _pool_records(n: int) -> list[tuple[int, dict]]:
    from .models import ProspectPool
    with session_scope() as s:
        items = list(s.scalars(select(ProspectPool).where(ProspectPool.status == "queued").order_by(ProspectPool.id).limit(n)))
        # تحقق «عملاء عشوائيون» يُعامل كالملف لكن مصدره قائمة غير موثّقة
        out = []
        for p in items:
            r = p.row or {}
            uni = re.sub(r"\D", "", normalize_arabic_digits(r.get("unified") or ""))
            out.append((p.id, {"external_id": f"import:{p.id}", "name": r.get("name", "")[:300], "name_en": r.get("name_en") or None,
                               "website": r.get("website") or None, "email": r.get("email") or None,
                               "phones": [x for x in re.split(r"[;,/|]", r.get("phone") or "") if x.strip()],
                               "city": r.get("city") or "", "activity": r.get("activity") or "", "description": "",
                               "listing_url": (r.get("source_url") or f"file://{p.file_name}#{p.id}")[:500],
                               "unified": uni if re.fullmatch(r"7\d{9}", uni) else None,
                               "cr": re.sub(r"\D", "", r.get("cr") or "") or None}))
        return out


def normalize_arabic_digits(t: str) -> str:
    return t.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))


# ---------------------------------------------------------------- خرائط Google (الاكتشاف فقط)

def google_pairs(day: date) -> list[tuple[str, str]]:
    """(النشاط، المدينة) لكل الأنشطة والمدن، بترتيب يتغير يوميًا ويتناوب بين القطاعات."""
    from .activities import ACTIVITIES
    from .geo import PLACES
    cities = [p["ar"] for p in PLACES.values() if p.get("type") in ("capital", "city", "governorate")] or \
        [p["ar"] for p in PLACES.values()]
    acts = [(v.get("q") or [v["label"]])[0] for v in ACTIVITIES.values()]
    rnd = random.Random(day.toordinal())
    rnd.shuffle(acts)
    rnd.shuffle(cities)
    return [(a, c) for c in cities for a in acts]


def google_spent_today(s) -> float:
    from .models import CostEntry
    return float(s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(
        CostEntry.day == riyadh_today(), CostEntry.service == "google_places")) or 0)


# ---------------------------------------------------------------- التشغيل اليومي

def get_auto(s) -> dict:
    row = s.get(Setting, AUTO_KEY)
    cfg = {**AUTO_DEFAULT, **((row.value if row else {}) or {})}
    from .goal import target as _goal
    cfg["target"] = _goal(s)  # الهدف اليومي الموحّد
    cfg["sources"] = ",".join(x for x in str(cfg.get("sources") or "").split(",") if x and x != "google_places")
    return cfg


def set_auto(s, **kw) -> dict:
    cfg = {**get_auto(s), **kw}
    row = s.get(Setting, AUTO_KEY)
    if row is None:
        s.add(Setting(key=AUTO_KEY, value=cfg))
        s.flush()
    else:
        row.value = cfg
    return cfg


def schedule_daily() -> bool:
    """يطلب دفعة اليوم مرة واحدة بعد الموعد المحدد (توقيت الرياض)."""
    from .timeutil import to_riyadh
    with session_scope() as s:
        cfg = get_auto(s)
        if not cfg.get("enabled"):
            return False
        now = to_riyadh(now_utc())
        hh, mm = (int(x) for x in str(cfg.get("time", "05:30")).split(":"))
        if (now.hour, now.minute) < (hh, mm):
            return False
        a, b = riyadh_day_bounds_utc(riyadh_today())
        today_runs = s.scalars(select(ProspectRun).where(ProspectRun.created_at >= a - timedelta(hours=3)))
        if any((r.stats or {}).get("_auto") == riyadh_today().isoformat() for r in today_runs):
            return False
        if s.scalar(select(ProspectRun.id).where(ProspectRun.status.in_(["pending", "running"]))):
            return False
        r = ProspectRun(status="pending", target=int(cfg.get("target") or DAILY_TARGET), sources=cfg["sources"],
                        stats={"_auto": riyadh_today().isoformat()}, log=[])
        s.add(r)
        return True


# ---------------------------------------------------------------- التصنيف والأولوية

def sector_of(activity: str) -> str:
    a = activity or ""
    for key, words in SECTOR_WORDS:
        if any(w in a for w in words):
            return key
    return "community"


STRONG_RX = [
    re.compile(r"(?:أسطول|اسطول)\w*\s+(?:من|يضم|يتجاوز|يزيد|مكون|حديث|كبير|متكامل|خاص|ضخم|متنوع|مجهز)", re.I),
    re.compile(r"(?:نمتلك|نملك|لدينا|لدينا|نشغل|نشغّل|نوفر|تمتلك|تملك|تشغّل|تشغل|يضم)\s+(?:\S+\s+){0,2}(?:أسطول|اسطول|شاحنات|حافلات|مركبات|سيارات|صهاريج|برادات|سطحات)", re.I),
    re.compile(r"(?:إدارة|ادارة|الإدارة|الادارة|تشغيل)\s+(?:\S+\s+){0,4}(?:ل|ب|لل|ال)?(?:أسطول|اسطول|أساطيل|اساطيل)", re.I),
    re.compile(r"\b(?:our|a|own|operate|operates|operating|modern|large)\s+fleet\b|\bfleet\s+of\b", re.I),
    re.compile(r"(?:سيارات|شاحنات|مركبات)\s+(?:مبردة|مجهزة|نقل|توصيل|خدمة)", re.I),
]
WEAK_RX = [re.compile(r"(?:خدمة|خدمات)\s+(?:التوصيل|النقل)|نوصل|فرق\s+ميدانية|فريق\s+ميداني|زيارات\s+ميدانية|"
                      r"\bdelivery\s+service|\bfield\s+(?:team|service)", re.I)]
MENU_WORDS = ["من نحن", "تواصل معنا", "الرئيسية", "المدونة", "اتصل بنا", "Home", "About", "Contact", "Blog", "En "]


def _window(text: str, i: int, j: int) -> str:
    a = max(text.rfind(ch, 0, i) for ch in ".!؟?\n|•")
    a = max(a + 1, i - 140)
    ends = [k for k in (text.find(ch, j) for ch in ".!؟?\n|•") if k != -1]
    b = min(min(ends) + 1 if ends else len(text), j + 140)
    return re.sub(r"\s+", " ", text[a:b]).strip()


def _evidence(texts: list[tuple[str, str]]) -> tuple[str | None, str | None, str | None, int | None]:
    """(قوة الدليل strong/weak، الجملة، رابطها، عدد الأسطول المنشور) — جملة حقيقية فقط، لا قوائم التنقل."""
    best = (None, None, None)
    fleet = None
    for url, text in texts:
        if not text:
            continue
        for strength, rxs in (("strong", STRONG_RX), ("weak", WEAK_RX)):
            if best[0] == "strong" or (best[0] == "weak" and strength == "weak"):
                continue
            for rx in rxs:
                for m in rx.finditer(text):
                    sent = _window(text, m.start(), m.end())
                    if len(sent.split()) < 6 or sum(w in sent for w in MENU_WORDS) >= 2:
                        continue
                    best = (strength, sent[:220], url)
                    break
                if best[0] == strength:
                    break
        if fleet is None:
            for m in FLEET_RX.finditer(text):
                n = int(m.group(1))
                if 2 <= n <= 20000 and len(_window(text, m.start(), m.end()).split()) >= 5:
                    fleet = n
                    break
    return best[0], best[1], best[2], fleet


def rescore(ids: list[int], crawler=None) -> int:
    """إعادة حساب الأولوية بالقاعدة الحالية (يعيد زيارة موقع المنشأة — دون تكلفة)."""
    from .discovery.crawler import Crawler
    crawler = crawler or Crawler(max_pages=5)
    n = 0
    for cid in ids:
        with session_scope() as s:
            c = s.get(Company, cid)
            web, desc_src = c.website, next((x for x in c.sources if x.source_type == "directory"), None)
        texts = []
        if web:
            try:
                site = crawler.crawl(web)
                texts = [(p.url, " ".join([p.title or "", p.description or "", p.text or ""])) for p in site.pages]
            except Exception:  # noqa: BLE001
                pass
        strength, sentence, url, fleet = _evidence(texts)
        where = "موقعها"
        if strength != "strong" and desc_src is not None:
            s2, sen2, url2, f2 = _evidence([(desc_src.url, desc_src.snippet or "")])
            if s2 == "strong" or (s2 and not strength):
                strength, sentence, url, where = s2, sen2, url2, "صفحتها في الدليل"
            fleet = fleet or f2
        with session_scope() as s:
            c = s.get(Company, cid)
            c.vehicles_estimate = fleet
            score(c, strength, sentence, url, where)
            c.fit_reason, c.fit_evidence_url = c.priority_reason or "", c.priority_url
            n += 1
    return n


def score(c: Company, strength: str | None, sentence: str | None, url: str | None, where: str) -> None:
    from .activities import SECTORS
    p = 0
    if strength == "strong":
        p += 55
    elif strength == "weak":
        p += 30
    p += SECTOR_BOOST.get(c.sector_key or "", 3)
    if c.contact_level == "website":
        p += 12
    if c.primary("phone") and c.primary("email"):
        p += 5
    if c.vehicles_estimate:
        p += 10
    c.priority = min(p, 100)
    c.priority_level = "high" if p >= 60 else "medium" if p >= 35 else "low"
    if sentence:
        c.priority_reason = f"تذكر المنشأة في {where}: «{sentence}»"
        c.priority_url = url
        c.vehicle_indicator = "confirmed" if strength == "strong" else (c.vehicle_indicator or "unconfirmed")
    else:
        sec = SECTORS.get(c.sector_key or "", {}).get("label", "نشاطها")
        c.priority_reason = f"أولوية حسب القطاع ({sec}) — لا يوجد دليل منشور على تشغيل مركبات بعد"
        c.priority_url = None
    if c.vehicles_estimate:
        c.priority_reason += f" — عدد المركبات المنشور: {c.vehicles_estimate}"


# ---------------------------------------------------------------- معالجة منشأة واحدة

def _existing(s, rec: dict, phones: list[str], email: str | None, unified: str | None) -> Company | None:
    if unified:
        c = s.scalar(select(Company).where(Company.unified_number == unified))
        if c:
            return c
    from .companies import find_existing
    from .discovery.base import Candidate, FoundContact
    cand = Candidate(name=rec["name"], source_type="directory", city=rec.get("city") or "", website=rec.get("website"),
                     external_id=rec["external_id"])
    fcs = [FoundContact("phone", p, rec["listing_url"]) for p in phones] + \
          ([FoundContact("email", email, rec["listing_url"])] if email else [])
    return find_existing(s, cand, fcs)


def _is_customer(s, rec, phones, email) -> bool:
    from .companies import is_existing_customer
    from .discovery.base import Candidate, FoundContact
    cand = Candidate(name=rec["name"], source_type="directory", city=rec.get("city") or "", website=rec.get("website"))
    fcs = [FoundContact("phone", p, rec["listing_url"]) for p in phones] + \
          ([FoundContact("email", email, rec["listing_url"])] if email else [])
    return is_existing_customer(s, cand, fcs)


def process(rec: dict, crawler, stats: dict, day: date, src: str = "bluepages", recount_since=None) -> str:
    """يعيد: ready / needs / duplicate / existing_customer / rejected.

    recount_since: عند استئناف صفحة انقطعت أثناءها، المنشأة التي أضافتها الدفعة نفسها قبل الانقطاع تُحتسب بنتيجتها
    الأصلية (لا «مكررة») لأن عدّها ضاع مع الانقطاع."""
    from .companies import _add_contact, _refresh_preferred, find_hq, is_complete, log_activity
    from .discovery.base import FoundContact
    from .geo import PLACES, resolve_city
    site_dom = domain_of(rec["website"]) if rec.get("website") else None
    if site_dom and any(x in site_dom for x in ("bluepages", "facebook.", "instagram.", "x.com", "twitter.", "linkedin.",
                                                "wa.me", "linktr.ee", "google.")):
        rec["website"], site_dom = None, None
    phones = [p for p in (normalize_phone(x) for x in rec.get("phones") or []) if p]
    email = normalize_email(rec["email"]) if rec.get("email") else None
    if email and site_dom:  # البريد بنطاق آخر غير موقع المنشأة لا يُنسب لها (إلا البريد المجاني)
        from .companies import FREE_MAIL_DOMAINS, _same_domain
        ed = email.split("@")[-1]
        if ed not in FREE_MAIL_DOMAINS and not _same_domain(ed, site_dom):
            email = None
    with session_scope() as s:
        if _is_customer(s, rec, phones, email):
            return "existing_customer"
        ex = _existing(s, rec, phones, email, rec.get("unified"))
        if ex is not None:
            if recount_since and ex.prospect_on == day and ex.first_found_at and ex.first_found_at >= recount_since:
                return "ready" if ex.contactable else "needs"
            return "duplicate"
    # التحقق من موقع المنشأة نفسها (زاحف بلا تكلفة ذكاء اصطناعي)
    site = None
    if rec.get("website"):
        try:
            site = crawler.crawl(rec["website"])
        except Exception as e:  # noqa: BLE001
            log.info("crawl failed %s: %s", rec["website"], type(e).__name__)
    site_text = [(p.url, " ".join([p.title or "", p.description or "", p.text or ""])) for p in (site.pages if site else [])]
    joined = " ".join(t for _, t in site_text)
    unified = rec.get("unified") or (m.group(1) if (m := UNIFIED_RX.search(joined)) else None)
    cr = rec.get("cr") or (m.group(1) if (m := CR_RX.search(joined)) else None)
    strength, sentence, ev_url, fleet = _evidence(site_text)
    where = "موقعها"
    if strength != "strong":
        s2, sen2, url2, fleet2 = _evidence([(rec["listing_url"], rec.get("description") or "")])
        if s2 == "strong" or (s2 and not strength):
            strength, sentence, ev_url, where = s2, sen2, url2, "صفحتها في الدليل"
        fleet = fleet or fleet2
    with session_scope() as s:
        if unified and s.scalar(select(Company.id).where(Company.unified_number == unified)):
            return "duplicate"
        if site and site.contacts:
            from .companies import find_existing
            from .discovery.base import Candidate
            if find_existing(s, Candidate(name=rec["name"], source_type="directory", city=rec.get("city") or ""),
                             site.contacts) is not None:
                return "duplicate"
        pid = resolve_city(rec.get("city") or "")
        city = PLACES[pid]["ar"] if pid else (rec.get("city") or "")
        sector = sector_of(rec.get("activity") or rec["name"])
        c = Company(name=rec["name"], name_en=rec.get("name_en"), name_norm=normalize_name(rec["name"]),
                    activity=(rec.get("activity") or "")[:200], sector_key=sector, city=city, region_id=(PLACES[pid]["region"] if pid else None),
                    place_id=pid, website=rec.get("website"), domain=site_dom, lead_source=f"directory:{src}",
                    first_found_at=now_utc(), last_seen_at=now_utc(), prospect_on=day, unified_number=unified, cr_number=cr,
                    vehicles_estimate=fleet, fit_reason="", data_status="incomplete")
        hq = find_hq(s, rec["name"], city)
        if hq is not None:
            c.parent_id = hq.id
        s.add(c)
        s.flush()
        from .models import CompanySource
        s.add(CompanySource(company_id=c.id, source_type="directory", url=rec["listing_url"][:500],
                            external_id=rec["external_id"], snippet=(rec.get("description") or "")[:500]))
        added_site = 0
        for fc in (site.contacts if site else []):
            if fc.kind == "phone" and not (normalize_phone(fc.raw) or "").startswith("+966"):
                continue
            added_site += _add_contact(s, c, FoundContact(fc.kind, fc.raw, fc.source_url, "website", fc.label))
        lvl = "provider" if src == "import" else "directory"
        if src != "google_places":  # بيانات خرائط Google لا تُخزَّن؛ التواصل من موقع المنشأة فقط
            for p in phones:
                if p.startswith("+966"):
                    _add_contact(s, c, FoundContact("phone", p, rec["listing_url"], lvl, "من الدليل" if lvl == "directory" else "من الملف"))
            if email:
                _add_contact(s, c, FoundContact("email", email, rec["listing_url"], lvl))
        s.flush()
        s.refresh(c)
        _refresh_preferred(c)
        good = [x for x in c.contacts if x.verified and (x.kind == "email" or x.value.startswith("+966"))]
        c.contactable = bool(good)
        c.contact_level = ("website" if any(x.source_type == "website" for x in good) else lvl) if good else None
        c.channels_verified_at = now_utc() if good else None
        if is_complete(c):
            c.data_status, c.completed_at, c.counted_on = "complete", now_utc(), day
        else:
            c.retry_enrich_at = now_utc() + timedelta(days=1)
        score(c, strength, sentence, ev_url, where)
        c.fit_reason = c.priority_reason or ""
        c.fit_evidence_url = c.priority_url
        log_activity(s, c.id, "discovered", f"أُضيفت إلى «العملاء المحتملون» من {SOURCE_LABEL.get(src, src)}",
                     f"صفحة الدليل: {rec['listing_url']}\nالأولوية: {LEVELS[c.priority_level]} — {c.priority_reason}"
                     + (f"\nفرع لـ «{hq.name}»" if hq else ""))
        return "ready" if c.contactable else "needs"


# ---------------------------------------------------------------- الدفعة

def _bump(stats, key, src="bluepages"):
    stats.setdefault(src, {})
    stats[src][key] = stats[src].get(key, 0) + 1


def _log(r: ProspectRun, msg: str) -> None:
    r.log = (r.log or [])[-300:] + [f"{now_utc():%H:%M:%S} {msg}"]


def request_run(s, user: User | None, target: int = 100, sources: str = "bluepages,earabicmarket") -> ProspectRun:
    s.flush()
    busy = s.scalar(select(ProspectRun).where(ProspectRun.status.in_(["pending", "running"])))
    if busy:
        return busy
    r = ProspectRun(status="pending", target=max(1, min(int(target), 5000)), stats={}, log=[], sources=sources,
                    created_by=user.id if user else None)
    s.add(r)
    s.flush()
    return r


def run(run_id: int, crawler=None, client: httpx.Client | None = None, per_page: int = 8) -> None:
    from .discovery.crawler import Crawler
    crawler = crawler or Crawler(max_pages=5)
    client = client or httpx.Client(headers={"User-Agent": UA, "Accept-Language": "ar,en"})
    day = riyadh_today()
    with session_scope() as s:
        r = s.get(ProspectRun, run_id)
        r.status, r.started_at, r.heartbeat = "running", r.started_at or now_utc(), now_utc()
        target = r.target
        src_list = r.sources or r.source
        stats = dict(r.stats or {})
        done_pages = set(stats.get("_pages") or [])
        run_started = r.started_at
        resume_key = stats.pop("_inflight", None)  # الصفحة/السجل الذي انقطعت الدفعة أثناءه
        resume_at = stats.pop("_inflight_at", None)
        try:
            from datetime import datetime as _dt
            resume_at = _dt.fromisoformat(resume_at) if resume_at else None
        except ValueError:
            resume_at = None
        _log(r, f"بدء/استئناف: الهدف {target} منشأة جديدة قابلة للتواصل — المصادر: {r.sources or r.source}")
    sources = [x for x in (src_list or "bluepages").split(",") if x in SOURCE_LABEL]
    if "google_places" in sources:  # نتائج Google للعرض المباشر فقط — لا تدخل دفعة تحفظ سجلات
        sources.remove("google_places")
        with session_scope() as s:
            _log(s.get(ProspectRun, run_id), "خرائط Google للعرض المباشر فقط ولا تُحفظ نتائجها — تُجمع السجلات من المصادر المستقلة")

    def beat(msg=None):
        with session_scope() as s:
            r = s.get(ProspectRun, run_id)
            r.heartbeat = now_utc()
            r.stats = {**stats, "_pages": sorted(done_pages)}
            if msg:
                _log(r, msg)
            return r.status

    def ready_total():
        return sum((stats.get(x) or {}).get("ready", 0) for x in SOURCE_LABEL)

    def handle(src, url, items, key=None):
        st = stats.setdefault(src, {})
        took = 0
        key = key or url
        # فقط ما أُضيف بعد بدء الصفحة المنقطعة يُحتسب بنتيجته الأصلية؛ المكرر الحقيقي من صفحة سابقة يبقى «مكررًا»
        recount = (resume_at or run_started) if resume_key and key == resume_key else None
        stats["_inflight"], stats["_inflight_at"] = key, now_utc().isoformat()
        beat()
        for rec in items:
            if (took >= per_page and src in ("bluepages", "earabicmarket")) or ready_total() >= target:
                break
            st["examined"] = st.get("examined", 0) + 1
            try:
                out = process(rec, crawler, stats, day, src, recount_since=recount)
            except Exception as e:  # noqa: BLE001
                out = "error"
                log.warning("prospect failed: %s", type(e).__name__)
            st[out] = st.get(out, 0) + 1
            if out in ("ready", "needs"):
                took += 1
        stats.pop("_inflight", None)
        stats.pop("_inflight_at", None)
        if src != "import":
            beat(f"[{src}] {unquote(url).split('.com', 1)[-1][:80]}: {len(items)} بطاقة — جاهزة {ready_total()} من {target}")

    for src in sources:
        if ready_total() >= target:
            break
        st = stats.setdefault(src, {})
        if src == "import":
            from .models import ProspectPool
            while ready_total() < target:
                batch = _pool_records(20)
                if not batch:
                    break
                if beat() == "stopped":
                    return
                for pid, rec in batch:
                    before = dict(st)
                    handle(src, rec["listing_url"], [rec], key=f"pool:{pid}")
                    res = next((k for k in ("ready", "needs", "duplicate", "existing_customer", "error")
                                if st.get(k, 0) > before.get(k, 0)), "skipped")
                    with session_scope() as s:
                        p = s.get(ProspectPool, pid)
                        p.status, p.result, p.processed_at = "done", res, now_utc()
                    if ready_total() >= target:
                        break
            continue
        if src == "google_places":
            from .config import settings
            from .discovery.base import SourceError, SourceUnavailable
            from .discovery.sources import GooglePlacesSource
            if not settings.google_places_api_key:
                beat("خرائط Google: المفتاح غير مضبوط بعد — انتقال للمصدر التالي")
                continue
            with session_scope() as s:
                budget = float(get_auto(s).get("google_budget_usd") or 10)
            gp = GooglePlacesSource()
            done_q = set(stats.get("_gq") or [])
            for phrase, city in google_pairs(day):
                key = f"{phrase}|{city}"
                if key in done_q or ready_total() >= target:
                    continue
                with session_scope() as s:
                    if google_spent_today(s) >= budget:
                        beat(f"خرائط Google: بلغت ميزانية اليوم (${budget:.2f}) — انتقال للمصدر التالي")
                        break
                if beat() == "stopped":
                    return
                state: dict = {}
                try:
                    items = []
                    for cand in gp.search(phrase, city, state):
                        items.append({"external_id": f"gplace:{cand.external_id}", "name": cand.name[:300], "name_en": None,
                                      "website": cand.website, "email": None, "phones": [], "city": city,
                                      "activity": cand.activity or phrase, "description": "", "listing_url": cand.source_url})
                        if len(items) >= 20:
                            break
                except (SourceError, SourceUnavailable) as e:
                    beat(f"خرائط Google تعذّرت: {str(e)[:160]} — انتقال للمصدر التالي")
                    break
                done_q.add(key)
                stats["_gq"] = sorted(done_q)[-3000:]
                st["pages"] = st.get("pages", 0) + 1
                handle(src, f"«{phrase}» في {city}", items)
            continue
        if src == "bluepages":
            try:
                queue = _plan(listing_pages(client), day)
            except Exception as e:  # noqa: BLE001
                beat(f"تعذّرت خريطة {SOURCE_LABEL[src]} — {type(e).__name__}: {str(e)[:120]} — انتقال للمصدر التالي")
                continue
        else:
            queue = [EAM_START]
        seen_q = set(queue)
        while queue and ready_total() < target:
            url = queue.pop(0)
            if url in done_pages:
                continue
            if beat() == "stopped":
                return
            try:
                page = _get(url, client)
                if src == "bluepages":
                    items = parse_listing(page, url)
                else:
                    items, nxt = parse_eam(page, url)
                    for n in nxt:
                        if n not in seen_q:
                            seen_q.add(n)
                            queue.append(n)
            except Exception as e:  # noqa: BLE001
                tries = stats.setdefault("_tries", {})
                tries[url] = tries.get(url, 0) + 1
                if tries[url] < PAGE_TRIES:  # انقطاع مؤقت: تُعاد الصفحة لاحقًا في الدفعة نفسها
                    queue.append(url)
                    st["page_retries"] = st.get("page_retries", 0) + 1
                    beat(f"[{src}] تعذّرت صفحة ({type(e).__name__}) — ستُعاد المحاولة {tries[url] + 1} من {PAGE_TRIES}")
                    if RETRY_WAIT:
                        time.sleep(RETRY_WAIT * tries[url])
                    continue
                st["page_errors"] = st.get("page_errors", 0) + 1
                beat(f"[{src}] تعذّرت صفحة {PAGE_TRIES} مرات: {type(e).__name__} — تُترك لدفعة اليوم التالي")
                continue
            handle(src, url, items if src == "bluepages" else items[:50])
            st["pages"] = st.get("pages", 0) + 1
            done_pages.add(url)  # بعد معالجة كل سجلاتها — توقف العامل أثناءها يعيدها عند الاستئناف
            beat()
    with session_scope() as s:
        r = s.get(ProspectRun, run_id)
        r.status, r.finished_at = "done", now_utc()
        r.stats = {**stats, "_pages": sorted(done_pages),
                   "_minutes": round((r.finished_at - r.started_at).total_seconds() / 60, 1)}
        agg = {k: sum((stats.get(x) or {}).get(k, 0) for x in SOURCE_LABEL) for k in ("ready", "needs", "duplicate", "examined")}
        _log(r, f"انتهت الدفعة: جاهزة {agg['ready']} — تحتاج استكمال {agg['needs']} — مكررة {agg['duplicate']}"
                f" — فُحص {agg['examined']} سجلًا")


def tick() -> bool:
    """العامل: يلتقط دفعة معلّقة، ويستأنف دفعة انقطع نبضها (إعادة تشغيل)."""
    from .jobs import job_lock
    try:
        schedule_daily()
    except Exception:  # noqa: BLE001
        log.exception("prospects schedule failed")
    with session_scope() as s:
        for r in s.scalars(select(ProspectRun).where(ProspectRun.status == "running")):
            if r.heartbeat and r.heartbeat < now_utc() - timedelta(minutes=10):
                r.status = "pending"
                _log(r, "استئناف بعد انقطاع العامل")
        r = s.scalar(select(ProspectRun).where(ProspectRun.status == "pending").order_by(ProspectRun.id))
        rid = r.id if r else None
    if rid is None:
        return False
    with job_lock("prospects", minutes=15) as ok:
        if not ok:
            return False
        try:
            run(rid)
        except Exception as e:  # noqa: BLE001
            log.exception("prospect run failed")
            with session_scope() as s:
                x = s.get(ProspectRun, rid)
                x.status = "failed"
                _log(x, f"فشل: {type(e).__name__}: {str(e)[:200]}")
    return True


# ---------------------------------------------------------------- القائمة والملخص

def query(s, day: date | None = None, city: str = "", sector: str = "", level: str = "", state: str = "ready",
          q: str = "", owner: str = ""):
    qq = select(Company).where(Company.prospect_on.is_not(None), Company.is_test.is_(False))
    if day:
        qq = qq.where(Company.prospect_on == day)
    if state == "ready":
        qq = qq.where(Company.contactable.is_(True))
    elif state == "needs":
        qq = qq.where(Company.contactable.is_(False))
    if city:
        qq = qq.where(Company.city == city)
    if sector:
        qq = qq.where(Company.sector_key == sector)
    if level:
        qq = qq.where(Company.priority_level == level)
    if owner == "none":
        qq = qq.where(Company.owner_id.is_(None))
    if q:
        qq = qq.where(Company.name.like(f"%{q}%") | Company.activity.like(f"%{q}%"))
    return qq.order_by(Company.priority.desc(), Company.id.desc())


def summary(s, day: date) -> dict:
    base = select(func.count(Company.id)).where(Company.prospect_on == day, Company.is_test.is_(False))
    ready = s.scalar(base.where(Company.contactable.is_(True))) or 0
    needs = s.scalar(base.where(Company.contactable.is_(False))) or 0
    a, b = riyadh_day_bounds_utc(day)
    agg: dict[str, int] = {}
    by_src: dict[str, dict] = {}
    runs = list(s.scalars(select(ProspectRun).where(ProspectRun.created_at >= a - timedelta(hours=3),
                                                    ProspectRun.created_at < b)))
    for r in runs:
        for src in SOURCE_LABEL:
            for k, v in ((r.stats or {}).get(src) or {}).items():
                agg[k] = agg.get(k, 0) + int(v or 0)
                by_src.setdefault(src, {})[k] = by_src.setdefault(src, {}).get(k, 0) + int(v or 0)
    web = s.scalar(base.where(Company.contactable.is_(True), Company.contact_level == "website")) or 0
    phone = s.scalar(select(func.count(func.distinct(Contact.company_id))).join(Company, Company.id == Contact.company_id)
                     .where(Company.prospect_on == day, Contact.kind == "phone")) or 0
    mail = s.scalar(select(func.count(func.distinct(Contact.company_id))).join(Company, Company.id == Contact.company_id)
                    .where(Company.prospect_on == day, Contact.kind == "email")) or 0
    created = ready + needs
    return {"day": day, "target": DAILY_TARGET, "ready": ready, "needs": needs, "duplicates": agg.get("duplicate", 0),
            "existing_customers": agg.get("existing_customer", 0), "examined": agg.get("examined", 0),
            "pages": agg.get("pages", 0), "website_verified": web, "with_phone": phone, "with_email": mail,
            "ready_rate": round(100 * ready / created, 1) if created else 0.0,
            "high": s.scalar(base.where(Company.priority_level == "high", Company.contactable.is_(True))) or 0,
            "cost": 0.0, "runs": runs,
            "by_source": [{"label": SOURCE_LABEL[k], **v} for k, v in by_src.items()]}
