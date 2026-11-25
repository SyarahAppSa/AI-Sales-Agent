from datetime import date

from fastapi.testclient import TestClient

from mandoob import leaderboard as LB
from mandoob.contact import update_followup
from mandoob.db import init_db, session_scope
from mandoob.models import Company, User
from mandoob.web.app import app
from tests.test_ops_ready import ADMIN, _rep, otp_login


def test_levels_points_and_phrases():
    assert LB.score(60, 60) == 10 and LB.score(16, 60) == 2.7 and LB.score(120, 60) == 10
    assert LB.level(60, 60)["face"] == "😍" and LB.level(45, 60)["face"] == "🔥" and LB.level(30, 60)["face"] == "😎"
    assert LB.level(15, 60)["face"] == "🙂" and LB.level(1, 60)["face"] == "😐" and LB.level(0, 60)["face"] == "😢"
    assert len(set(LB.PHRASES)) == 31
    d = date(2026, 11, 25)
    assert LB.today_phrase(d) != LB.today_phrase(date(2026, 11, 26))
    assert LB.work_days(date(2026, 11, 22), date(2026, 11, 28)) == 5  # الأحد–السبت


def test_leaderboard_on_dashboard_and_my_card():
    init_db()
    with session_scope() as s:
        rid = _rep(s, "lb.rep1@x.test", "مندوب لوحة الصدارة")
        rep = s.get(User, rid)
        c = Company(name="عميل لوحة الصدارة", name_norm="عميل لوحة الصدارة", owner_id=rid)
        s.add(c)
        s.flush()
        update_followup(s, c, rep, "contacted", "تم")
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        html = cl.get("/").text
        assert "لوحة صدارة الفريق" in html and "مندوب لوحة الصدارة" in html and "عبارة اليوم" in html
        assert cl.get("/?lb=week").status_code == 200 and cl.get("/?lb=month").status_code == 200
    with TestClient(app) as cl:
        otp_login(cl, "lb.rep1@x.test", "staff")
        html = cl.get("/portal").text
        assert "تقدّمك اليوم" in html and "عبارة اليوم" in html and "لوحة صدارة الفريق" not in html


def test_bonus_streak_best_badges_bravo_and_celebration():
    from datetime import datetime, timedelta
    from mandoob.models import ContactLog, Notification
    from mandoob.timeutil import riyadh_today
    t = riyadh_today()
    # السلسلة تتخطى الجمعة والسبت، واليوم غير المكتمل لا يكسرها
    days = {}
    d, n = t - timedelta(days=1), 0
    while n < 2:
        if d.weekday() not in (4, 5):
            days[d] = {"_n": 60}
            n += 1
        d -= timedelta(days=1)
    assert LB.streak(days, t) == 2 and LB.best({**days, t: {"_n": 70}}, t) == 60
    assert LB.bonus({"offer_sent": 1, "subscribed": 1}) == 7 and LB.score(19, 60, 2) == 3.5
    base = datetime(2026, 1, 1, 10, 0)
    assert LB.suspicious([base + timedelta(seconds=10 * i) for i in range(10)])
    assert not LB.suspicious([base + timedelta(minutes=5 * i) for i in range(10)])
    init_db()
    with session_scope() as s:
        rid = _rep(s, "lb.rep2@x.test", "مندوب الاحتفال")
        rep = s.get(User, rid)
        for i in range(15):
            c = Company(name=f"عميل احتفال {i}", name_norm=f"عميل احتفال {i}", owner_id=rid)
            s.add(c)
            s.flush()
            update_followup(s, c, rep, "offer_sent" if i == 0 else "contacted", "")
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        html = cl.get("/").text
        assert "👏 برافو" in html and "هدف الفريق اليوم" in html and "(+2 بونص)" in html
        assert cl.post("/leaderboard/bravo", data={"uid": rid, "lb": "day"}).status_code in (200, 303)
        assert "👏 أُرسلت" in cl.get("/").text
    with session_scope() as s:
        assert s.query(Notification).filter_by(user_id=rid).filter(Notification.title.like("%برافو%")).count() == 1
    with TestClient(app) as cl:
        otp_login(cl, "lb.rep2@x.test", "staff")
        h1 = cl.get("/portal").text
        assert "وصلت 15" in h1 and "برافو من" in h1 and "رقمك القياسي" in h1 and "صائد العروض" in h1
        assert "وصلت 15" not in cl.get("/portal").text  # الاحتفال مرة واحدة
        assert cl.post("/leaderboard/bravo", data={"uid": rid}, follow_redirects=False).status_code in (403, 303, 307)
    with session_scope() as s:  # الموظف لا يرسل برافو لنفسه
        assert s.query(Notification).filter_by(user_id=rid).filter(Notification.title.like("%برافو%")).count() == 1
