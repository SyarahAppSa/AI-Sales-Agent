"""«مواقع التواصل الاجتماعي» في الحملة: رفع ← معاينة ← اعتماد واستيراد بلا تكرار، وفلتر «مصدر العميل»."""
import io

from fastapi.testclient import TestClient
from openpyxl import Workbook

from helpers import otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import Campaign, CampaignLead, CampaignSourceFile, Company, Contact, User
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"


def xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_social_sources_preview_import_dedupe_and_filter():
    init_db()
    with session_scope() as s:
        camp = Campaign(name="حملة تجريبية سوشال", status="draft")
        s.add(camp)
        s.flush()
        cid = camp.id
        # عميل موجود مسبقًا بمسؤول وملاحظة — يجب ألا يتغيرا
        admin = s.query(User).filter(User.email == ADMIN).first()
        old = Company(name="شركة قديمة للتجربة", name_norm="شركه قديمه للتجربه", owner_id=admin.id if admin else None,
                      last_note="ملاحظة قديمة")
        s.add(old)
        s.flush()
        s.add(Contact(company_id=old.id, kind="phone", value="+966551110000", source_url="x"))
        old_id = old.id
    snap = xlsx([["اسم العميل", "رقم الجوال", "البريد الإلكتروني", "المدينة", "ملاحظات", "اسم الحساب"],
                 ["مؤسسة النقل المشتركة", "+966501234567", "info@shared-test.sa", "الرياض", "مهتم", "@shared"],
                 ["متجر سناب فقط", "0559876543 / 0114448899", "", "الرياض", "", "@snaponly"],
                 ["عميل بلا رقم", "", "", "الرياض", "", ""]])
    goog = xlsx([["الاسم", "الهاتف", "النشاط"],
                 ["مؤسسة النقل المشتركة", "0501234567", "نقل"],
                 ["شركة قديمة للتجربة", "0551110000", "توزيع"],
                 ["مصنع جوجل فقط", "0533334444", "صناعة"]])
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        r = cl.post(f"/targets/{cid}", data={"name": "حملة تجريبية سوشال", "target_place": "الرياض", "social_present": "1",
                                             "social_sources": ["snapchat", "google"], "cat_count": "0"})
        assert r.status_code in (200, 303)
        html = cl.get(f"/targets/{cid}").text
        assert "مواقع التواصل الاجتماعي" in html and "المكان المستهدف" in html
        for lbl in ("سناب شات", "تيك توك", "الموقع الإلكتروني لسيارة اب", "فيس بوك", "إنستغرام", "إكس", "شات جي بي تي",
                    "جوجل", "لينكدإن"):
            assert lbl in html
        assert 'id="src-snapchat" data-src="snapchat" >' in html or 'id="src-snapchat" data-src="snapchat"  >' in html
        assert 'id="src-tiktok" data-src="tiktok" hidden' in html  # المصادر غير المختارة مخفية
        # رفع ← معاينة فورية تحت سناب شات
        r = cl.post(f"/targets/{cid}/social/upload", data={"source": "snapchat", "target_place": "الرياض"},
                    files={"file": ("snap.xlsx", snap, "application/octet-stream")}, follow_redirects=True)
        assert "snap.xlsx" in r.text and "3 عميل" in r.text and "عرض العملاء" in r.text
        sec = r.text.split('id="src-snapchat"')[1].split('id="src-tiktok"')[0]
        assert "مؤسسة النقل المشتركة" in sec and "+966501234567" in sec and "@snaponly" in sec
        assert '<div class="ph" translate="no" dir="ltr">0559876543</div><div class="ph" translate="no" dir="ltr">0114448899</div>' in sec
        hdr = sec.split("<th>")
        assert hdr[2].startswith("اسم العميل / الشركة") and hdr[3].startswith("رقم التواصل")
        cl.post(f"/targets/{cid}/social/upload", data={"source": "google"},
                files={"file": ("google.xlsx", goog, "application/octet-stream")})
        with session_scope() as s:
            assert s.query(CampaignLead).count() == 0  # المعاينة لا تستورد
            assert s.query(CampaignSourceFile).filter_by(campaign_id=cid, status="staged").count() == 2
        # بعد «إغلاق» الحملة وفتحها من جديد يبقى الملف وجدوله
        html = cl.get(f"/targets/{cid}").text
        assert "snap.xlsx" in html and "google.xlsx" in html and "متجر سناب فقط" in html
        assert "اعتماد واستيراد العملاء" in html
        r = cl.post(f"/targets/{cid}/social/import", follow_redirects=True)
        assert "2 جديد، 0 مكرر، 1 فشل" in r.text and "1 جديد، 2 مكرر، 0 فشل" in r.text
        assert "رقم التواصل غير صالح أو فارغ" in r.text or "لا يوجد رقم تواصل ولا بريد" in r.text
        with session_scope() as s:
            shared = s.query(Company).filter(Company.name == "مؤسسة النقل المشتركة").all()
            assert len(shared) == 1
            srcs = {l.source_key for l in s.query(CampaignLead).filter_by(company_id=shared[0].id)}
            assert srcs == {"snapchat", "google"}
            assert {l.place for l in s.query(CampaignLead).filter_by(campaign_id=cid)} == {"الرياض"}
            o = s.get(Company, old_id)
            assert o.last_note == "ملاحظة قديمة" and (o.owner_id is None or o.owner_id == (admin.id if admin else None))
            assert all(f.place == "الرياض" for f in s.query(CampaignSourceFile).filter_by(campaign_id=cid))
        for src in ("snapchat", "google"):
            t = cl.get(f"/targets/{cid}?src={src}").text.split('id="leads"')[1]
            assert t.count("مؤسسة النقل المشتركة") == 1
        t = cl.get(f"/targets/{cid}?src=snapchat").text.split('id="leads"')[1]
        assert "متجر سناب فقط" in t and "مصنع جوجل فقط" not in t
        t = cl.get(f"/targets/{cid}").text.split('id="leads"')[1]
        assert t.count("مؤسسة النقل المشتركة") == 1 and "سناب شات" in t and "جوجل" in t
        # صفحة الملف الكامل داخل المنصة
        with session_scope() as s:
            fid = s.query(CampaignSourceFile).filter_by(campaign_id=cid, source_key="snapchat").first().id
        assert "متجر سناب فقط" in cl.get(f"/targets/{cid}/social/{fid}").text


def test_social_upload_requires_manager():
    init_db()
    with session_scope() as s:
        camp = Campaign(name="حملة صلاحيات", status="draft")
        s.add(camp)
        s.flush()
        cid = camp.id
    with TestClient(app) as cl:
        r = cl.post(f"/targets/{cid}/social/upload", data={"source": "snapchat"},
                    files={"file": ("a.xlsx", xlsx([["الاسم", "الجوال"], ["س", "0501112222"]]), "application/octet-stream")},
                    follow_redirects=False)
        assert r.status_code in (303, 401, 403)
    with session_scope() as s:
        assert s.query(CampaignSourceFile).filter_by(campaign_id=cid).count() == 0
