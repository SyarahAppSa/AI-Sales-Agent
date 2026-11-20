"""«العملاء المحتملون»: جمع من الدليل، تحقق من موقع المنشأة، منع التكرار، قابلية التواصل، الأولوية بدليل منشور."""
import io
import json

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from helpers import otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import Company, ExistingCustomer, ProspectRun
from mandoob.discovery.base import FoundContact
from mandoob.discovery.crawler import PageExtract, SiteResult
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
BP = "https://bluepages.com.sa"
CAT1 = BP + "/%D8%A7%D9%84%D8%B1%D9%8A%D8%A7%D8%B6/%D8%AE%D8%AF%D9%85%D8%A7%D8%AA-%D9%84%D9%88%D8%AC%D8%B3%D8%AA%D9%8A%D8%A9"
CAT2 = BP + "/%D8%AC%D8%AF%D8%A9/%D9%85%D8%AF%D8%A7%D8%B1%D8%B3"


def card(i, name, city, cat, **k):
    return {"id": f"x{i}", "nameAr": name, "nameEn": None, "website": k.get("website"), "email": k.get("email"),
            "standardPhone": k.get("phone"), "hotline": None, "whatsapp": None, "phones": [], "verified": True,
            "descriptionAr": k.get("desc", ""), "city": {"nameAr": city}, "categories": [{"nameAr": cat}]}


def page(items):
    payload = json.dumps('x:{"activityName":"z","initialData":{"cards":{"items":' + json.dumps(items, ensure_ascii=False)
                         + ',"nextCursor":null}}}', ensure_ascii=False)
    return f'<html><script>self.__next_f.push([1,{payload}])</script></html>'


PAGES = {
    BP + "/sitemap.xml": f"<urlset><url><loc>{BP}/</loc></url><url><loc>{CAT1}</loc></url><url><loc>{CAT2}</loc></url>"
                         f"<url><loc>{CAT1}/company-a1</loc></url></urlset>",
    CAT1: page([card(1, "شركة الأسطول السريع للنقل", "الرياض", "خدمات لوجستية", website="https://fleet-fast.example",
                     phone="0117390251"),
                card(2, "مؤسسة بلا تواصل", "الرياض", "خدمات لوجستية"),
                card(3, "عميلنا الحالي للنقل", "الرياض", "خدمات لوجستية", phone="0117264093"),
                card(4, "مكتب استشارات", "الرياض", "استشارات ادارية", email="info@gmail.com", phone="0538027614",
                     desc="مكتب استشارات إدارية للشركات")]),
    CAT2: page([card(5, "شركة الأسطول السريع للنقل", "الرياض", "خدمات لوجستية", website="https://fleet-fast.example",
                     phone="0117390251"),
                card(6, "مدارس النخبة الأهلية", "جدة", "مدارس", phone="0126390472", email="info@nukhba-schools.example",
                     website="https://nukhba-schools.example", desc="نوفر خدمة النقل المدرسي بحافلات حديثة")]),
}


class FakeResp:
    def __init__(self, t):
        self.text = t

    def raise_for_status(self):
        pass


class FakeClient:
    def get(self, url, **k):
        return FakeResp(PAGES[url])


class FakeCrawler:
    def crawl(self, url):
        if "fleet-fast" in url:
            return SiteResult(website=url, contacts=[
                FoundContact("email", "sales@fleet-fast.example", "https://fleet-fast.example/contact"),
                FoundContact("phone", "0117390251", "https://fleet-fast.example/contact")],
                pages=[PageExtract(url="https://fleet-fast.example/about",
                                   text="نشغّل أسطولًا من 120 شاحنة مبردة لتوزيع المنتجات في المملكة. الرقم الوطني الموحد 7001234567")])
        return SiteResult(website=url)


def setup_module():
    init_db()
    with session_scope() as s:
        s.add(ExistingCustomer(name="عميلنا الحالي للنقل", name_norm="عميلنا الحالي للنقل", phone="+966117264093"))


def test_pilot_batch_counts_contactable_dedupes_and_ranks(monkeypatch):
    from mandoob import prospects as P
    monkeypatch.setattr(P, "DELAY", 0)
    with session_scope() as s:
        rid = P.request_run(s, None, target=10).id
    P.run(rid, crawler=FakeCrawler(), client=FakeClient())
    with session_scope() as s:
        r = s.get(ProspectRun, rid)
        st = r.stats["bluepages"]
        assert r.status == "done" and st["examined"] == 6
        assert st["ready"] == 3 and st["needs"] == 1 and st["duplicate"] == 1 and st["existing_customer"] == 1
        c = s.query(Company).filter(Company.name == "شركة الأسطول السريع للنقل").one()
        assert c.contactable and c.contact_level == "website" and c.priority_level == "high"
        assert "أسطول" in c.priority_reason and c.priority_url == "https://fleet-fast.example/about"
        assert c.vehicles_estimate == 120 and c.unified_number == "7001234567"
        assert c.primary("email").value == "sales@fleet-fast.example"
        n = s.query(Company).filter(Company.name == "مؤسسة بلا تواصل").one()
        assert not n.contactable and n.priority_reason
        sch = s.query(Company).filter(Company.name == "مدارس النخبة الأهلية").one()
        assert sch.contact_level == "directory" and "حافلات" in sch.priority_reason
        cons = s.query(Company).filter(Company.name == "مكتب استشارات").one()
        assert cons.contactable and cons.priority_level == "low" and "أولوية حسب القطاع" in cons.priority_reason
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        html = cl.get("/prospects").text
        assert "العملاء المحتملون" in html and "شركة الأسطول السريع للنقل" in html and "مؤسسة بلا تواصل" not in html
        assert html.index("شركة الأسطول السريع للنقل") < html.index("مكتب استشارات")           # الأعلى أولوية أولًا
        assert "تحتاج استكمال" in html and "مؤسسة بلا تواصل" in cl.get("/prospects?state=needs").text
        assert "مدارس النخبة" not in cl.get("/prospects?level=high&city=الرياض").text
        rows = list(load_workbook(io.BytesIO(cl.get("/prospects.xlsx").content)).active.iter_rows(values_only=True))
        assert any("شركة الأسطول السريع للنقل" in [str(x) for x in r] for r in rows)
    # يوم آخر: المنشأة نفسها لا تعود جديدة
    with session_scope() as s:
        rid2 = P.request_run(s, None, target=10).id
    monkeypatch.setattr(P, "riyadh_today", lambda: __import__("datetime").date(2030, 1, 2))
    P.run(rid2, crawler=FakeCrawler(), client=FakeClient())
    with session_scope() as s:
        st = s.get(ProspectRun, rid2).stats["bluepages"]
        assert st.get("ready", 0) == 0 and st["duplicate"] >= 4


def test_earabicmarket_listing_parse_and_pagination():
    from mandoob import prospects as P
    html = ('<span id="dlCompanies_lblCompanyName_0">شركة التقنية للأنظمة</span><span id="dlCompanies_lblCity_0">الرياض</span>'
            '<span id="dlCompanies_lblAddress_0">ص.ب. 5285 - الرياض 11422</span><span id="dlCompanies_lblTelephone_0">٠٠٩٦٦٠١١٤١٢٢٢١١</span>'
            '<a id="dlCompanies_lnkWebSite_0" class="website" href="http://www.techgroup-sa.example" target="_blank">x</a>'
            '<span id="lblPromotionCompanyName">شركتك هنا</span>'
            '<a href="https://www.earabicmarket.com/ar/companies/saudi-arabia/all/search?page=2&amp;qry=abc">2</a>')
    items, nxt = P.parse_eam(html, "https://www.earabicmarket.com/ar/companies/saudi-arabia/all")
    assert len(items) == 1 and items[0]["name"] == "شركة التقنية للأنظمة" and items[0]["city"] == "الرياض"
    assert items[0]["website"] == "http://www.techgroup-sa.example" and items[0]["phones"]
    assert nxt == ["https://www.earabicmarket.com/ar/companies/saudi-arabia/all/search?page=2&qry=abc"]


def test_licensed_file_import_and_google_discovery_and_daily_schedule(monkeypatch):
    """ملف مزوّد (عناوين عربية) ← طابور ← معالجة؛ خرائط Google للاكتشاف فقط (التواصل من الموقع)؛ والجدولة اليومية."""
    from mandoob import config, prospects as P
    from mandoob.models import ProspectPool
    monkeypatch.setattr(P, "DELAY", 0)
    csv_text = ("الاسم,النشاط,المدينة,الموقع,الهاتف,البريد,الرقم الموحد\n"
                "مصنع الملف المرخص للتغليف,مصانع تغليف,الدمام,,0138201746,info@licfile-pack.example,7009988776\n"
                "اسم بلا تواصل من الملف,تجارة,الرياض,,,,\n")
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        r = cl.post("/prospects/import", data={},
                    files={"file": ("lic.csv", csv_text.encode("utf-8"), "text/csv")})
        assert r.status_code in (200, 303)
    with session_scope() as s:
        assert s.query(ProspectPool).filter(ProspectPool.status == "queued").count() == 2

    class G:
        def search(self, phrase, city, state):
            from mandoob.discovery.base import Candidate
            yield Candidate(name="شركة الخرائط للتوزيع", source_type="google_places", source_url="https://maps/x",
                            external_id="gp1", city=city, website="https://fleet-fast.example", activity="توزيع")
    monkeypatch.setattr(config.settings, "google_places_api_key", "k")
    monkeypatch.setattr("mandoob.discovery.sources.GooglePlacesSource", lambda *a, **k: G())
    monkeypatch.setattr(P, "google_pairs", lambda day: [("توزيع", "الرياض")])
    with session_scope() as s:
        rid = P.request_run(s, None, target=50, sources="import,google_places").id
    P.run(rid, crawler=FakeCrawler(), client=FakeClient())
    with session_scope() as s:
        st = s.get(ProspectRun, rid).stats
        assert st["import"]["ready"] == 1 and st["import"]["needs"] == 1
        c = s.query(Company).filter(Company.name == "مصنع الملف المرخص للتغليف").one()
        assert c.unified_number == "7009988776" and c.contact_level == "provider" and c.contactable
        assert s.query(ProspectPool).filter(ProspectPool.status == "queued").count() == 0
        # موقع شركة الخرائط هو نفسه موقع منشأة موجودة ← مكررة، ولا يُخزَّن شيء من بيانات الخرائط
        assert st["google_places"]["duplicate"] == 1
    # الجدولة اليومية: مرة واحدة في اليوم بعد الموعد
    with session_scope() as s:
        P.set_auto(s, enabled=True, time="00:00")
    monkeypatch.setattr(P, "riyadh_today", lambda: __import__("datetime").date(2031, 5, 5))
    assert P.schedule_daily() is True and P.schedule_daily() is False
    with session_scope() as s:
        r = s.scalar(__import__("sqlalchemy").select(ProspectRun).order_by(ProspectRun.id.desc()))
        assert r.target == 1000 and "google_places" in r.sources
        r.status = "done"
        P.set_auto(s, enabled=False)


def test_random_leads_upload_pdf_and_move_to_verification(monkeypatch):
    """«عملاء عشوائيون»: قائمة PDF تُحفظ منفصلة دون تحقق، ثم «نقل للتحقق» يدخلها مسار العملاء المحتملين."""
    import pypdf
    from mandoob import prospects as P
    from mandoob.models import ProspectPool

    class Page:
        def extract_text(self):
            return ("قائمة الشركات العقارية\nشركة الأفق العقارية للتجربة 0138437261 info@ufuq-test.example\n"
                    "مؤسسة الواحة للعقار\nهاتف: ٠٥٤٧٢١٩٣٦٨\nسطر بلا بيانات")

    class Reader:
        def __init__(self, *_):
            self.pages = [Page()]
    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    monkeypatch.setattr(P, "DELAY", 0)
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        cl.post("/random-leads/upload", data={"source_note": "ملف 630 شركة عقارية — تجربة"},
                files={"file": ("list.pdf", b"%PDF-fake", "application/pdf")})
        page = cl.get("/random-leads").text
        assert "شركة الأفق العقارية للتجربة" in page and "مؤسسة الواحة للعقار" in page and "+966" not in page.split("telcol")[0][-50:]
        assert "0138437261" in page and "0547219368" in page
        with session_scope() as s:
            assert s.query(ProspectPool).filter(ProspectPool.kind == "random", ProspectPool.status == "random").count() == 2
            assert not s.query(Company).filter(Company.name == "مؤسسة الواحة للعقار").count()   # لا تدخل قبل التحقق
        cl.post("/random-leads/verify", data={"all": "1", "q": ""})
    with session_scope() as s:
        rid = P.request_run(s, None, target=50, sources="import").id
    P.run(rid, crawler=FakeCrawler(), client=FakeClient())
    with session_scope() as s:
        c = s.query(Company).filter(Company.name == "مؤسسة الواحة للعقار").one()
        assert c.contactable and c.primary("phone").value == "+966547219368" and c.prospect_on
        assert s.query(ProspectPool).filter(ProspectPool.kind == "random", ProspectPool.status == "done").count() == 2


def test_upload_parser_robust_formats():
    """عناوين في صف لاحق، ملف بلا عناوين، CSV بترميز ويندوز العربي وفاصلة منقوطة، وملفات غير مدعومة برسالة واضحة."""
    import io
    import pytest
    from openpyxl import Workbook
    from mandoob import prospects as P
    wb = Workbook()
    ws = wb.active
    ws.append(["قائمة شركات المقاولات في الدمام"])
    ws.append([])
    ws.append(["م", "اسم الشركة", "رقم الجوال", "المدينة"])
    ws.append([1, "شركة البناء المتين", "0551234987", "الدمام"])
    ws2 = wb.create_sheet("ورقة2")
    ws2.append(["مؤسسة الخبر للتجارة", "0138887766", "info@khobar-trade.example"])
    ws2.append(["مؤسسة الظهران للنقل", "0503332211", ""])
    buf = io.BytesIO()
    wb.save(buf)
    rows, m = P.parse_upload("list.xlsx", buf.getvalue())
    names = {r["name"] for r in rows}
    assert {"شركة البناء المتين", "مؤسسة الخبر للتجارة", "مؤسسة الظهران للنقل"} <= names
    assert next(r for r in rows if r["name"] == "شركة البناء المتين")["phone"] == "0551234987"
    csv_bytes = "الاسم;الهاتف\nمحل الأمانة;0114443322\n".encode("cp1256")
    rows, _ = P.parse_upload("x.csv", csv_bytes)
    assert rows and rows[0]["name"] == "محل الأمانة" and rows[0]["phone"] == "0114443322"
    with pytest.raises(P.UploadError):
        P.parse_upload("list.numbers", b"xxxx")


def test_admin_list_upload_shows_excel_as_is():
    import io
    from openpyxl import Workbook
    from mandoob.models import AdminList, ProspectPool
    wb = Workbook()
    ws = wb.active
    ws.title = "المؤسسات"
    ws.append(["قائمة المؤسسات في الجبيل"])
    ws.append(["اسم المؤسسة", "النشاط", "رقم الجوال", "ملاحظات الإدارة"])
    ws.append(["مؤسسة الجبيل الإدارية للتجربة", "مقاولات", "0559871234", "عميل مهم"])
    ws.append(["مؤسسة الساحل الإدارية للتجربة", "نقل", "0137771122", ""])
    buf = io.BytesIO()
    wb.save(buf)
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        r = cl.post("/admin-lists/upload", files={"file": ("جبيل.xlsx", buf.getvalue(), "application/vnd.ms-excel")},
                    follow_redirects=True)
        html = r.text
        assert "قائمة عملاء إدارية" in html and "مؤسسة الجبيل الإدارية للتجربة" in html and "ملاحظات الإدارة" in html
        assert "عميل مهم" in html and "0559871234" in html and "(2 صف)" in html
        assert "مؤسسة الساحل" not in cl.get(f"/admin-lists?q=الجبيل").text.split("<table")[1]
        with session_scope() as s:
            lid = s.query(AdminList).order_by(AdminList.id.desc()).first().id
            before = s.query(ProspectPool).count()
        rows = list(load_workbook(io.BytesIO(cl.get(f"/admin-lists/{lid}.xlsx").content)).active.iter_rows(values_only=True))
        assert any("مؤسسة الجبيل الإدارية للتجربة" in [str(x) for x in r_] for r_ in rows)
        cl.post(f"/admin-lists/{lid}/verify")
        with session_scope() as s:
            assert s.query(ProspectPool).count() == before + 2
            assert s.get(AdminList, lid) is not None          # القائمة الأصلية باقية
