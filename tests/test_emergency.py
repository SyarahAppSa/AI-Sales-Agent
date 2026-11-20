"""«لوحة طوارئ»: منع كل تنزيل (مندوب ومدير، أيقونة Excel ورابط مباشر ورابط قديم ومرفق)، تسجيل المحاولة باسم الحساب
الصحيح وتنبيه غير مقروء حتى تُفتح اللوحة، العلامة المائية، حالة الخوادم و«تعذّر التحديث»، والسجل الزمني."""
import re

import pytest
from fastapi.testclient import TestClient

from helpers import otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import Company, DownloadAttempt, User
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
REP = "emerg.rep@syarahapp.test"


@pytest.fixture(autouse=True)
def blocked(monkeypatch):
    from mandoob.config import settings
    monkeypatch.setattr(settings, "downloads_blocked", True)


def setup_module():
    init_db()
    with TestClient(app) as adm:
        from mandoob.config import settings
        otp_login(adm, ADMIN, "admin")
        adm.post("/staff/add", data={"name": "أحمد المندوب", "email": REP, "team": "", "profile": "custom", "customize": "on",
                                     "scope": "own", "see_phone": "on", "export": "on", "see_reports": "on"})
    with session_scope() as s:
        u = s.query(User).filter(User.email == REP).one()
        s.add(Company(name="منشأة الطوارئ المسندة", name_norm="emerg-own", activity="نقل", city="الرياض",
                      data_status="complete", lead_source="discovery", owner_id=u.id))
        s.add(Company(name="منشأة لزميل آخر", name_norm="emerg-other", activity="نقل", city="الرياض",
                      data_status="complete", lead_source="discovery"))


def _no_file(r):
    assert r.status_code == 303 and r.headers.get("x-download-blocked") == "1"
    assert not r.content.startswith(b"PK") and "spreadsheet" not in r.headers.get("content-type", "")


def test_rep_then_admin_downloads_blocked_and_alerted():
    with TestClient(app) as rep:
        otp_login(rep, REP, "portal")
        home = rep.get("/portal").text
        assert "تنزيل Excel" in home                                                   # الأيقونة باقية
        assert "منشأة الطوارئ المسندة" in home and "منشأة لزميل آخر" not in home        # المسند إليه فقط
        r = rep.get("/portal/export.xlsx", headers={"referer": "http://testserver/portal"}, follow_redirects=False)
        _no_file(r)
        page = rep.get(r.headers["location"]).text
        assert "تنزيل الملفات محظور وفق سياسة حماية بيانات المنصة" in page
        _no_file(rep.get("/portal/officials.xlsx", follow_redirects=False))             # رابط مباشر
        assert "/portal/export/phones.csv" in home
        _no_file(rep.get("/portal/export/phones.csv", follow_redirects=False))          # «نسخ كل الأرقام»
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        _no_file(adm.get("/officials.xlsx", headers={"referer": "http://testserver/officials"}, follow_redirects=False))
        _no_file(adm.get("/companies.xlsx?status=complete&kind=real", follow_redirects=False))   # رابط قديم بمعاملات
        _no_file(adm.get("/officials.xlsx", follow_redirects=False))                    # تكرار
        _no_file(adm.get("/messages/1/attachment", follow_redirects=False))             # مرفق
        _no_file(adm.get("/old-exports/customers.csv", follow_redirects=False))         # رابط قديم غير موجود
        _no_file(adm.get("/reports/weekly.pdf", follow_redirects=False))
        with session_scope() as s:
            rows = list(s.query(DownloadAttempt).order_by(DownloadAttempt.id))
            reps_ = [a for a in rows if a.user_email == REP]
            assert {a.label for a in reps_} >= {"تقرير «عملائي» (بوابة الموظفين)", "تقرير مسؤولي الشركات (بوابة الموظفين)"}
            assert all(a.user_name == "أحمد المندوب" for a in reps_)
            off = [a for a in rows if a.user_email == ADMIN and a.target == "/officials.xlsx"]
            assert len(off) == 2 and off[0].report_id == "127" and off[0].page == "/officials"
        dash = adm.get("/").text
        assert re.search(r'لوحة طوارئ</span><span class="count">\d+', dash) and "bellcount" in dash
        em = adm.get("/emergency").text
        assert "مُنع تنزيل تقرير موظفي الشركات." in em and "رقم التقرير: <b>127</b>" in em and "كرّر المحاولة 2 مرات" in em
        assert "أحمد المندوب" in em and REP in em and "جديد" in em
        assert 'class="count"' not in adm.get("/").text.split("لوحة طوارئ")[1][:60]      # قُرئت بعد فتح اللوحة
        assert "--wm:url(" in em and "data:image/svg+xml" in em                         # علامة مائية باسم المستخدم


def test_rep_cannot_open_emergency_panel():
    with TestClient(app) as rep:
        otp_login(rep, REP, "portal")
        assert rep.get("/emergency", follow_redirects=False).status_code in (303, 403)


def test_server_status_and_metric_failure_shows_last_success(monkeypatch):
    from mandoob import emergency as E
    E.refresh_metrics("web", reader=lambda prev: {"mem_used_mb": 210.5, "mem_limit_mb": 512.0, "disk_total_gb": 10.0,
                                                  "disk_used_gb": 2.0, "disk_free_gb": 8.0, "cpu_limit": 0.5})

    def broken(prev):
        raise OSError("cgroup unavailable")
    monkeypatch.setattr(E, "container_metrics", broken)
    monkeypatch.setattr(E.refresh_metrics, "__defaults__", (broken,))
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        em = adm.get("/emergency").text
        assert "خدمة الويب (الموقع)" in em and "قاعدة البيانات" in em and "خدمة البحث المجدول (العامل)" in em
        assert "تعذّر التحديث — آخر قراءة ناجحة:" in em and "210.5 / 512.0 MB" in em
        assert "رصد التصوير: <b>غير متاح</b>" in em
        tl = adm.get("/emergency?etype=download").text
        assert "مُنع تنزيل" in tl and "تسجيل الدخول" in adm.get("/emergency?etype=login").text
