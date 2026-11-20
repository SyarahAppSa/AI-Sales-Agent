"""«ملفات طلبات العملاء»: الإدارة ترفع وتنشر، والمندوب يحمّل وينسخ رسالة ويسجّل الإرسال من بطاقة العميل،
ورابط المشاركة يفتح المستند المعتمد وحده بلا حساب، وتنزيل تقارير العملاء يبقى محظورًا."""
import io
import json
import re

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from helpers import otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import Activity, Company, DownloadAttempt, KitItem, KitLog, User
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
REP = "kit.rep@syarahapp.test"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture(autouse=True)
def blocked(monkeypatch):
    from mandoob.config import settings
    monkeypatch.setattr(settings, "downloads_blocked", True)


def setup_module():
    init_db()
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        adm.post("/staff/add", data={"name": "سالم مندوب الملفات", "email": REP, "team": "", "profile": "custom",
                                     "customize": "on", "scope": "own", "see_phone": "on"})
    with session_scope() as s:
        u = s.query(User).filter(User.email == REP).one()
        s.add(Company(name="شركة عميل الملفات", name_norm="kit-client", activity="نقل", city="الرياض",
                      data_status="complete", lead_source="discovery", owner_id=u.id))


def _item(s, title):
    return s.query(KitItem).filter(KitItem.title == title).one()


def _template():
    wb = Workbook()
    wb.active.append(["رقم اللوحة", "نوع المركبة", "رقم الهيكل"])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_kit_admin_publish_rep_download_message_share_and_block():
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        html = adm.get("/kit").text
        titles = ["بروفايل سيارة اب التعريفي", "السجل التجاري", "الرقم الضريبي وشهادة التسجيل الضريبي", "العنوان الوطني",
                  "نموذج Excel لإضافة المركبات", "رابط تطبيق السائق", "رابط خريطة المحطات", "رابط مواقع تركيب الشرائح",
                  "رابط بوابة إدارة الشركات «أدمن الشركات»", "البريد الإلكتروني الرسمي للشركة", "الرقم الموحد للتواصل",
                  "دليل بدء استخدام الخدمة", "خطوات تفعيل الشركة وإضافة المركبات", "الأسئلة الشائعة"]
        pos = [html.find(f"<b>{t}</b>") for t in titles]
        assert all(p > 0 for p in pos) and pos == sorted(pos)  # الترتيب المطلوب
        with session_scope() as s:
            prof, tmpl, tax, mp = (_item(s, titles[0]).id, _item(s, titles[4]).id, _item(s, titles[2]).id, _item(s, titles[6]).id)
        # رفع ومعاينة قبل النشر (مسودة لا يراها المندوب)
        adm.post(f"/kit/{prof}/save", data={"title": titles[0], "kind": "file", "share_enabled": "on"},
                 files={"file": ("profile.pdf", PDF, "application/pdf")})
        r = adm.get(f"/kit/{prof}/view")
        assert r.status_code == 200 and r.content == PDF and "inline" in r.headers["content-disposition"]
        adm.post(f"/kit/{prof}/save", data={"title": titles[0], "kind": "file", "published": "on", "share_enabled": "on",
                                            "expires_on": "2030-12-31"})
        adm.post(f"/kit/{tmpl}/save", data={"title": titles[4], "kind": "template", "published": "on", "share_enabled": "on"},
                 files={"file": ("نموذج المركبات.xlsx", _template(), "application/octet-stream")})
        adm.post(f"/kit/{tax}/save", data={"title": titles[2], "kind": "number", "value": "300123456700003",
                                           "published": "on", "share_enabled": "on"})
        adm.post(f"/kit/{mp}/save", data={"title": titles[6], "kind": "link", "url": "https://maps.example.com/stations",
                                          "published": "on", "share_enabled": "on"})
        html = adm.get("/kit").text
        assert "منشور للمندوبين" in html and "2030" in html and "النسخة 1" in html
    with session_scope() as s:
        cid = s.query(Company).filter(Company.name == "شركة عميل الملفات").one().id
        share_token = _item(s, titles[0]).share_token
    with TestClient(app) as rep:
        otp_login(rep, REP, "portal")
        card = rep.get(f"/portal/companies/{cid}").text
        assert f'href="/portal/kit?company={cid}"' in card
        assert 'href="/portal/kit"' in rep.get("/portal").text  # في قائمة «المبيعات والمتابعة»
        page = rep.get(f"/portal/kit?company={cid}").text
        assert "بروفايل سيارة اب التعريفي" in page and "السجل التجاري" not in page  # المسودات الفارغة لا تظهر
        assert "تحميل الملف" in page and "نسخ رسالة للعميل" in page and "تسجيل ما تم إرساله" in page and "نسخ الرقم" in page
        # تحميل النموذج — مسموح لأنه معتمد في هذا القسم
        r = rep.get(f"/kit/{tmpl}/file?company={cid}", follow_redirects=False)
        assert r.status_code == 200 and r.content.startswith(b"PK") and "attachment" in r.headers["content-disposition"]
        # رسالة تضم رابط البروفايل والرقم الضريبي والخريطة
        lines = {}
        for m in re.finditer(r'class="kit-pick" value="(\d+)"[^>]*data-lines="([^"]*)"', page):
            import html as _h
            lines[int(m.group(1))] = json.loads(_h.unescape(m.group(2)))
        msg = lines[prof] + lines[tax] + lines[mp]
        assert any(f"/kit/s/{share_token}" in x for x in msg)
        assert any("300123456700003" in x for x in msg) and any("maps.example.com/stations" in x for x in msg)
        r = rep.post("/kit/sent", data={"company_id": str(cid), "item_ids": [str(prof), str(tax), str(mp)]},
                     follow_redirects=True)
        assert "سُجّل إرسال 3 عنصر" in r.text and "سبق إرساله لهذا العميل (3)" in r.text and "سالم مندوب الملفات" in r.text
        # تنزيل تقرير العملاء ما زال محظورًا
        r = rep.get("/portal/export.xlsx", headers={"referer": "http://testserver/portal"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers.get("x-download-blocked") == "1"
        # المسودة لا تُحمَّل بمعرفها المباشر
        with session_scope() as s:
            cr = _item(s, titles[1])
            cr.file_data, cr.file_name, cr.mime_type = PDF, "cr.pdf", "application/pdf"
            crid = cr.id
        assert rep.get(f"/kit/{crid}/file").status_code == 404
    # متصفح غير مسجّل: رابط البروفايل يعرض الملف المعتمد وحده
    with TestClient(app) as anon:
        r = anon.get(f"/kit/s/{share_token}", follow_redirects=False)
        assert r.status_code == 200 and r.content == PDF and r.headers["content-type"].startswith("application/pdf")
        assert anon.get("/kit", follow_redirects=False).status_code in (303, 401, 403)
        assert anon.get(f"/kit/{prof}/file", follow_redirects=False).status_code in (303, 401, 403)
        assert anon.get("/kit/s/not-a-token").status_code == 404
    with session_scope() as s:
        assert s.query(KitLog).filter_by(action="download", item_id=tmpl, company_id=cid).count() == 1
        assert s.query(KitLog).filter_by(action="sent", company_id=cid).count() == 3
        assert s.query(KitLog).filter_by(action="share_open", item_id=prof).count() == 1
        assert s.query(Activity).filter(Activity.company_id == cid, Activity.title == "أُرسلت ملفات للعميل").count() == 1
        # التحميل المعتمد لا يدخل «لوحة طوارئ»؛ والمحاولة المحظورة تدخل
        assert not s.query(DownloadAttempt).filter(DownloadAttempt.target.like("/kit/%")).count()
        assert s.query(DownloadAttempt).filter(DownloadAttempt.target == "/portal/export.xlsx").count() >= 1
    # إيقاف المشاركة يوقف الرابط فورًا
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        adm.post(f"/kit/{prof}/save", data={"title": titles[0], "kind": "file", "published": "on"})
        assert adm.get(f"/kit/s/{share_token}").status_code == 404
        # إضافة عنصر جديد وترتيبه
        adm.post("/kit/new", data={"title": "عرض الأسعار المعتمد", "kind": "link", "url": "https://example.com/prices",
                                   "published": "on", "share_enabled": "on"})
        with session_scope() as s:
            nid = _item(s, "عرض الأسعار المعتمد").id
            last = s.get(KitItem, nid).position
        adm.post(f"/kit/{nid}/move", data={"dir": "up"})
        with session_scope() as s:
            assert s.get(KitItem, nid).position == last - 1


def test_rep_cannot_manage_kit():
    with TestClient(app) as rep:
        otp_login(rep, REP, "portal")
        assert rep.post("/kit/new", data={"title": "x"}, follow_redirects=False).status_code == 403
        assert rep.get("/kit", follow_redirects=False).status_code in (303, 403)
