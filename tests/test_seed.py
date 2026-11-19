"""٥٠٠ عميل تجريبي: فريدون، يُستكمل الناقص فقط، ولا رسائل ولا مكالمات ولا متابعات ولا احتساب في الهدف."""
from sqlalchemy import func, select

from mandoob.db import init_db, session_scope
from mandoob.models import Campaign, Category, Company, Contact, Offer, OutboundMessage
from mandoob.seed_test import seed, verify


def test_seed_500_unique_idempotent_and_never_contacted():
    init_db()
    from mandoob.auth import hash_password
    from mandoob.models import User
    with session_scope() as s:
        if not s.scalar(select(User).where(User.role == "rep", User.active.is_(True))):
            s.add(User(name="موظف للاختبار", email="seed.rep@syarahapp.test", password_hash=hash_password("x"), role="rep"))
    with session_scope() as s:
        assert seed(s)["added"] == 500
    with session_scope() as s:
        v = verify(s)
        assert v["test_customers"] == 500 and v["unique_emails"] == 500
        assert v["by_group"] == {"شركات لديها مركبات تشغيلية أو فرق ميدانية": 150,
                                 "مؤسسات صغيرة تستخدم سيارة واحدة أو أكثر": 100,
                                 "مصانع تستخدم مركبات للنقل أو التوزيع": 100,
                                 "منشآت نقليات وشحن وتوصيل": 100,
                                 "أفراد وأصحاب أنشطة يستخدمون مركباتهم": 50}
        assert v["not_contacted"] == 500 and v["single_vehicle"] > 50 and v["cities"] >= 15
        assert v["assigned"] > 0 and v["unassigned"] > 0
    # إعادة التشغيل لا تضيف شيئًا؛ وبعد حذف ١٠ تُستكمل العشرة فقط
    with session_scope() as s:
        assert seed(s)["added"] == 0
    with session_scope() as s:
        from mandoob.models import Activity
        victims = list(s.scalars(select(Company).where(Company.is_test.is_(True)).order_by(Company.id).limit(10)))
        s.query(Activity).filter(Activity.company_id.in_([c.id for c in victims])).delete(synchronize_session=False)
        for c in victims:
            s.delete(c)
    with session_scope() as s:
        assert seed(s)["added"] == 10
    with session_scope() as s:
        assert verify(s)["test_customers"] == 500
        assert s.scalar(select(func.count(Contact.id)).where(Contact.value.like("client%@example.test"))) == 500

    # حتى لو وُضعت السجلات التجريبية عمدًا في حملة نشطة بإرسال مفعّل: لا يُجهز لها شيء ولا يُرسل
    from mandoob.outreach import queue_followups, queue_offers, send_due
    from mandoob.voice import check_call_eligibility
    with session_scope() as s:
        offer = Offer(key="seed-guard", service_name="اختبار الحماية", subject_template="x", approved_body="x", approved=True)
        s.add(offer)
        s.flush()
        camp = Campaign(name="حملة اختبار الحماية", status="active", send_enabled=True)
        s.add(camp)
        s.flush()
        cat = Category(campaign_id=camp.id, sector_key="vehicle_users", label="x", cities=[], quota=10, offer_id=offer.id)
        s.add(cat)
        s.flush()
        for c in s.scalars(select(Company).where(Company.is_test.is_(True)).limit(50)):
            c.campaign_id, c.category_id = camp.id, cat.id
        cid = camp.id
    assert queue_offers(cid)["queued"] == 0
    queue_followups()
    send_due(ignore_window=True)
    with session_scope() as s:
        v = verify(s)
        assert v["messages_sent"] == 0 and v["messages_queued"] == 0 and v["messages_any"] == 0 and v["calls"] == 0
        assert v["counted_in_daily_target"] == 0
        ok, why = check_call_eligibility(s, s.scalar(select(Company).where(Company.is_test.is_(True))))
        assert not ok and "تجريبية" in why
        for c in s.scalars(select(Company).where(Company.campaign_id == cid)):
            c.campaign_id, c.category_id = None, None
        s.query(OutboundMessage).filter(OutboundMessage.company_id.in_(
            select(Company.id).where(Company.is_test.is_(True)))).delete(synchronize_session=False)


def test_test_filter_and_excluded_from_today():
    from fastapi.testclient import TestClient
    from helpers import otp_login
    from mandoob.web.app import app
    with TestClient(app) as c:
        otp_login(c, "admin@sayaraup.test", "admin")
        r = c.get("/companies?kind=test")
        assert "بيانات تجريبية" in r.text and "500 سجل" in r.text
        assert "client001@example.test" in c.get("/companies?kind=test&q=client001").text
        assert "بيانات تجريبية" not in c.get("/today").text  # خارج «عملاء اليوم» وهدف الـ٤٠٠
        assert c.get("/companies?kind=test&owner=none").status_code == 200
