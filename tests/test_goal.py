"""الهدف اليومي الموحّد (٢٠٠): إعداد واحد تقرؤه الجدولة والحملة اليومية ولوحة التحكم والتقرير اليومي و«اكتشاف
عملاء جدد» و«العملاء المحتملون»؛ يُحتسب المكتمل مرة واحدة بجوال سعودي وبريد لكلٍّ رابط إثبات؛ ويتوقف الجمع عند الهدف
أو حد التكلفة؛ وزر «بحث في خرائط Google» بلا مفتاح."""
from fastapi.testclient import TestClient

from helpers import otp_login
from mandoob import goal
from mandoob.costs import record_cost
from mandoob.db import init_db, session_scope
from mandoob.models import Company, Contact, Setting
from mandoob.timeutil import riyadh_today
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"


def _co(s, name, *, phone="+966551110100", phone_url="https://site.example/contact", email="info@goal-test.sa",
        email_url="https://site.example/contact", test=False, existing=False, status="complete", day=None):
    c = Company(name=name, name_norm=name, data_status=status, counted_on=day or riyadh_today(), is_test=test,
                is_existing_customer=existing)
    s.add(c)
    s.flush()
    if phone:
        s.add(Contact(company_id=c.id, kind="phone", value=phone, source_url=phone_url or ""))
    if email:
        s.add(Contact(company_id=c.id, kind="email", value=email, source_url=email_url or ""))
    s.flush()
    return c


def test_goal_unified_counting_display_and_halt():
    init_db()
    with session_scope() as s:
        base = goal.achieved(s)
        goal.set_goal(s, 200, cost_cap_usd=1000)
    with session_scope() as s:
        from mandoob import directory, prospecting, prospects
        assert goal.target(s) == 200
        assert directory.get_auto(s)["target_complete"] == 200 and prospects.get_auto(s)["target"] == 200
        assert prospecting.get_config(s)["daily_target"] == 200
        _co(s, "منشأة هدف مكتملة ١", phone="+966551110101", email="a1@goal-test.sa")
        _co(s, "منشأة هدف مكتملة ٢", phone="+966551110102", email="a2@goal-test.sa")
        _co(s, "بلا رابط للبريد", phone="+966551110103", email="a3@goal-test.sa", email_url="")
        _co(s, "جوال غير سعودي", phone="+971501110104", email="a4@goal-test.sa")
        _co(s, "عميل حالي", phone="+966551110105", email="a5@goal-test.sa", existing=True)
        _co(s, "جوال بلا رابط", phone="+966551110106", phone_url="", email="a6@goal-test.sa")
        _co(s, "قيد الاستكمال", phone="+966551110107", email=None, status="incomplete")
    with session_scope() as s:
        assert goal.achieved(s) == base + 2  # فقط المكتملتان بالتعريف
        why = goal.shortfall_reason(s)
        assert why
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        dash = cl.get("/").text
        assert f"{base + 2} <small>من 200</small>" in dash and "سبب النقص" in dash
        rep = cl.get("/daily-report").text
        assert f"{base + 2} <small class=\"muted\">من 200</small>" in rep and "سبب النقص" in rep
        disc = cl.get("/discover").text
        assert "بحث في خرائط Google ↗" in disc and "google.com/maps/search/?api=1" in disc and "بانتظار المفتاح" in disc
        assert 'value="200"' in disc  # هدف المكتملة يوميًا في نموذج الجدولة
        # حفظ الهدف من «البحث عن العملاء» يطبّقه على الكل
        pg = cl.get("/prospecting").text
        assert 'name="daily_target" value="200"' in pg
        cl.post("/discover/auto", data={"enabled": "", "time": "05:30", "target": "150", "budget": "30"})
    with session_scope() as s:
        from mandoob import directory, prospects
        assert goal.target(s) == 150 and goal.get(s)["cost_cap_usd"] == 30
        assert directory.get_auto(s)["target_complete"] == 150 and prospects.get_auto(s)["target"] == 150
        goal.set_goal(s, base + 2, cost_cap_usd=1000)  # الهدف = ما اكتمل → يتوقف الجمع
    assert goal.halt_reason() == f"بلغ هدف اليوم: {base + 2} من {base + 2}"
    from mandoob.prospecting import halt_reason
    assert halt_reason() is not None  # «اكتشاف عملاء جدد» يسألها قبل كل خطوة
    with session_scope() as s:
        goal.set_goal(s, 200, cost_cap_usd=0.5)
    record_cost("claude", 1, 0.6, note="goal-test")
    assert "حد التكلفة" in goal.halt_reason()
    with session_scope() as s:
        goal.set_goal(s, 200, cost_cap_usd=1000)
        assert s.get(Setting, goal.KEY).value["target"] == 200
