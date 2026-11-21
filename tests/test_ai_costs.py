"""«تكلفة الذكاء الاصطناعي»: صرف Claude حسب الفترة، المقارنة بالفترة السابقة، الصرف بالساعة/اليوم، التوزيع حسب الغرض،
وتكلفة المنشأة المكتملة — والتكاليف غير Claude لا تُخلط بها."""
from datetime import timedelta

from fastapi.testclient import TestClient

from helpers import otp_login
from mandoob import ai_costs as A
from mandoob.db import init_db, session_scope
from mandoob.models import CostEntry
from mandoob.timeutil import now_utc, riyadh_today
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"


def test_ai_costs_report_and_page():
    init_db()
    t = riyadh_today()
    with session_scope() as s:
        base = A.report(s, "today")
        base_y = A.report(s, "yesterday")
        s.add_all([
            CostEntry(day=t, service="claude", units=1, cost_usd=0.30, note="dir:web_search:harvest", at=now_utc()),
            CostEntry(day=t, service="claude", units=1, cost_usd=0.10, note="dir:reasons", at=now_utc()),
            CostEntry(day=t, service="google_places", units=1, cost_usd=5.0, note="google_live", at=now_utc()),
            CostEntry(day=t - timedelta(days=1), service="claude", units=1, cost_usd=0.20, note="agent",
                      at=now_utc() - timedelta(days=1)),
        ])
    with session_scope() as s:
        r = A.report(s, "today")
        assert round(r["cost"] - base["cost"], 4) == 0.40 and r["calls"] - base["calls"] == 2
        assert round(r["all_cost"] - base["all_cost"], 4) == 5.40  # خرائط Google ضمن الكل لا ضمن Claude
        assert len(r["bars"]) == 24 and r["unit"] == "بالساعة"
        labels = {p["label"] for p in r["by_purpose"]}
        assert "كتابة سبب الترشيح" in labels and any(x.startswith("جمع المنشآت — ") for x in labels)
        assert A.report(s, "yesterday")["cost"] - base_y["cost"] >= 0.2
        w = A.report(s, "7d")
        assert len(w["bars"]) == 7 and w["unit"] == "باليوم"
        c = A.report(s, "x", t - timedelta(days=2), t)
        assert c["key"] == "custom" and c["days"] == 3
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        html = cl.get("/ai-costs").text
        for x in ("تكلفة الذكاء الاصطناعي", "صرف اليوم مقابل السقف اليومي", "الاستدعاءات", "متوسط الاستدعاء",
                  "تكلفة المنشأة المكتملة", "المتوقع للشهر", "الصرف بالساعة", "حسب الغرض", "كتابة سبب الترشيح",
                  "آخر 90 يوم", "تصدير Excel", 'href="/ai-costs"'):
            assert x in html, x
        assert "الصرف باليوم" in cl.get("/ai-costs?period=30d").text
        assert "فترة مخصصة" in cl.get(f"/ai-costs?period=custom&d_from={t - timedelta(days=3)}&d_to={t}").text
        x = cl.get("/ai-costs.xlsx?period=7d")
        assert x.status_code == 200 and x.content[:2] == b"PK"
