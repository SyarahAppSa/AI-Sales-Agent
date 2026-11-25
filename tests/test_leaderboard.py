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
