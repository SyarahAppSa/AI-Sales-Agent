"""«بريد العملاء»: ربط صندوق مستقل (Zoho)، إرسال يدوي بأي نص وعدة مرفقات، قراءة الردود عبر IMAP وربطها بالمحادثة
والشركة مع تنبيه، واحترام قائمة إيقاف التواصل — دون المساس ببريد أكواد الدخول."""
import email
from email.message import EmailMessage
from email.policy import default as policy

import pytest
from fastapi.testclient import TestClient

from helpers import otp_login
from mandoob import customer_mail as CM
from mandoob.db import init_db, session_scope
from mandoob.models import (Company, Contact, CustomerMail, CustomerMailFile, MailAccount, Notification, Suppression,
                            User)
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
SENT: list = []


class FakeSMTP:
    def __init__(self, host):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, u, p):
        assert u == "syarahapp@mandoob.live" and p == "app-pass-123"

    def send_message(self, m):
        SENT.append(m)


class FakeIMAP:
    def __init__(self, host, msgs):
        self.msgs = msgs

    def login(self, u, p):
        pass

    def select(self, box, readonly=False):
        return "OK", [b"1"]

    def response(self, k):
        return k, [b"777"]

    def uid(self, cmd, *args):
        if cmd == "search":
            return "OK", [" ".join(str(u) for u in self.msgs).encode()]
        u = int(args[0])
        return "OK", [(b"1 (BODY[] {n}", self.msgs[u]), b")"]

    def logout(self):
        pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(CM, "_smtp", lambda h: FakeSMTP(h))
    monkeypatch.setattr(CM, "connect", lambda a, p: ({"smtp_host": "smtppro.zoho.com", "imap_host": "imappro.zoho.com"}, ""))
    monkeypatch.setattr(CM, "_append_sent", lambda *a: None)


def test_customer_mail_send_reply_and_threading(monkeypatch):
    init_db()
    with session_scope() as s:
        co = Company(name="شركة بريد العملاء للفحص", name_norm="شركة بريد العملاء للفحص", data_status="complete")
        s.add(co)
        s.flush()
        s.add(Contact(company_id=co.id, kind="email", value="buyer@cm-test.sa", source_url="https://cm-test.sa"))
        s.add(Suppression(kind="email", value="stop@cm-test.sa", channel="all", reason="طلب الإيقاف"))
        cid = co.id
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        with session_scope() as s:
            login_accounts = [(a.id, a.active) for a in s.query(MailAccount)]
        page = cl.get("/customer-mail").text
        assert "ربط بريد العملاء" in page and "syarahapp@mandoob.live" in page and 'href="/customer-mail"' in page
        r = cl.post("/customer-mail/connect", data={"address": "syarahapp@mandoob.live", "name": "سيارة آب",
                                                    "password": "app-pass-123"}, follow_redirects=True)
        assert "رُبط syarahapp@mandoob.live" in r.text
        with session_scope() as s:
            cfg = CM.get_cfg(s)
            assert cfg["imap_host"] == "imappro.zoho.com" and "app-pass-123" not in str(cfg)
            assert [(a.id, a.active) for a in s.query(MailAccount)] == login_accounts  # بريد أكواد الدخول كما هو
        cl.post("/customer-mail/settings", data={"name": "سيارة آب", "signature": "فريق سيارة آب\n0500000000"})
        page = cl.get(f"/customer-mail?company={cid}").text
        assert 'value="buyer@cm-test.sa"' in page and "شركة بريد العملاء للفحص" in page
        r = cl.post("/customer-mail/send", data={"to": "buyer@cm-test.sa", "cc": "", "subject": "عرض أسعار السيارات",
                                                 "body": "السلام عليكم،\nمرفق العرض وأي تفاصيل تحتاجها.", "company_id": str(cid)},
                    files=[("files", ("عرض.pdf", b"%PDF-1.4 test", "application/pdf")),
                           ("files", ("prices.xlsx", b"PK\x03\x04xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
                    follow_redirects=True)
        assert "أُرسل البريد إلى buyer@cm-test.sa مع 2 مرفق" in r.text
        m = SENT[-1]
        assert m["To"] == "buyer@cm-test.sa" and "syarahapp@mandoob.live" in m["From"]
        assert sorted(p.get_filename() for p in m.iter_attachments()) == ["prices.xlsx", "عرض.pdf"]
        assert "فريق سيارة آب" in m.get_body(("plain",)).get_content()
        blocked = cl.post("/customer-mail/send", data={"to": "stop@cm-test.sa", "subject": "x", "body": "y"}, follow_redirects=True)
        assert "قائمة إيقاف التواصل" in blocked.text
        with session_scope() as s:
            out = s.query(CustomerMail).filter_by(subject="عرض أسعار السيارات").one()
            assert out.company_id == cid and out.status == "sent" and s.query(CustomerMailFile).filter_by(mail_id=out.id).count() == 2
            mid, out_id = out.message_id, out.id
        # رد العميل عبر IMAP
        rep = EmailMessage()
        rep["From"] = "محمد المشتريات <buyer@cm-test.sa>"
        rep["To"] = "syarahapp@mandoob.live"
        rep["Subject"] = "Re: عرض أسعار السيارات"
        rep["Message-ID"] = "<reply-1@cm-test.sa>"
        rep["In-Reply-To"] = mid
        rep["Date"] = email.utils.formatdate()
        rep.set_content("شكرًا، نحتاج 12 سيارة. متى نجتمع؟")
        rep.add_attachment(b"specs", maintype="application", subtype="pdf", filename="specs.pdf")
        other = EmailMessage()
        other["From"] = "new@other-co.sa"
        other["Subject"] = "استفسار"
        other["Message-ID"] = "<new-1@other-co.sa>"
        other.set_content("هل لديكم سيارات للإيجار؟")
        monkeypatch.setattr(CM, "_imap", lambda h: FakeIMAP(h, {5: rep.as_bytes(), 6: other.as_bytes()}))
        assert CM.sync() == {"new": 2}
        assert CM.sync() == {"new": 0}  # لا تكرار
        with session_scope() as s:
            r1 = s.query(CustomerMail).filter_by(message_id="<reply-1@cm-test.sa>").one()
            assert r1.thread_key == mid and r1.company_id == cid and r1.direction == "in" and not r1.read
            assert s.query(CustomerMailFile).filter_by(mail_id=r1.id, name="specs.pdf").count() == 1
            assert s.query(Notification).filter(Notification.title.like("رد بريد من شركة بريد العملاء للفحص%")).count() >= 1
            assert CM.get_cfg(s)["last_uid"] == 6 and CM.unread_count(s) >= 2
        lst = cl.get("/customer-mail").text
        assert "رد جديد" in lst and "استفسار" in lst
        th = cl.get(f"/customer-mail?m={out_id}").text
        assert "نحتاج 12 سيارة" in th and "specs.pdf" in th and "إرسال الرد" in th and 'value="buyer@cm-test.sa"' in th
        with session_scope() as s:
            assert s.query(CustomerMail).filter_by(message_id="<reply-1@cm-test.sa>").one().read
        r = cl.post("/customer-mail/send", data={"to": "buyer@cm-test.sa", "subject": "Re: عرض أسعار السيارات",
                                                 "body": "يوم الأحد الساعة 10 مناسب؟", "reply_to_id": str(r1.id)}, follow_redirects=True)
        assert SENT[-1]["In-Reply-To"] == "<reply-1@cm-test.sa>" and mid in SENT[-1]["References"]
        with session_scope() as s:
            assert s.query(CustomerMail).filter_by(thread_key=mid).count() == 3
