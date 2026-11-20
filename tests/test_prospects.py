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
