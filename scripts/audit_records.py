"""تدقيق سجلات محددة بفتح روابط مصادرها من الخادم.

لكل سجل: هل الهاتف والبريد موجودان فعلًا في صفحة المصدر؟ هل اسم المنشأة في الصفحة؟ هل نطاق صفحة الهاتف
ونطاق البريد يطابقان موقع المنشأة نفسها؟ هل في الصفحة ما يثبت نشاطًا داخل السعودية (مدينة/عنوان/سجل تجاري/920)
— لا يكفي وجود رقم سعودي وحده.
التشغيل: python scripts/audit_records.py 713 714 717 727 739
"""
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402

from mandoob.db import session_scope  # noqa: E402
from mandoob.models import Company  # noqa: E402
from mandoob.normalize import normalize_arabic, normalize_name  # noqa: E402

http = httpx.Client(timeout=25, follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; MandoobVerifier/1.0)", "Accept-Language": "ar,en"})
FREE_MAIL = {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com", "live.com"}
SAUDI_MARKERS = ["الرياض", "جده", "جدة", "الدمام", "المملكه العربيه السعوديه", "السعوديه", "riyadh", "saudi", "ksa",
                 "سجل تجاري", "س.ت", "الرقم الضريبي", "vat", "920", "حي "]
_cache = {}


def page(url):
    if url not in _cache:
        try:
            r = http.get(url)
            txt = re.sub(r"<[^>]+>", " ", r.text)
            _cache[url] = (r.status_code, normalize_arabic(txt).lower(), r.text.lower())
        except Exception as e:  # noqa: BLE001
            _cache[url] = (type(e).__name__, "", "")
    return _cache[url]


def dom(u):
    h = urlparse(u if "//" in u else "//" + u).hostname or ""
    return h.lower().removeprefix("www.")


def phone_on(v, t, raw):
    nat = v[4:] if v.startswith("+966") else v.lstrip("+")
    d = re.sub(r"\D", "", t + " " + raw)
    return any(x in d for x in (nat, "0" + nat, "966" + nat))


ids = [int(x) for x in sys.argv[1:]]
with session_scope() as s:
    for cid in ids:
        c = s.get(Company, cid)
        site = dom(c.website or "")
        print(f"AUDIT ==== #{c.id} {c.name} | {c.activity} | {c.city} | site={site or '-'}", flush=True)
        tokens = [w for w in normalize_name(c.name).split() if len(w) >= 3]
        problems = []
        for ct in c.contacts:
            if not ct.verified:
                continue
            code, t, raw = page(ct.source_url)
            on = phone_on(ct.value, t, raw) if ct.kind == "phone" else ct.value.lower() in (t + raw)
            name_hits = sum(w in t for w in tokens)
            pdom = dom(ct.source_url)
            same_site = bool(site) and (pdom == site or pdom.endswith("." + site) or site.endswith("." + pdom))
            markers = [m for m in SAUDI_MARKERS if m in t]
            line = (f"AUDIT {ct.kind}={ct.value} | src={ct.source_url} | http={code} | on_page={on} | "
                    f"name_tokens_on_page={name_hits}/{len(tokens)} | src_domain_is_company_site={same_site} | "
                    f"saudi_markers={markers[:6]}")
            if ct.kind == "email":
                ed = ct.value.split("@")[-1].lower()
                rel = ("free_mail" if ed in FREE_MAIL else
                       "matches_site" if site and (ed == site or ed.endswith("." + site) or site.endswith(ed)) else "OTHER_DOMAIN")
                line += f" | email_domain={ed}:{rel}"
                if rel == "OTHER_DOMAIN":
                    problems.append(f"نطاق البريد {ed} لا يطابق موقع المنشأة {site}")
            if not on:
                problems.append(f"{ct.kind} غير موجود في صفحة المصدر")
            if not same_site:
                problems.append(f"مصدر {ct.kind} من نطاق آخر ({pdom})")
            if not markers:
                problems.append(f"لا دليل سعودي في صفحة مصدر {ct.kind}")
            print(line, flush=True)
        print("AUDIT verdict #%d: %s" % (c.id, "سليم" if not problems else "؛ ".join(problems)), flush=True)
