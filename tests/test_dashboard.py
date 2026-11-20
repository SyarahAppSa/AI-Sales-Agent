"""لوحة التحكم: أربع بطاقات، رقم كل بطاقة = عدد القائمة التي تفتحها، الاسم الجديد، وشريط «حالة البحث» بالعربية."""
import re
from datetime import timedelta

from fastapi.testclient import TestClient

from helpers import otp_login
from mandoob.db import init_db, session_scope
from mandoob.health import classify
from mandoob.models import Company, Task, User
from mandoob.timeutil import now_utc, riyadh_today
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"


def setup_module():
    init_db()
    with session_scope() as s:
        rep = s.query(User).filter(User.email == "dash.rep@syarahapp.test").first()
        if rep is None:
            rep = User(name="مندوب اللوحة", email="dash.rep@syarahapp.test", role="rep", active=True, password_hash="x")
            s.add(rep)
            s.flush()
        for i in range(3):
            s.add(Company(name=f"لوحة جديد {i}", name_norm=f"dash-new-{i}", activity="نقل", city="الرياض",
                          data_status="complete", counted_on=riyadh_today(), first_found_at=now_utc(),
                          lead_source="discovery"))
        for i in range(2):
            s.add(Company(name=f"لوحة ناقص {i}", name_norm=f"dash-inc-{i}", activity="نقل", city="جدة",
                          data_status="incomplete", first_found_at=now_utc(), lead_source="discovery"))
        c = Company(name="لوحة مسند", name_norm="dash-owned", activity="نقل", city="الدمام", data_status="complete",
                    owner_id=rep.id, lead_source="discovery")
        s.add(c)
        s.flush()
        s.add(Task(company_id=c.id, owner_id=rep.id, title="اتصال متابعة — لوحة", due_at=now_utc() - timedelta(hours=1),
                   status="open"))


def _total(html):
    m = re.search(r"(\d+) (?:سجل|متابعة)", html)
    return int(m.group(1)) if m else None


def test_four_cards_match_their_lists_and_branding():
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        html = c.get("/").text
        assert "<title>لوحة التحكم — المندوب الذكي لسيارة اب</title>" in html
        assert ">المندوب الذكي<" in html and ">لسيارة اب<" in html and "سيـــارة" not in html
        assert "البحث عن عملاء" in html and "حالة البحث" in html and "أداء المبيعات" in html
        assert "أحدث العملاء" in html and "متابعات الفريق" in html
        for g in ("العملاء", "المتابعة والمبيعات", "التقارير", "الإدارة والإعدادات"):
            assert f'<div class="grp">{g}</div>' in html
        cards = re.findall(r'<a class="kpi[^"]*" href="([^"]+)" data-card="(\w+)">.*?<div class="v">(\d+)', html, re.S)
        assert [k for _, k, _ in cards] == ["new", "incomplete", "ready", "followups"]
        assert "من 400" in html
        for href, key, n in cards:
            page = c.get(href.replace("&amp;", "&")).text
            assert _total(page) == int(n), (key, _total(page), n)
            if key == "followups":
                assert int(n) >= 1 and "لوحة مسند" in page
        nums = {k: int(n) for _, k, n in cards}
        assert nums["new"] >= 3 and nums["incomplete"] >= 2
        for p in ("today", "7d", "30d", "month", "all"):
            assert c.get(f"/?period={p}").status_code == 200


def test_search_status_messages_are_arabic_with_cause_and_action(monkeypatch):
    from mandoob import config
    monkeypatch.setattr(type(config.settings), "ai_enabled", property(lambda self: True))
    c = classify("Error code: 400 - {'type': 'invalid_request_error', 'message': 'Your credit balance is too low'}")
    assert c["cause"] == "رصيد خدمة البحث غير كافٍ" and c["fatal"]
    assert classify("ReadTimeout: timed out")["fatal"] is False
    assert classify("HTTP 403 Cloudflare")["cause"] == "الموقع يمنع الوصول الآلي"
    from mandoob.models import DirectoryRun
    with session_scope() as s:
        s.add(DirectoryRun(status="done", sources=["web_search", "social"], params={}, budget_usd=1, log=[],
                           stats={"_errors": {"social": {"n": 2, "last": "ReadTimeout: timed out"}}},
                           created_at=now_utc(), started_at=now_utc(), finished_at=now_utc()))
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        html = cl.get("/").text
        assert 'class="health warn"' in html and "تعذّر" in html and "ReadTimeout" not in html  # التقني لا يظهر
        runs = cl.get("/runs").text
        assert "ReadTimeout" in runs and "انتهت مهلة الاتصال بالمصدر" in runs                # التفاصيل في السجل
    with session_scope() as s:
        s.add(DirectoryRun(status="stopped", sources=["web_search"], params={}, budget_usd=1, created_at=now_utc(),
                           log=["10:00:00 تعذر «بحث الويب»: BadRequestError: credit balance is too low"],
                           stats={"_errors": {"web_search": {"n": 1, "last": "BadRequestError: credit balance is too low"}}}))
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        html = cl.get("/").text
        assert 'class="health err"' in html and "توقف البحث" in html and "رصيد خدمة البحث غير كافٍ" in html
