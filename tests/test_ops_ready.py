"""جاهزية التشغيل: إسناد ذري لا يكتب فوق إسناد متزامن، بلاغ الموظف يصل للمالك، تنبيه الأعطال مع منع التكرار،
ومراقب العامل."""
from datetime import timedelta

from fastapi.testclient import TestClient

from helpers import otp_login
from mandoob import emergency as E
from mandoob.db import init_db, session_scope
from mandoob.models import Company, Incident, Notification, Setting, User
from mandoob.timeutil import now_utc
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"


def _rep(s, email, name):
    u = s.query(User).filter_by(email=email).first()
    if u is None:
        u = User(name=name, email=email, role="rep", password_hash="x", profile="sales_rep", scope="own")
        s.add(u)
        s.flush()
    return u.id


def test_atomic_assignment_and_issue_report_and_alerts():
    init_db()
    with session_scope() as s:
        r1, r2 = _rep(s, "ops.rep1@x.test", "مندوب أول للفحص"), _rep(s, "ops.rep2@x.test", "مندوب ثانٍ للفحص")
        c = Company(name="عميل إسناد متزامن للفحص", name_norm="عميل إسناد متزامن للفحص")
        s.add(c)
        s.flush()
        cid = c.id
    with TestClient(app) as m1, TestClient(app) as m2:
        otp_login(m1, ADMIN, "admin")
        otp_login(m2, ADMIN, "admin")
        # مديران يُسندان العميل نفسه «في الوقت نفسه»: الثاني قرأ العميل بلا مسؤول قبل أن يكتب الأول
        from mandoob.web import app as A
        orig = A.Company
        m1.post("/companies/assign", data={"owner_id": str(r1), "company_ids": [str(cid)]})
        with session_scope() as s:
            s.query(Company).filter_by(id=cid).update({"owner_id": r1})
        r = m2.post("/companies/assign", data={"owner_id": str(r2), "company_ids": [str(cid)]}, follow_redirects=True)
        assert "مسندة لموظف آخر" in r.text or "أُسندت للتو" in r.text
        with session_scope() as s:
            assert s.get(Company, cid).owner_id == r1  # لا مسؤولان، والأول لم يُكتب فوقه
        assert orig is A.Company
        # بلاغ موظف
        r = m1.post("/report-issue", data={"text": "لا تظهر قائمة عملائي", "page": "/portal"}, follow_redirects=True)
        assert "وصل بلاغك للإدارة" in r.text
    with session_scope() as s:
        assert s.query(Notification).filter(Notification.title.like("بلاغ من%لا تظهر قائمة عملائي%")).count() >= 1


def test_alert_throttle_and_watchdog():
    init_db()
    with session_scope() as s:
        row = s.get(Setting, "alert_throttle")
        if row:
            row.value = {}
        n0 = s.query(Notification).filter(Notification.title == "عطل في البريد وأكواد الدخول").count()
    E.note_incident("email", "login_code", "تجربة عطل بريد")
    E.note_incident("email", "login_code", "تجربة عطل بريد ثانية")
    with session_scope() as s:
        assert s.query(Notification).filter(Notification.title == "عطل في البريد وأكواد الدخول").count() - n0 >= 1
        per_admin = s.query(User).filter_by(role="admin", active=True).count()
        assert s.query(Notification).filter(Notification.title == "عطل في البريد وأكواد الدخول").count() - n0 == per_admin
        hb = s.get(Setting, "worker_heartbeat")
        old = (hb.value if hb else None)
        if hb is None:
            s.add(Setting(key="worker_heartbeat", value={"at": (now_utc() - timedelta(minutes=30)).isoformat()}))
        else:
            hb.value = {"at": (now_utc() - timedelta(minutes=30)).isoformat()}
    E.watchdog()
    with session_scope() as s:
        assert s.query(Incident).filter(Incident.service == "scheduler", Incident.message.like("توقف العامل%")).count() >= 1
        hb = s.get(Setting, "worker_heartbeat")
        hb.value = old or {"at": now_utc().isoformat()}
