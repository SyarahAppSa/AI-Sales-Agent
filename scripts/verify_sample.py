"""مطابقة عينة من السجلات الحقيقية بفتح روابط مصادرها من الخادم: هل الهاتف والبريد والاسم موجودة في الصفحة؟"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from mandoob.db import session_scope  # noqa: E402
from mandoob.models import Company  # noqa: E402
from mandoob.normalize import normalize_arabic, normalize_name  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
http = httpx.Client(timeout=25, follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; MandoobVerifier/1.0)", "Accept-Language": "ar,en"})


def text_of(url):
    try:
        r = http.get(url)
        return r.status_code, re.sub(r"<[^>]+>", " ", r.text)
    except Exception as e:  # noqa: BLE001
        return type(e).__name__, ""


with session_scope() as s:
    rows = list(s.scalars(select(Company).where(Company.is_test.is_(False), Company.data_status == "complete",
                                                Company.lead_source == "discovery").order_by(Company.id.desc()).limit(40)))
    seen_cities, sample = set(), []
    for c in rows:  # تنويع المدن والأنشطة
        if c.city not in seen_cities or len(sample) < N:
            sample.append(c)
            seen_cities.add(c.city)
        if len(sample) >= N:
            break
    for c in sample:
        print(f"VERIFY ==== {c.name} | {c.activity} | {c.city}", flush=True)
        for ct in c.contacts:
            if not ct.verified:
                continue
            code, txt = text_of(ct.source_url)
            t = normalize_arabic(txt).lower()
            if ct.kind == "phone":
                nat = ct.value[4:] if ct.value.startswith("+966") else ct.value
                d = re.sub(r"\D", "", t)
                ok = any(v in d for v in (nat, "0" + nat, "966" + nat))
            else:
                ok = ct.value.lower() in t
            name_ok = sum(w in t for w in normalize_name(c.name).split() if len(w) >= 3) >= 1
            print(f"VERIFY {ct.kind}={ct.value} | {ct.source_url} | http={code} | value_on_page={ok} | name_on_page={name_ok}",
                  flush=True)
