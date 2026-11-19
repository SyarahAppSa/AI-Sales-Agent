"""البيانات التدريبية: تملأ الشاشات، ولا يُرسل أو يُتصل بأي منها، وتُحذف بالكامل."""
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from helpers import otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import Call, Campaign, Company, OutboundMessage
from mandoob.outreach import queue_followups, queue_offers, send_due
from mandoob.training_demo import create, exists, remove
from mandoob.web.app import app


def test_training_fills_screens_without_sending():
    init_db()
    with session_scope() as s:
        res = create(s)
        assert res["created"] == 100 and res["offers"] >= 60 and res["replies"] >= 15 and res["meetings"] >= 5
    with session_scope() as s:
        assert create(s)["created"] == 0  # لا تكرار
        camp = s.scalar(select(Campaign).where(Campaign.name.like("حملة تدريبية%")))
        assert camp.status == "paused" and not camp.send_enabled
        before = s.scalar(select(func.count(OutboundMessage.id)))
    queue_offers(camp.id)
    queue_followups()
    assert send_due(ignore_window=True)["sent"] == 0
    with session_scope() as s:
        assert s.scalar(select(func.count(OutboundMessage.id))) == before
        assert s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.status.in_(["queued", "sending"]))) == 0
        assert s.scalar(select(func.count(Call.id)).where(Call.provider != "training")) == 0
    with TestClient(app) as c:
        otp_login(c, "admin@sayaraup.test", "admin")
        home = c.get("/").text
        assert "وضع العرض التدريبي مفعّل" in home
        assert "الشركات التي أُرسلت إليها عروض" in c.get("/offers-report").text
        rep = c.get("/offers-report").text
        assert "(تدريبي)" in rep or "تدريبي" in rep
        assert c.get("/followups").status_code == 200 and c.get("/daily-report").status_code == 200
        assert c.get("/weekly-report").status_code == 200 and c.get("/leads").status_code == 200
        c.post("/demo-view", data={})  # إطفاء
        assert "وضع العرض التدريبي مفعّل" not in c.get("/").text
    with session_scope() as s:
        assert remove(s) >= 100 and exists(s) == 0
        assert s.scalar(select(func.count(Company.id)).where(Company.lead_source == "training_demo")) == 0
