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


def test_team_activity_report():
    from mandoob.contact import update_followup
    from mandoob.models import ContactLog
    init_db()
    with session_scope() as s:
        rid = _rep(s, "ops.rep3@x.test", "مندوب تقرير الفريق")
        rep = s.get(User, rid)
        c = Company(name="عميل تقرير الفريق للفحص", name_norm="عميل تقرير الفريق للفحص", owner_id=rid)
        t = Company(name="عميل تجريبي مستبعد للفحص", name_norm="عميل تجريبي مستبعد للفحص", owner_id=rid, is_test=True)
        s.add_all([c, t])
        s.flush()
        update_followup(s, c, rep, "no_answer", "لم يرد — للفحص", now_utc() + timedelta(days=1), None, False)
        update_followup(s, t, rep, "contacted", "تجريبي", None, None, False)
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        html = cl.get("/team-activity").text
        assert "تقرير الفريق وجودة البيانات" in html and "مندوب تقرير الفريق" in html and "عميل تقرير الفريق للفحص" in html
        assert "لم يرد — للفحص" in html and "عميل تجريبي مستبعد للفحص" not in html
        assert 'href="/team-activity"' in html
    with session_scope() as s:
        for t in s.query(Company).filter_by(name="عميل تجريبي مستبعد للفحص"):
            s.query(ContactLog).filter_by(company_id=t.id).delete()
            from mandoob.models import Activity
            s.query(Activity).filter_by(company_id=t.id).delete()
            s.delete(t)


def test_ops_report_and_logout_audit():
    from mandoob import ops_report
    from mandoob.models import AuditLog
    init_db()
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        cl.get("/logout")
    with session_scope() as s:
        assert s.query(AuditLog).filter_by(action="logout").count() >= 1
    out = "\n".join(ops_report.build(5))
    for x in ("[الدخول والخروج]", "دخل:", "خرج:", "[التواصل مع العملاء]", "[العملاء]", "[التكلفة اليوم]", "[الأعطال]",
              "[بلاغات الموظفين]", "[العامل]"):
        assert x in out, x


def test_team_report_new_statuses_columns_quality_and_email(monkeypatch):
    from mandoob import team_report as TR
    from mandoob.contact import update_followup
    from mandoob.models import Contact
    from mandoob.reports import data_quality, team_activity
    from mandoob.timeutil import riyadh_today
    init_db()
    with session_scope() as s:
        rid = _rep(s, "ops.rep5@x.test", "مندوب التقرير اليومي")
        rep = s.get(User, rid)
        cs = []
        for i in range(4):
            c = Company(name=f"عميل تقرير يومي {i}", name_norm=f"عميل تقرير يومي {i}", owner_id=rid, assigned_at=now_utc(),
                        data_status="complete")
            s.add(c)
            cs.append(c)
        s.flush()
        s.add(Contact(company_id=cs[0].id, kind="phone", value="+966551230001", source_url="https://x.example"))
        update_followup(s, cs[0], rep, "subscribed", "اشترك")
        update_followup(s, cs[1], rep, "new_request", "طلب خدمة جديدة")
        update_followup(s, cs[2], rep, "call_later", "", now_utc() + timedelta(days=1), None, False)
        assert cs[0].stage == "won" and cs[0].won_at is not None
        admin = s.query(User).filter_by(role="admin").first() or User(name="x", role="admin", scope="all")
        r = team_activity(s, admin, riyadh_today(), riyadh_today())
        x = next(v for v in r["reps"] if v["name"] == "مندوب التقرير اليومي")
        assert x["assigned"] == 4 and x["subscribed"] == 1 and x["new_request"] == 1 and x["call_later"] == 1
        assert x["not_contacted"] == 1 and x["total"] == 3
        assert [k for k, _ in r["COLS"]][:8] == ["assigned", "no_answer", "call_later", "offer_sent", "not_interested",
                                                 "new_request", "not_contacted", "subscribed"]
        q = data_quality(s, admin)
        assert q["total"] >= 4 and 0 <= q["phone_pct"] <= 100
        subject, text, html = TR.build_html(s, riyadh_today(), "https://mandoob.live")
        assert "مندوب التقرير اليومي" in html and "مشترك لدى سيارة اب" in html and "جودة بيانات" in html
    sent = []
    from mandoob.mail import sandbox
    orig_send = sandbox.SandboxProvider.send

    def fake_send(self, m):
        if m.to.startswith("mgr"):
            sent.append(m.to)
            return type("R", (), {"accepted": True})()
        return orig_send(self, m)
    monkeypatch.setattr(sandbox.SandboxProvider, "send", fake_send)
    monkeypatch.setattr(TR, "recipients", lambda s: [type("U", (), {"email": "mgr1@co.example"})(), type("U", (), {"email": "mgr2@co.example"})()])
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        html = cl.get("/team-activity").text
        for t in ("تقرير الفريق وجودة البيانات", "عدد الإسناد", "لم يتم التواصل", "مشترك لدى سيارة اب", "طلب جديد",
                  "إجمالي التواصل", "جودة بيانات العملاء المسندين", "أرسل تقرير اليوم الآن"):
            assert t in html, t
        sent.clear()
        r = cl.post("/team-activity/email", data={}, follow_redirects=True)
        assert sent == ["mgr1@co.example", "mgr2@co.example"] and "أُرسل التقرير" in r.text
    # الإرسال اليومي مرة واحدة
    with session_scope() as s:
        TR.put_cfg(s, enabled=True, time="00:00", last_sent=None)
    sent.clear()
    assert TR.tick() is True and len(sent) == 2
    assert TR.tick() is False and len(sent) == 2


def test_team_report_visible_only_to_owner_and_named_viewers():
    from mandoob import team_report as TR
    init_db()
    with session_scope() as s:
        m = s.query(User).filter_by(email="ops.mgr@x.example").first()
        if m is None:
            m = User(name="مدير غير مخوّل للفحص", email="ops.mgr@x.example", role="manager", password_hash="x",
                     profile="team_lead", scope="team")
            s.add(m)
        TR.put_cfg(s, viewers=[])
    with TestClient(app) as cl:
        otp_login(cl, "ops.mgr@x.example", "admin")
        assert cl.get("/team-activity").status_code == 403
        assert 'href="/team-activity"' not in cl.get("/").text
    with session_scope() as s:
        TR.put_cfg(s, viewers=["ops.mgr@x.example"])
        assert "ops.mgr@x.example" in [u.email for u in TR.recipients(s)]
    with TestClient(app) as cl:
        otp_login(cl, "ops.mgr@x.example", "admin")
        assert cl.get("/team-activity").status_code == 200
    with session_scope() as s:
        TR.put_cfg(s, viewers=[])
        assert "ops.mgr@x.example" not in [u.email for u in TR.recipients(s)]
