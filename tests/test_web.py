"""اختبار الواجهة: الدخول، الصلاحيات، وكل الصفحات تعمل."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from mandoob.auth import hash_password
from mandoob.db import init_db, session_scope
from mandoob.models import Company, OutboundMessage, User
from mandoob.web.app import app


def _login(c, email, pw=None, portal="admin"):
    from helpers import otp_login
    otp_login(c, email, portal)


def test_pages_render_for_admin_and_scoping_for_rep():
    init_db()
    with TestClient(app) as c:
        assert c.get("/", follow_redirects=False).headers["location"] == "/admin/login"
        _login(c, "admin@sayaraup.test", "admin-pass-123")
        for url in ["/", "/targets", "/companies", "/companies?status=incomplete", "/offers-report", "/followups",
                    "/offers", "/mail", "/team", "/settings", "/runs", "/daily-report", "/notifications", "/sandbox"]:
            r = c.get(url)
            assert r.status_code == 200, url
            assert 'dir="rtl"' in r.text
        r = c.get("/offers-report")
        assert "عدد الشركات الفريدة التي أُرسلت إليها عروض" in r.text and "إجمالي العروض المرسلة" in r.text
        x = c.get("/offers-report.xlsx?reply=replied")
        assert x.status_code == 200 and x.content[:2] == b"PK"
        with session_scope() as s:
            cid = s.scalar(select(Company.id).where(Company.domain == "alpha-rent.example"))
            mid = s.scalar(select(OutboundMessage.id).where(OutboundMessage.company_id == cid))
        r = c.get(f"/companies/{cid}")
        assert r.status_code == 200 and "مهتم، اتصلوا بي الأحد بعد الظهر" in r.text
        assert c.get(f"/messages/{mid}").status_code == 200
        assert c.get(f"/messages/{mid}/attachment").content.startswith(b"%PDF")
        # تحويل طلب عربي إلى إعدادات
        r = c.post("/targets/parse", data={"text": "شركات التوصيل في جدة", "daily_target": "50"})
        assert r.status_code == 200 and "التوصيل" in r.text and "جدة" in r.text
    with session_scope() as s:
        if not s.scalar(select(User).where(User.email == "rep2@sayaraup.test")):
            s.add(User(name="مندوب آخر", email="rep2@sayaraup.test", password_hash=hash_password("p"), role="rep",
                       perms={"see_phone": True}))
    with TestClient(app) as c:
        _login(c, "rep2@sayaraup.test", portal="staff")
        assert c.get(f"/portal/companies/{cid}").status_code == 404  # ليست من عملائه
        assert c.get("/targets", follow_redirects=False).headers["location"] == "/portal"  # بوابة الإدارة مغلقة عليه
        assert c.get("/staff", follow_redirects=False).status_code == 303
        assert c.post("/targets/parse", data={"text": "x"}, follow_redirects=False).status_code == 303

def test_save_campaign_from_parsed_form():
    import re
    from mandoob.models import Campaign
    with TestClient(app) as c:
        _login(c, "admin@sayaraup.test", "admin-pass-123")
        html = c.post("/targets/parse", data={"text": "شركات التوصيل وتأجير السيارات في جدة. ٣٠ شركة توصيل و٢٠ شركة تأجير سيارات"}).text
        # نجمع حقول النموذج كما يرسلها المتصفح
        form = {}
        for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html):
            form.setdefault(m.group(1), m.group(2))
        for m in re.finditer(r'<textarea[^>]*name="([^"]+)"[^>]*>([^<]*)</textarea>', html):
            form[m.group(1)] = m.group(2)
        for m in re.finditer(r'<select name="([^"]+)">(.*?)</select>', html, re.S):
            sel = re.search(r'<option value="([^"]*)" selected', m.group(2))
            form[m.group(1)] = sel.group(1) if sel else ""
        form["cat_0_active"] = form["cat_1_active"] = "on"
        form["send_days"] = ["6", "0"]
        r = c.post("/targets/save", data=form, follow_redirects=False)
        assert r.status_code == 303
        cid = int(r.headers["location"].rsplit("/", 1)[1])
    with session_scope() as s:
        camp = s.get(Campaign, cid)
        assert camp.status == "draft" and camp.daily_target == 50
        assert sorted((c.sector_key, c.quota, tuple(c.cities)) for c in camp.categories) == [
            ("car_rental", 20, ("جدة",)), ("delivery", 30, ("جدة",))]
        assert camp.send_days == [6, 0]
