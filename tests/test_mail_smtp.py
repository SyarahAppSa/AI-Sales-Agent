"""كلمة سر SMTP من الصفحة: تُختبر بالدخول، تُحفظ مشفّرة، لا تظهر في الصفحة، ويستخدمها الإرسال."""
from fastapi.testclient import TestClient

from helpers import otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import MailAccount
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"


def test_smtp_password_saved_encrypted_and_used(monkeypatch):
    import smtplib
    from mandoob.mail import smtp as M
    init_db()
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, context=None, timeout=None):
            if host != "smtppro.zoho.com":
                raise OSError("unreachable")
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            if p != "Zoho-App-Pass-123":
                raise smtplib.SMTPAuthenticationError(535, b"bad")
            calls.append(("login", self.host, u))

        def send_message(self, mime):
            calls.append(("send", self.host, mime["To"]))
    monkeypatch.setattr(M.smtplib, "SMTP_SSL", FakeSMTP)
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        c.post("/mail", data={"provider": "smtp", "sender_address": "dheya@mandoob-test.example", "sender_name": "سيارة آب"})
        r = c.post("/mail/smtp", data={"password": "wrong"}, follow_redirects=True)
        assert "كلمة السر أو البريد غير صحيح" in r.text
        with session_scope() as s:
            a = s.query(MailAccount).filter_by(active=True).one()
            assert a.smtp_password_enc is None
        r = c.post("/mail/smtp", data={"password": "Zoho-App-Pass-123"}, follow_redirects=True)
        assert "نجح الاتصال بخادم البريد (smtppro.zoho.com)" in r.text and "Zoho-App-Pass-123" not in r.text
        with session_scope() as s:
            a = s.query(MailAccount).filter_by(active=True).one()
            assert a.smtp_host == "smtppro.zoho.com" and "Zoho-App-Pass-123" not in (a.smtp_password_enc or "")
        r = c.post("/mail/test", data={"to": "owner@mandoob-test.example"}, follow_redirects=True)
        assert "نجح الإرسال" in r.text
        assert ("send", "smtppro.zoho.com", "owner@mandoob-test.example") in calls
    # إعادة البريد التجريبي حتى لا تتأثر بقية الاختبارات
    with session_scope() as s:
        for a in s.query(MailAccount).all():
            a.active = a.provider == "sandbox"
