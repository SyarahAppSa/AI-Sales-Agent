"""اختبار التسليم: بوابة الإدارة وبوابة الموظفين، الدخول برمز البريد، الصلاحيات على الخادم، والإيقاف الفوري."""
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from helpers import ensure_mail, last_code_for, otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import AuditLog, Company, Contact, LoginCode, Task, User
from mandoob.timeutil import now_utc
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
E1, E2 = "khaled.view@syarahapp.test", "mona.follow@syarahapp.test"
S = {}


def setup_module():
    init_db()
    ensure_mail()
    with session_scope() as s:
        a = Company(name="شركة الأفق للتوزيع", name_norm="افق توزيع", activity="توزيع مبرد", city="الرياض",
                    data_status="complete", stage="contacted")
        b = Company(name="مؤسسة الريم للصيانة", name_norm="ريم صيانه", activity="صيانة متنقلة", city="جدة",
                    data_status="complete", stage="contacted")
        s.add_all([a, b])
        s.flush()
        a.contacts.append(Contact(kind="phone", value="+966114440001", source_url="https://ofuq.example/c"))
        a.contacts.append(Contact(kind="email", value="sales@ofuq.example", source_url="https://ofuq.example/c"))
        b.contacts.append(Contact(kind="phone", value="+966126667788", source_url="https://reem.example/c"))
        b.contacts.append(Contact(kind="email", value="ops@reem.example", source_url="https://reem.example/c"))
        s.add(Task(company_id=b.id, title="الاتصال بالعميل — مؤسسة الريم", details="الهاتف: +966 12 666 7788",
                   due_at=now_utc() + timedelta(days=1)))
        S.update(a=a.id, b=b.id)


def test_1_admin_adds_two_employees_and_assigns_one_customer_each():
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        # الأول: اطلاع فقط
        r = c.post("/staff/add", data={"name": "خالد", "email": E1, "team": "فريق الرياض", "profile": "view_only"},
                   follow_redirects=False)
        assert r.status_code == 303
        # الثانية: تحديث المتابعة فقط، والهاتف مخفي
        r = c.post("/staff/add", data={"name": "منى", "email": E2, "team": "فريق جدة", "profile": "custom", "customize": "on",
                                       "scope": "own", "see_email": "on", "see_offers": "on", "see_replies": "on",
                                       "update_status": "on", "export": "on"}, follow_redirects=False)
        assert r.status_code == 303
        with session_scope() as s:
            u1 = s.scalar(select(User).where(User.email == E1))
            u2 = s.scalar(select(User).where(User.email == E2))
            assert u1.profile == "view_only" and not any(u1.perms[k] for k in ("add_note", "update_status", "send_offer", "export"))
            assert u2.perms["update_status"] and not u2.perms["see_phone"]
            S.update(u1=u1.id, u2=u2.id)
        c.post(f"/staff/{S['u1']}/assign", data={"company_ids": [str(S["a"])]})
        c.post(f"/staff/{S['u2']}/assign", data={"company_ids": [str(S["b"])]})
    # وصلت دعوة البوابة إلى بريد كل منهما
    from email import message_from_bytes, policy
    from mandoob.mail.sandbox import ROOT
    invites = [message_from_bytes(f.read_bytes(), policy=policy.default) for f in (ROOT / "outbox").glob("*.eml")]
    assert {str(m["To"]) for m in invites if "دعوة" in str(m["Subject"])} >= {E1, E2}


def test_2_each_employee_sees_only_own_customer_and_permissions_enforced():
    with TestClient(app) as c1, TestClient(app) as c2:
        otp_login(c1, E1, "staff")
        otp_login(c2, E2, "staff")
        h1, h2 = c1.get("/portal").text, c2.get("/portal").text
        assert "شركة الأفق للتوزيع" in h1 and "مؤسسة الريم للصيانة" not in h1
        assert "مؤسسة الريم للصيانة" in h2 and "شركة الأفق للتوزيع" not in h2
        # نسخ رابط عميل الزميل لا يفتحه
        assert c1.get(f"/portal/companies/{S['b']}").status_code == 404
        assert c2.get(f"/portal/companies/{S['a']}").status_code == 404
        # الاطلاع فقط: لا ملاحظة ولا تحديث ولا تصدير ولا إرسال
        assert c1.post(f"/portal/companies/{S['a']}/note", data={"note": "x"}).status_code == 403
        assert c1.post(f"/portal/companies/{S['a']}/status", data={"stage": "won"}).status_code == 403
        assert c1.get("/portal/export.xlsx").status_code == 403
        assert c1.post(f"/portal/companies/{S['a']}/send-offer").status_code == 403
        assert "+966114440001" in c1.get(f"/portal/companies/{S['a']}").text
        # منى: تحديث المتابعة مسموح
        r = c2.post(f"/portal/companies/{S['b']}/status",
                    data={"stage": "interested", "next_title": "اتصال الأحد", "next_at": "2026-11-22T13:00"},
                    follow_redirects=False)
        assert r.status_code == 303
        assert c2.post(f"/portal/companies/{S['b']}/note", data={"note": "x"}).status_code == 403
        # الهاتف مخفي عنها في الصفحة والسجل والبحث والتصدير
        page = c2.get(f"/portal/companies/{S['b']}").text
        assert "666 7788" not in page and "6667788" not in page and "ops@reem.example" in page
        assert "مؤسسة الريم" not in c2.get("/portal?q=6667788").text
        x = c2.get("/portal/export.xlsx")
        assert x.status_code == 200
        from io import BytesIO
        from openpyxl import load_workbook
        ws = load_workbook(BytesIO(x.content)).active
        cells = " ".join(str(v) for row in ws.iter_rows(values_only=True) for v in row if v)
        assert "الهاتف" not in cells and "6667788" not in cells and "ops@reem.example" in cells
        # بوابة الإدارة مغلقة على الموظف
        assert c2.get("/companies", follow_redirects=False).headers["location"] == "/portal"
    with session_scope() as s:
        assert s.get(Company, S["b"]).stage == "interested"


def test_3_permission_change_applies_immediately():
    with TestClient(app) as admin, TestClient(app) as c1:
        otp_login(admin, ADMIN, "admin")
        otp_login(c1, E1, "staff")
        assert c1.post(f"/portal/companies/{S['a']}/note", data={"note": "x"}).status_code == 403
        admin.post(f"/staff/{S['u1']}/update", data={"name": "خالد", "team": "فريق الرياض", "profile": "custom",
                                                    "customize": "on", "scope": "own", "see_phone": "on", "add_note": "on"})
        # نفس الجلسة المفتوحة — دون إعادة دخول
        assert c1.post(f"/portal/companies/{S['a']}/note", data={"note": "أول ملاحظة"}, follow_redirects=False).status_code == 303


def test_4_deactivation_cuts_open_session_and_transfers_customers():
    with TestClient(app) as admin, TestClient(app) as c2:
        otp_login(admin, ADMIN, "admin")
        otp_login(c2, E2, "staff")
        assert c2.get("/portal").status_code == 200
        r = admin.post(f"/staff/{S['u2']}/deactivate", data={"transfer_to": str(S["u1"])}, follow_redirects=False)
        assert r.status_code == 303
        # البوابة كانت مفتوحة لديها: أي طلب جديد يُرفض
        r = c2.get("/portal", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/portal/login"
        assert c2.get(f"/portal/companies/{S['b']}", follow_redirects=False).status_code == 303
        # ولا يصلها رمز دخول جديد
        before = last_code_for(E2)
        c2.post("/portal/login/request", data={"email": E2})
        assert last_code_for(E2) == before
    with session_scope() as s:
        b = s.get(Company, S["b"])
        assert b.owner_id == S["u1"]
        assert all(t.owner_id == S["u1"] for t in s.scalars(select(Task).where(Task.company_id == b.id, Task.status == "open")))
        actions = [a.action for a in s.scalars(select(AuditLog).where(AuditLog.target_user_id == S["u2"]))]
        assert {"add_user", "assign_customers", "deactivate"} <= set(actions)
        log = s.scalar(select(AuditLog).where(AuditLog.action == "update_permissions", AuditLog.target_user_id == S["u1"]))
        assert log.actor_name and log.details["before"]["perms"]["add_note"] is False and log.details["after"]["perms"]["add_note"]


def test_5_code_rules(monkeypatch):
    import mandoob.auth as _auth
    monkeypatch.setattr(_auth, "RESEND_AFTER_SECONDS", 0)  # مهلة إعادة الإرسال لها اختبار مستقل
    from mandoob.auth import OTPError, request_code, verify_code
    with session_scope() as s:
        s.query(LoginCode).delete()
    with session_scope() as s:
        u, code1 = request_code(s, E1, "staff")
    with session_scope() as s:
        _, code2 = request_code(s, E1, "staff")  # الرمز الجديد يلغي السابق
    with session_scope() as s:
        try:
            verify_code(s, E1, code1, "staff") if code1 != code2 else None
            assert code1 == code2
        except OTPError:
            pass
    with session_scope() as s:
        assert verify_code(s, E1, code2, "staff").email == E1
    with session_scope() as s:  # لا يُستخدم مرتين
        try:
            verify_code(s, E1, code2, "staff")
            raise AssertionError("reused")
        except OTPError:
            pass
    # الانتهاء بعد ٥ دقائق
    with session_scope() as s:
        s.query(LoginCode).delete()
    with session_scope() as s:
        _, code = request_code(s, E1, "staff")
        lc = s.scalar(select(LoginCode).order_by(LoginCode.id.desc()))
        lc.expires_at = now_utc() - timedelta(seconds=1)
    with session_scope() as s:
        try:
            verify_code(s, E1, code, "staff")
            raise AssertionError("expired accepted")
        except OTPError:
            pass
    # ٥ محاولات خاطئة تُبطل الرمز حتى لو جاء الصحيح بعدها
    with session_scope() as s:
        _, code = request_code(s, E1, "staff")
    for _ in range(5):
        with session_scope() as s:
            try:
                verify_code(s, E1, "000000" if code != "000000" else "111111", "staff")
            except OTPError:
                pass
    with session_scope() as s:
        try:
            verify_code(s, E1, code, "staff")
            raise AssertionError("code after lockout")
        except OTPError:
            pass
    # حد طلبات إعادة الإرسال: ٣ رموز كل ١٥ دقيقة (طُلب رمزان منذ التفريغ؛ الثالث مسموح والرابع مرفوض)
    with session_scope() as s:
        request_code(s, E1, "staff")
    with session_scope() as s:
        try:
            request_code(s, E1, "staff")
            raise AssertionError("rate limit not applied")
        except OTPError as e:
            assert "١٥" in str(e)
    # بريد غير مسجل، أو موظف عند بوابة الإدارة: لا رمز
    with session_scope() as s:
        assert request_code(s, "stranger@x.test", "staff") == (None, None)
        assert request_code(s, E1, "admin") == (None, None)


def test_6_bootstrap_password_only_without_mail():
    with TestClient(app) as c:
        # البريد مربوط في الاختبارات ← الدخول بكلمة المرور معطّل
        r = c.post("/admin/login/password", data={"email": ADMIN, "password": "admin-pass-123"}, follow_redirects=False)
        assert r.headers["location"] == "/admin/login"
        assert c.get("/", follow_redirects=False).headers["location"] == "/admin/login"
        assert "دخول تأسيسي" not in c.get("/admin/login").text


def test_7_email_is_username_clear_messages():
    with TestClient(app) as c:
        r = c.post("/portal/login/request", data={"email": "nobody@syarahapp.test"})
        assert "غير مسجّل" in r.text and 'name="code"' not in r.text
        r = c.post("/portal/login/request", data={"email": ADMIN})
        assert "بوابة الإدارة" in r.text
        r = c.post("/portal/login/request", data={"email": E2})  # منى موقوفة
        assert "موقوف" in r.text
        with session_scope() as s:
            s.query(LoginCode).delete()  # الاختبار السابق استنفد حد الطلبات عمدًا
        r = c.post("/portal/login/request", data={"email": E1.upper()})  # البريد لا يتأثر بحالة الأحرف
        assert "أُرسل كود الدخول" in r.text and 'name="code"' in r.text and "password" not in r.text


def test_8_unconfigured_m365_keeps_bootstrap_until_verified():
    from mandoob.auth import mail_ready
    from mandoob.models import MailAccount
    with session_scope() as s:
        for a in s.scalars(select(MailAccount)):
            a.active = False
        s.add(MailAccount(provider="microsoft365", sender_address="dheya@syarahapp.test", active=True))
    with session_scope() as s:
        assert mail_ready(s) is False  # مفاتيح Microsoft 365 غير مضبوطة ← لا يُغلق الدخول بكلمة المرور
    with TestClient(app) as c:
        assert "دخول تأسيسي" in c.get("/admin/login").text
        r = c.post("/admin/login/password", data={"email": ADMIN, "password": "admin-pass-123"}, follow_redirects=False)
        assert r.headers["location"] == "/"
        r = c.post("/mail/test", data={"to": "", "message": "السلام عليكم"})
        assert "مفاتيح المزود غير مضبوطة" in r.text
    with session_scope() as s:  # إعادة صندوق التجربة لبقية الاختبارات
        for a in s.scalars(select(MailAccount)):
            a.active = a.provider == "sandbox"
