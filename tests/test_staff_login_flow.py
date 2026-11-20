"""دخول موظف المبيعات ببريده وكود يصله من بريد الشركة المربوط — المسار كاملًا عبر مداخل التطبيق."""
from datetime import timedelta
from email import message_from_bytes, policy

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from helpers import ensure_mail, last_code_for, otp_login
from mandoob.db import init_db, session_scope
from mandoob.mail.sandbox import ROOT
from mandoob.models import Company, Contact, LoginCode, OutboundMessage, User
from mandoob.timeutil import now_utc
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
REP = "Login.Flow.Rep@SyarahApp.test"
S = {}


def _last_mail(to):
    files = sorted((ROOT / "outbox").glob("*.eml"), key=lambda p: p.stat().st_mtime_ns)
    for f in reversed(files):
        m = message_from_bytes(f.read_bytes(), policy=policy.default)
        if str(m["To"]).strip().lower() == to.lower():
            return m


def _age_codes(email):  # يحاكي مرور دقيقة منذ آخر إرسال
    with session_scope() as s:
        uid = s.scalar(select(User.id).where(User.email == email.lower()))
        for lc in s.scalars(select(LoginCode).where(LoginCode.user_id == uid)):
            lc.created_at = lc.created_at - timedelta(seconds=61)


def setup_module():
    init_db()
    ensure_mail()
    with session_scope() as s:
        for i, nm in enumerate(["منشأة مسندة للموظف", "منشأة لموظف آخر"]):
            c = Company(name=nm, name_norm=f"loginflow-{i}", activity="نقل", city="الرياض", data_status="complete",
                        lead_source="discovery", website=f"https://loginflow-{i}.example", domain=f"loginflow-{i}.example")
            s.add(c)
            s.flush()
            c.contacts.append(Contact(kind="phone", value=f"+96659911{i:04d}", source_url=f"https://loginflow-{i}.example/c",
                                      verified=True))
            S[i] = c.id


def test_full_staff_login_with_company_mail_code():
    with session_scope() as s:
        out_before = s.scalar(select(func.count(OutboundMessage.id)))
    # ١) الأدمن يضيف الموظف وصلاحياته؛ البريد هو اسم المستخدم
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        adm.post("/staff/add", data={"name": "موظف مسار الدخول", "email": REP, "profile": "sales_rep"})
        with session_scope() as s:
            u = s.scalar(select(User).where(User.email == REP.lower()))
            assert u and u.role == "rep" and u.active
            S["uid"] = u.id
            s.get(Company, S[0]).owner_id = u.id
    with TestClient(app) as c:
        # ٢) يكتب بريده فقط ← الكود يصل من بريد الشركة المربوط
        r = c.post("/portal/login/request", data={"email": REP.upper()})
        m = _last_mail(REP)
        assert m is not None and str(m["Subject"]) == "رمز دخولك إلى منصة المندوب الذكي لسيارة اب"
        assert "sales@sayaraup.test" in str(m["From"])                          # من بريد الشركة المربوط
        html = m.get_body(preferencelist=("html",)).get_content()
        code1 = last_code_for(REP)
        assert code1 and len(code1) == 6 and code1 in html and "font-size:40px" in html and "٥ دقائق" in html
        # ٣) حقل «أدخل كود الدخول» وزر إعادة الإرسال معطّل دقيقة
        assert "أدخل كود الدخول" in r.text and "إعادة إرسال الكود" in r.text and 'id="resend" disabled' in r.text
        c.post("/portal/login/request", data={"email": REP})                       # قبل الدقيقة: لا كود جديد
        assert last_code_for(REP) == code1
        _age_codes(REP)
        c.post("/portal/login/request", data={"email": REP})                       # بعد الدقيقة: كود جديد
        code2 = last_code_for(REP)
        with session_scope() as s:  # كود جديد صدر والسابق أُلغي
            codes = list(s.scalars(select(LoginCode).where(LoginCode.user_id == S["uid"]).order_by(LoginCode.id)))
            assert len(codes) == 2 and codes[0].revoked and not codes[1].revoked
        r = c.post("/portal/login/verify", data={"code": code1}, follow_redirects=False)
        if code2 != code1:
            assert r.headers["location"] == "/portal/login"                       # السابق انتهت صلاحيته
        # ٤) الكود الصحيح ← البوابة مباشرة، وعملاؤه المسندون فقط
        r = c.post("/portal/login/verify", data={"code": code2}, follow_redirects=False)
        assert r.headers["location"] == "/portal"
        home = c.get("/portal").text
        assert "منشأة مسندة للموظف" in home and "منشأة لموظف آخر" not in home
    with TestClient(app) as c2:                                                   # الكود نفسه مرة ثانية ← مرفوض
        c2.post("/portal/login/request", data={"email": REP})  # (داخل الدقيقة: لا يُرسل جديد)
        r = c2.post("/portal/login/verify", data={"code": code2}, follow_redirects=False)
        assert r.headers["location"] == "/portal/login"
        assert c2.get("/portal", follow_redirects=False).headers["location"] == "/portal/login"
    # التعطيل ← ينتهي الوصول وتتوقف الأكواد
    with TestClient(app) as c3:
        _age_codes(REP)
        c3.post("/portal/login/request", data={"email": REP})
        ok = c3.post("/portal/login/verify", data={"code": last_code_for(REP)}, follow_redirects=False)
        assert ok.headers["location"] == "/portal"
        with TestClient(app) as adm:
            otp_login(adm, ADMIN, "admin")
            adm.post(f"/staff/{S['uid']}/deactivate", data={"transfer_to": ""})
        assert c3.get("/portal", follow_redirects=False).headers["location"] == "/portal/login"
        _age_codes(REP)
        before = last_code_for(REP)
        c3.post("/portal/login/request", data={"email": REP})
        assert last_code_for(REP) == before                                       # لا كود لحساب موقوف
    with session_scope() as s:  # العروض التسويقية تبقى معطّلة: لا رسائل في طابور الإرسال
        assert s.scalar(select(func.count(OutboundMessage.id))) == out_before
