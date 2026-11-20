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
SOURCE_LABEL = {"bluepages": "دليل الصفحات الزرقاء (bluepages.com.sa)"}
LEVELS = {"high": "عالية", "medium": "متوسطة", "low": "منخفضة"}
CONTACT_LEVELS = {"website": "موثّقة من موقع المنشأة", "directory": "منشورة في الدليل باسم المنشأة"}

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


# ---------------------------------------------------------------- التصنيف والأولوية

def sector_of(activity: str) -> str:
    a = activity or ""
    for key, words in SECTOR_WORDS:
        if any(w in a for w in words):
            return key
    return "community"


def _evidence(texts: list[tuple[str, str]]) -> tuple[str | None, str | None, str | None, int | None]:
    """(قوة الدليل strong/weak، الجملة، رابطها، عدد الأسطول المنشور) من نصوص (الرابط، النص)."""
    best = (None, None, None)
    fleet = None
    for url, text in texts:
        if not text:
            continue
        for sent in re.split(r"(?<=[.!؟?\n])\s+|\s{2,}|[|•]", text):
            s = sent.strip()
            if not (12 <= len(s) <= 400):
                continue
            low = s.lower()
            if any(w.lower() in low for w in STRONG):
                if best[0] != "strong":
                    best = ("strong", s[:220], url)
            elif best[0] is None and any(w.lower() in low for w in WEAK):
                best = ("weak", s[:220], url)
            if fleet is None and (m := FLEET_RX.search(s)):
                n = int(m.group(1))
                if 2 <= n <= 20000:
                    fleet = n
    return best[0], best[1], best[2], fleet


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


def process(rec: dict, crawler, stats: dict, day: date) -> str:
    """يعيد: ready / needs / duplicate / existing_customer / rejected."""
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
        if _existing(s, rec, phones, email, None) is not None:
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
    unified = (m.group(1) if (m := UNIFIED_RX.search(joined)) else None)
    cr = (m.group(1) if (m := CR_RX.search(joined)) else None)
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
                    place_id=pid, website=rec.get("website"), domain=site_dom, lead_source="directory:bluepages",
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
        for p in phones:
            if p.startswith("+966"):
                _add_contact(s, c, FoundContact("phone", p, rec["listing_url"], "directory", "من الدليل"))
        if email:
            _add_contact(s, c, FoundContact("email", email, rec["listing_url"], "directory"))
        s.flush()
        s.refresh(c)
        _refresh_preferred(c)
        good = [x for x in c.contacts if x.verified and (x.kind == "email" or x.value.startswith("+966"))]
        c.contactable = bool(good)
        c.contact_level = ("website" if any(x.source_type == "website" for x in good) else "directory") if good else None
        c.channels_verified_at = now_utc() if good else None
        if is_complete(c):
            c.data_status, c.completed_at, c.counted_on = "complete", now_utc(), day
        else:
            c.retry_enrich_at = now_utc() + timedelta(days=1)
        score(c, strength, sentence, ev_url, where)
        c.fit_reason = c.priority_reason or ""
        c.fit_evidence_url = c.priority_url
        log_activity(s, c.id, "discovered", "أُضيفت إلى «العملاء المحتملون» من دليل الصفحات الزرقاء",
                     f"صفحة الدليل: {rec['listing_url']}\nالأولوية: {LEVELS[c.priority_level]} — {c.priority_reason}"
                     + (f"\nفرع لـ «{hq.name}»" if hq else ""))
        return "ready" if c.contactable else "needs"


# ---------------------------------------------------------------- الدفعة

def _bump(stats, key, src="bluepages"):
    stats.setdefault(src, {})
    stats[src][key] = stats[src].get(key, 0) + 1


def _log(r: ProspectRun, msg: str) -> None:
    r.log = (r.log or [])[-300:] + [f"{now_utc():%H:%M:%S} {msg}"]


def request_run(s, user: User | None, target: int = 100) -> ProspectRun:
    s.flush()
    busy = s.scalar(select(ProspectRun).where(ProspectRun.status.in_(["pending", "running"])))
    if busy:
        return busy
    r = ProspectRun(status="pending", target=max(1, min(int(target), 5000)), stats={}, log=[],
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
        stats = dict(r.stats or {})
        done_pages = set(stats.get("_pages") or [])
        _log(r, f"بدء/استئناف: الهدف {target} منشأة جديدة قابلة للتواصل — المصدر: {SOURCE_LABEL['bluepages']}")
    st = stats.setdefault("bluepages", {})
    try:
        pages = _plan(listing_pages(client), day)
    except Exception as e:  # noqa: BLE001
        with session_scope() as s:
            r = s.get(ProspectRun, run_id)
            r.status, r.finished_at = "failed", now_utc()
            _log(r, f"فشل: تعذّر قراءة خريطة الدليل — {type(e).__name__}: {str(e)[:160]}")
        return

    def beat(msg=None):
        with session_scope() as s:
            r = s.get(ProspectRun, run_id)
            r.heartbeat = now_utc()
            r.stats = {**stats, "_pages": sorted(done_pages)}
            if msg:
                _log(r, msg)
            return r.status

    for url in pages:
        if st.get("ready", 0) >= target:
            break
        if url in done_pages:
            continue
        if beat() == "stopped":
            return
        try:
            items = parse_listing(_get(url, client), url)
        except Exception as e:  # noqa: BLE001
            _bump(stats, "page_errors")
            beat(f"تعذّرت صفحة {unquote(url)[len(BP):][:80]}: {type(e).__name__}")
            done_pages.add(url)
            continue
        done_pages.add(url)
        _bump(stats, "pages")
        took = 0
        for rec in items:
            if took >= per_page or st.get("ready", 0) >= target:
                break
            _bump(stats, "examined")
            try:
                out = process(rec, crawler, stats, day)
            except Exception as e:  # noqa: BLE001
                out = "error"
                log.warning("prospect failed: %s", type(e).__name__)
            _bump(stats, out)
            if out in ("ready", "needs"):
                took += 1
        beat(f"{unquote(url)[len(BP):][:80]}: {len(items)} بطاقة — جاهزة {st.get('ready', 0)} من {target}")
    with session_scope() as s:
        r = s.get(ProspectRun, run_id)
        r.status, r.finished_at = "done", now_utc()
        r.stats = {**stats, "_pages": sorted(done_pages),
                   "_minutes": round((r.finished_at - r.started_at).total_seconds() / 60, 1)}
        _log(r, f"انتهت الدفعة: جاهزة {st.get('ready', 0)} — تحتاج استكمال {st.get('needs', 0)} — مكررة {st.get('duplicate', 0)}"
                f" — فُحص {st.get('examined', 0)} سجلًا")


def tick() -> bool:
    """العامل: يلتقط دفعة معلّقة، ويستأنف دفعة انقطع نبضها (إعادة تشغيل)."""
    from .jobs import job_lock
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
    runs = list(s.scalars(select(ProspectRun).where(ProspectRun.created_at >= a - timedelta(hours=3),
                                                    ProspectRun.created_at < b)))
    for r in runs:
        for k, v in ((r.stats or {}).get("bluepages") or {}).items():
            agg[k] = agg.get(k, 0) + int(v or 0)
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
            "cost": 0.0, "runs": runs}
