"""تحقق إنتاجي من لوحة التحكم: رقم كل بطاقة = عدد القائمة التي تفتحها (ببيانات المنصة الفعلية، قراءة فقط).

python scripts/verify_dashboard.py [--html]  ← مع --html يطبع صفحة اللوحة مضغوطة (لالتقاط صور الشاشة خارج الخادم)."""
import base64
import re
import sys
import zlib

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select

from mandoob.db import session_scope
from mandoob.models import User
from mandoob.web import app as webapp

with session_scope() as s:
    uid = s.scalar(select(User.id).where(User.role == "admin", User.active.is_(True)).order_by(User.id))


def _admin():
    with session_scope() as s:
        u = s.get(User, uid)
        s.expunge(u)
        return u


for dep in [d for d in (getattr(webapp, n, None) for n in ("current_user",)) if d]:
    webapp.app.dependency_overrides[dep] = _admin
with TestClient(webapp.app) as c:
    html = c.get("/").text
    cards = re.findall(r'<a class="kpi[^"]*" href="([^"]+)" data-card="(\w+)">.*?<div class="v">(\d+)', html, re.S)
    ok = True
    for href, key, n in cards:
        page = c.get(href.replace("&amp;", "&")).text
        m = re.search(r"(\d+) (?:سجل|متابعة)", page)
        got = int(m.group(1)) if m else -1
        ok &= got == int(n)
        print(f"CARD {key}: البطاقة {n} | القائمة {got} | {'مطابق' if got == int(n) else 'غير مطابق'} | {href}", flush=True)
    print("CARDS_OK" if ok else "CARDS_MISMATCH", flush=True)
    if "--html" in sys.argv:
        for name, url in [("dash", "/"), ("companies_new", cards[0][0].replace("&amp;", "&")), ("login", None)]:
            body = c.get(url).text if url else c.get("/admin/login").text
            blob = base64.b64encode(zlib.compress(body.encode(), 9)).decode()
            for i in range(0, len(blob), 3000):
                print(f"HTML {name} {i // 3000:03d} {blob[i:i + 3000]}", flush=True)
