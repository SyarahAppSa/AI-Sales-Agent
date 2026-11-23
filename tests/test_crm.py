"""«عملاء CRM»: ربط للقراءة فقط بواجهة ABP (crmapi.syarahapp.sa) أو استيراد ملف — عينة ثم إعادة سحبها بلا تضاعف،
بقاء مندوب المنصة وملاحظاته، ملاحظات الـCRM واسم الموظف، الاستبعاد من عدّاد الألف، والصلاحيات والبحث."""
import io
import json

import httpx
from fastapi.testclient import TestClient
from openpyxl import Workbook

from helpers import otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import Activity, Company, Contact, ExistingCustomer, Setting, User
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
REP = "crm.rep@syarahapp.test"


def _customers(n=45, notes_suffix=""):
    out = []
    for i in range(1, n + 1):
        out.append({"id": f"3a1b-guid-{i:04d}", "code": f"C-{1000 + i}", "companyName": f"شركة الـCRM رقم {i}",
                    "contactName": f"مسؤول {i}", "phoneNumber": f"05{58000000 + i:08d}",
                    "email": f"c{i}@crm-client.sa", "city": {"id": 1, "name": "الرياض"},
                    "status": {"id": 2, "name": "عميل نشط"},
                    "assignedTo": {"name": "سالم مندوب CRM" if i % 2 else "موظف خارج المنصة", "email": REP if i % 2 else "x@y.z"},
                    "notes": f"ملاحظة الـCRM للعميل {i}{notes_suffix}"})
    out.append({"id": "", "companyName": "سجل بلا رقم"})  # يتعذّر استيراده بسبب واضح
    return out


class FakeCRM:
    def __init__(self):
        self.data = _customers()
        self.writes = 0
        self.token_calls = 0

    def handler(self, req: httpx.Request):
        if req.url.path == "/connect/token":
            self.token_calls += 1
            body = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            if body.get("grant_type") != "password" or body.get("client_id") != "CRM_App" or body.get("username") != "crm-reader":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "tok-123", "token_type": "Bearer"})
        if req.headers.get("authorization") != "Bearer tok-123":
            return httpx.Response(401)
        if req.method != "GET":
            self.writes += 1
            return httpx.Response(405)
        if req.url.path == "/api/app/contact":
            skip, n = int(req.url.params["SkipCount"]), int(req.url.params["MaxResultCount"])
            return httpx.Response(200, json={"totalCount": len(self.data), "items": self.data[skip:skip + n]})
        return httpx.Response(404)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def setup_module():
    init_db()
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        adm.post("/staff/add", data={"name": "سالم مندوب CRM", "email": REP, "team": "", "profile": "custom",
                                     "customize": "on", "scope": "own", "see_phone": "on", "see_email": "on",
                                     "update_status": "on", "add_note": "on"})
    with session_scope() as s:
        # عميل موجود مسبقًا في المنصة بنفس جوال عميل الـCRM رقم 4 — مندوبه وملاحظته يجب أن يبقيا
        rep = s.query(User).filter_by(email=REP).one()
        admin = s.query(User).filter_by(email=ADMIN).one()
        c = Company(name="منشأة موجودة قبل الربط", name_norm="pre-crm", owner_id=admin.id, last_note="ملاحظة المندوب القديمة")
        s.add(c)
        s.flush()
        s.add(Contact(company_id=c.id, kind="phone", value="+966558000004", source_url="x"))


def test_crm_settings_secure_sample_resync_full_and_exclusion(monkeypatch):
    from mandoob import crm
    fake = FakeCRM()
    monkeypatch.setattr(crm, "_client", fake.client)
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        page = adm.get("/crm").text
        assert "عملاء CRM" in page and "إعدادات الربط الآمنة" in page and "تحديث من الـCRM" not in page
        adm.post("/crm/settings", data={"username": "crm-reader", "password": "S3cret-Pass!", "entity": "contact", "daily": "on"})
        with session_scope() as s:
            conn = s.get(Setting, "crm_conn").value
            assert conn["username"] == "crm-reader" and "S3cret-Pass!" not in json.dumps(conn)  # مشفّرة
        page = adm.get("/crm").text
        assert "S3cret-Pass!" not in page and "تحديث من الـCRM" in page and "محفوظة مشفّرة" in page
        r = adm.post("/crm/test", follow_redirects=True)
        assert "نجح الاتصال بالـCRM: 46 عميل" in r.text and "companyname" in r.text
        # طلب عينة من الواجهة ثم تنفيذها في العامل
        adm.post("/crm/sync", data={"mode": "sample"})
    assert crm.tick() is True
    with session_scope() as s:
        st = s.get(Setting, "crm_state").value
        assert st["last"]["added"] == 19 and st["last"]["updated"] == 1 and st["last"]["linked"] == 1
        assert s.query(Company).filter(Company.crm_id.isnot(None)).count() == 20
        pre = s.query(Company).filter_by(name="منشأة موجودة قبل الربط").one()
        assert pre.crm_id == "3a1b-guid-0004" and pre.owner_id is not None and pre.last_note == "ملاحظة المندوب القديمة"
        assert pre.crm_notes == "ملاحظة الـCRM للعميل 4" and pre.crm_owner_name == "موظف خارج المنصة"
        c1 = s.query(Company).filter_by(crm_id="3a1b-guid-0001").one()
        rep_id = s.query(User).filter_by(email=REP).one().id
        assert c1.owner_id == rep_id  # الموظف في الـCRM = مستخدم في المنصة (بالبريد)
        assert c1.crm_status == "عميل نشط" and c1.city == "الرياض" and c1.crm_number == "C-1001" and c1.crm_contact_name == "مسؤول 1"
        c1_id = c1.id
    # المندوب يسجّل ملاحظة في المنصة، ثم تُسحب العينة نفسها مرة أخرى (والـCRM غيّر ملاحظته)
    with TestClient(app) as rep:
        otp_login(rep, REP, "portal")
        rep.post(f"/portal/companies/{c1_id}/note", data={"note": "ملاحظة المندوب في المنصة"},
                 headers={"referer": f"http://testserver/portal/companies/{c1_id}"})
    fake.data = _customers(notes_suffix=" — محدّثة")
    crm.sync_api(limit=20, client=fake.client())
    with session_scope() as s:
        st = s.get(Setting, "crm_state").value
        assert st["last"]["added"] == 0 and st["last"]["updated"] == 20
        assert s.query(Company).filter(Company.crm_id.isnot(None)).count() == 20  # لم يتضاعف
        c1 = s.get(Company, c1_id)
        assert c1.last_action == "ملاحظة المندوب في المنصة" and c1.owner_id == rep_id
        assert s.query(Activity).filter(Activity.company_id == c1_id, Activity.kind == "note",
                                        Activity.details == "ملاحظة المندوب في المنصة").count() == 1
        assert c1.crm_notes == "ملاحظة الـCRM للعميل 1 — محدّثة"
        assert s.query(Contact).filter_by(company_id=c1_id, kind="phone").count() == 1
    # السحب الكامل
    st = crm.sync_api(client=fake.client())
    assert st["added"] == 25 and st["updated"] == 20 and st["failed"] == 1
    assert st["failures"][0]["reason"] == "بلا رقم عميل في الـCRM"
    assert fake.writes == 0  # قراءة فقط
    with session_scope() as s:
        assert s.query(Company).filter(Company.crm_id.isnot(None)).count() == 45
        assert s.query(ExistingCustomer).filter(ExistingCustomer.crm_id.isnot(None)).count() == 45 * 3
    # مستبعد من عدّاد العملاء المحتملين الجدد
    from mandoob import prospects as P
    from mandoob.discovery.crawler import SiteResult
    from mandoob.timeutil import riyadh_today

    class Cr:
        def crawl(self, u):
            return SiteResult(website=u)
    rec = {"external_id": "dir:crm-overlap", "name": "اسم مختلف لعميل CRM", "phones": ["0558000030"], "email": None,
           "website": None, "city": "الرياض", "activity": "نقل", "description": "", "listing_url": "https://dir.example/x"}
    assert P.process(rec, Cr(), {}, riyadh_today()) == "existing_customer"
    # العرض والبحث
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        page = adm.get("/crm").text
        assert "45" in page and "شركة الـCRM رقم 1<" in page and "تحديث من الـCRM" in page
        assert "+966558000001" in page and "c1@crm-client.sa" in page and "سالم مندوب CRM" in page
        assert "ملاحظة الـCRM للعميل 1 — محدّثة" in page
        for q in ("شركة الـCRM رقم 7", "0558000007", "C-1007", "3a1b-guid-0007"):
            body = adm.get("/crm", params={"q": q}).text.split("<table>")[1]
            assert "شركة الـCRM رقم 7<" in body and "شركة الـCRM رقم 8<" not in body, q
        card = adm.get(f"/companies/{c1_id}").text
        assert "من الـCRM" in card and "ملاحظة الـCRM للعميل 1 — محدّثة" in card
    with TestClient(app) as rep:
        otp_login(rep, REP, "portal")
        page = rep.get("/portal/crm").text
        assert "عملاء الـCRM المسندون إليك" in page and "شركة الـCRM رقم 1<" in page and "شركة الـCRM رقم 2<" not in page
        assert "إعدادات الربط" not in page and "تحديث من الـCRM" not in page
        assert rep.post("/crm/sync", data={"mode": "full"}, follow_redirects=False).status_code in (303, 403)


def test_crm_file_import_dedupes_by_crm_number():
    wb = Workbook()
    ws = wb.active
    ws.append(["رقم العميل", "اسم الشركة", "مسؤول التواصل", "رقم الجوال", "البريد الإلكتروني", "المدينة", "حالة العميل",
               "الموظف", "الملاحظات"])
    ws.append(["F-1", "شركة من ملف الـCRM", "خالد", "0559100001", "a@file-crm.sa", "جدة", "متابعة", "موظف الملف", "اتصل الأحد"])
    ws.append(["F-2", "شركة ثانية من الملف", "نورة", "0559100002", "", "الدمام", "عميل", "موظف الملف", ""])
    b = io.BytesIO()
    wb.save(b)
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        r = adm.post("/crm/import", files={"file": ("crm-export.xlsx", b.getvalue(), "application/octet-stream")},
                     follow_redirects=True)
        assert "2 مضاف، 0 محدّث" in r.text
        r = adm.post("/crm/import", files={"file": ("crm-export.xlsx", b.getvalue(), "application/octet-stream")},
                     follow_redirects=True)
        assert "0 مضاف، 2 محدّث" in r.text
    with session_scope() as s:
        c = s.query(Company).filter_by(crm_id="F-1").one()
        assert s.query(Company).filter_by(crm_id="F-1").count() == 1
        assert c.crm_notes == "اتصل الأحد" and c.crm_owner_name == "موظف الملف" and c.crm_source == "file"
        assert s.query(Activity).filter(Activity.company_id == c.id, Activity.title == "نُسخ من الـCRM").count() == 1


def test_crm_export_v2_english_headers_and_background_queue(monkeypatch):
    """ملف تصدير الـCRM الجديد (Customer ID / Customer Phone / Contact Person… / Assigned To Email، و«NULL» للفارغ)،
    والملف الكبير يُستورد في الخلفية عبر العامل مع التقدّم."""
    import io
    import openpyxl
    from mandoob import crm
    from mandoob.db import session_scope
    from mandoob.models import Company, Contact
    hdr = ["Customer ID", "Customer Name", "Customer Email", "Customer Phone", "Customer Address", "Number Of Cars",
           "Customer Description", "Contact Person Name", "Contact Person Email", "Contact Person Phone", "City",
           "City (Arabic)", "Region", "Assigned To", "Assigned To Username", "Assigned To Email"]
    rows = [["V2-15546", None, "BAAWAAZIIR@GMAIL.COM", "562780620", None, "29", "NULL", "حسن", "BAAWAAZIIR@GMAIL.COM",
             "NULL", "مكة المكرمة", "NULL", "Western region", "Dheya ", "Dheya", "nobody@x.test"],
            ["V2-15950", "شركة التوصيل السريع للفحص", "finance@expressdelivery-v2.test", "557472556", None, "100", "NULL",
             "احمد", "NULL", "0557472557", "NULL", "الرياض", "NULL", "Dheya", "Dheya", "nobody@x.test"],
            ["V2-16483", None, None, None, None, "0", None, None, None, None, "NULL", "NULL", "NULL", "Dheya", "Dheya", ""]]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(hdr)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    st = crm.import_file("crm-customers_2.xlsx", buf.getvalue())
    assert st["added"] == 2 and st["failed"] == 1  # الصف الفارغ تمامًا فقط
    with session_scope() as s:
        a = s.query(Company).filter_by(crm_id="V2-15546").one()
        assert a.name == "حسن" and "عدد السيارات: 29" in (a.crm_notes or "") and a.city == "مكة المكرمة"
        assert {c.value for c in s.query(Contact).filter_by(company_id=a.id)} == {"+966562780620", "baawaaziir@gmail.com"}
        b = s.query(Company).filter_by(crm_id="V2-15950").one()
        assert b.name == "شركة التوصيل السريع للفحص" and b.city == "الرياض"
        assert {"+966557472556", "+966557472557"} <= {c.value for c in s.query(Contact).filter_by(company_id=b.id)}
    # الملف الكبير: طابور ← العامل
    monkeypatch.setattr(crm, "BIG_FILE", 10)
    ws.append(["V2-17000", "منشأة الخلفية للفحص", "NULL", "0551119871"] + [None] * 12)
    buf2 = io.BytesIO()
    wb.save(buf2)
    crm.queue_file("big.xlsx", buf2.getvalue())
    with session_scope() as s:
        assert crm.get_state(s)["file_queued"] == "big.xlsx"
    assert crm.tick() is True
    with session_scope() as s:
        assert s.query(Company).filter_by(crm_id="V2-17000").count() == 1
        st2 = crm.get_state(s)
        assert not st2.get("running") and not st2.get("file_queued") and st2["file_name"] == "big.xlsx"
