"""العربية | English: الاتجاه، الترجمة، بقاء أسماء الشركات والملاحظات كما أُدخلت، Excel، رسالة الكود، وتذكّر الاختيار."""
import io
import re

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from helpers import last_code_for, otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import Company, Contact, User
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
NOTE = "لم يرد"  # ملاحظة تطابق اسم حالة: يجب ألا تُترجم لأنها نص الموظف
S = {}


def setup_module():
    init_db()
    with session_scope() as s:
        c = Company(name="مؤسسة اللغتين للنقل", name_norm="i18n-co", activity="نقل", city="الرياض", data_status="complete",
                    lead_source="discovery", website="https://i18n.example", domain="i18n.example", last_note=NOTE)
        s.add(c)
        s.flush()
        c.contacts.append(Contact(kind="phone", value="+966557304418", source_url="https://i18n.example/c", verified=True))
        c.contacts.append(Contact(kind="email", value="info@i18n.example", source_url="https://i18n.example/c", verified=True))
        S["cid"] = c.id


def test_switch_keeps_page_and_user_text_and_direction():
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        url = f"/companies/{S['cid']}"
        ar = c.get(url).text
        assert '<html lang="ar" dir="rtl">' in ar and "العربية" in ar and "English" in ar
        r = c.get(f"/lang?to=en&next={url}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == url                 # الصفحة نفسها
        en = c.get(url).text
        assert '<html lang="en" dir="ltr">' in en
        assert "مؤسسة اللغتين للنقل" in en                                              # اسم الشركة كما أُدخل
        assert re.search(r'translate="no">\s*' + NOTE, en)                               # الملاحظة كما كُتبت
        assert "+966557304418" in en or "966557304418" in en.replace(" ", "")          # الهاتف واضح
        assert "info@i18n.example" in en
        assert "Start contact" in en and "Dashboard" in en and "بدء التواصل" not in en
        q = c.get("/companies?status=complete&q=اللغتين").text                         # الفلاتر تبقى في الرابط
        assert 'value="اللغتين"' in q and "Download Excel" in q
        # Excel: عناوين الأعمدة بالإنجليزية، والملاحظة واسم الشركة كما أُدخلا
        wb = load_workbook(io.BytesIO(c.get("/companies.xlsx?status=complete&q=اللغتين").content))
        rows = [[("" if v is None else str(v)) for v in r] for r in wb.active.iter_rows(values_only=True)]
        head = next(r for r in rows if "Business" in r or "Company" in r or any("Phone" in x for x in r))
        assert not any(re.search(r"[؀-ۿ]", h) for h in head), head
        line = next(r for r in rows if "مؤسسة اللغتين للنقل" in r)
        assert NOTE in line                                                               # الملاحظة لم تُترجم
        c.get(f"/lang?to=ar&next={url}")
        assert '<html lang="ar" dir="rtl">' in c.get(url).text


def test_choice_remembered_after_logout_and_login_code_email_in_english(monkeypatch):
    from mandoob import auth
    monkeypatch.setattr(auth, "RESEND_AFTER_SECONDS", 0)
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        c.get("/lang?to=en")
        with session_scope() as s:
            assert s.query(User).filter(User.email == ADMIN).one().lang == "en"
        c.get("/logout")
    with TestClient(app) as fresh:                                                        # متصفح جديد بلا ملفات تعريف
        assert '<html lang="ar"' in fresh.get("/admin/login").text                      # العربية افتراضيًا
        otp_login(fresh, ADMIN, "admin")
        page = fresh.get("/").text
        assert '<html lang="en" dir="ltr">' in page and "Dashboard" in page             # تذكّر الاختيار
    with TestClient(app) as c2:
        c2.get("/lang?to=en")
        assert "Send sign-in code" in c2.get("/admin/login").text or "sign-in code" in c2.get("/admin/login").text
        c2.post("/admin/login/request", data={"email": ADMIN})
        from email import message_from_bytes, policy
        from mandoob.mail.sandbox import ROOT
        f = sorted((ROOT / "outbox").glob("*.eml"), key=lambda p: p.stat().st_mtime_ns)[-1]
        m = message_from_bytes(f.read_bytes(), policy=policy.default)
        assert str(m["Subject"]) == "Sign-in code for Syarah App Smart Sales Agent"
        body = m.get_body(preferencelist=("plain",)).get_content()
        assert "Valid for 5 minutes" in body and last_code_for(ADMIN)
        c2.post("/admin/login/verify", data={"code": last_code_for(ADMIN)})  # استهلاك الرمز (لا يبقى رمز معلّق)


def teardown_module():
    with session_scope() as s:  # بقية الاختبارات تتوقع العربية الافتراضية
        for u in s.query(User).all():
            u.lang = None
