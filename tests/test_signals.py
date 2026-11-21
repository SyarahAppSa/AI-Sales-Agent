"""«اكتشاف من خرائط جوجل»: مسارات فرص البيع بأدلة حرفية مؤرخة، والمرحلة الثانية من الموقع الرسمي، ودمج الأدلة في
سجل واحد، و«قيد التحقق»، ومنع التكرار، وما يراه المندوب قبل الإسناد؛ والحاسبة العامة؛ وعيّنة البيانات المشتراة؛
والتقرير الأسبوعي حسب المصدر."""
import io
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from fakes import FakeCrawler, add_site
from helpers import otp_login
from mandoob import ai
from mandoob.config import settings
from mandoob.costs import record_cost
from mandoob.db import init_db, session_scope
from mandoob.models import (Company, CompanySignal, Contact, DataPurchase, DirectoryRun, InboundLead, LeadStub, User)
from mandoob.normalize import normalize_name
from mandoob.timeutil import riyadh_today
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
JOB = "https://jobs-sig.example/ad-501"
NEWS = "https://news-sig.example/fleet"
TODAY = riyadh_today().isoformat()
OLD = (riyadh_today() - timedelta(days=400)).isoformat()
PAGE_JOB = ("مطلوب مشرف حركة وسائقي توزيع لدى شركة الواحة لتوزيع الأغذية بجدة — التقديم عبر موقع الشركة. "
            "كما تعلن مؤسسة النخيل القديمة لدينا عن وظيفة مدير أسطول.")
PAGE_NEWS = "تسليم دفعة من 25 شاحنة مبردة إلى شركة الواحة لتوزيع الأغذية في جدة ضمن توسعة أسطولها."


def fake_agent(system, prompt, schema, *, note="agent", **kw):
    record_cost("claude", 1, 0.30, note=note)
    if note.endswith(":harvest"):
        items = [
            {"name": "شركة الواحة لتوزيع الأغذية", "activity": "توزيع أغذية", "city": "جدة",
             "website": "https://waha-food-sig.example", "evidence_url": JOB, "evidence_date": TODAY,
             "evidence_quote": "مطلوب مشرف حركة وسائقي توزيع لدى شركة الواحة لتوزيع الأغذية",
             "reason": "شركة توزيع أغذية في جدة أعلنت هذا الأسبوع عن توظيف مشرف حركة وسائقي توزيع",
             "vehicles": "", "contact_dept": "إدارة النقل والتوزيع", "employer_hidden": False},
            {"name": "مؤسسة النخيل القديمة لدينا", "activity": "نقل", "city": "جدة", "website": "",
             "evidence_url": JOB, "evidence_date": TODAY, "evidence_quote": "كما تعلن مؤسسة النخيل القديمة لدينا عن وظيفة مدير أسطول",
             "reason": "توظف مدير أسطول", "vehicles": "", "contact_dept": "التشغيل", "employer_hidden": False},
            {"name": "شركة مختلقة بلا دليل", "activity": "", "city": "جدة", "website": "", "evidence_url": JOB,
             "evidence_date": TODAY, "evidence_quote": "نص غير موجود في أي صفحة قرأها الوكيل إطلاقًا",
             "reason": "", "vehicles": "", "contact_dept": "", "employer_hidden": False},
            {"name": "جهة غير معلنة", "activity": "توزيع", "city": "جدة", "website": "", "evidence_url": JOB,
             "evidence_date": TODAY, "evidence_quote": "مطلوب مشرف حركة وسائقي توزيع لدى شركة الواحة",
             "reason": "مكتب توظيف أخفى الاسم", "vehicles": "", "contact_dept": "", "employer_hidden": True},
            {"name": "شركة الواحة لتوزيع الأغذية", "activity": "توزيع أغذية", "city": "جدة", "website": "",
             "evidence_url": NEWS, "evidence_date": OLD, "evidence_quote": "تسليم دفعة من 25 شاحنة مبردة إلى شركة الواحة",
             "reason": "خبر قديم", "vehicles": "25", "contact_dept": "", "employer_hidden": False},
        ]
        blocks = [NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url=u, content=NS(
            type="document", source=NS(type="text", data=d)))) for u, d in ((JOB, PAGE_JOB), (NEWS, PAGE_NEWS))]
        return {"items": items}, blocks, 1
    return {"items": [], "prospects": []}, [], 0


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ai, "ai_agent", fake_agent)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "voice_calls_enabled", False)
    monkeypatch.setattr(settings, "auto_outreach_enabled", False)


def setup_module():
    init_db()
    add_site("waha-food-sig.example", "شركة الواحة لتوزيع الأغذية", "توزيع أغذية مبردة بأسطول في جدة",
             phone="0126651100", email="sales@waha-food-sig.example")
    with session_scope() as s:
        s.add(Company(name="مؤسسة النخيل القديمة لدينا", name_norm=normalize_name("مؤسسة النخيل القديمة لدينا"),
                      activity="نقل", city="جدة", data_status="complete", last_note="اتصلت الأسبوع الماضي — طلب عرضًا",
                      contact_status="call_later"))


def test_signal_tracks_evidence_dedupe_and_rep_view():
    from mandoob import directory as D
    with session_scope() as s:
        r = D.request(s, sources=["sig_hiring"], params={"activities": ["trade.distributors"],
                                                         "scope": {"places": ["SA-02/jeddah"]}, "stage1_limit": 10},
                      budget=5, user_id=None)
        rid = r.id
    D.run(rid, crawler=FakeCrawler())
    with session_scope() as s:
        st = s.get(DirectoryRun, rid).stats["sig_hiring"]
        assert st["rejected_name"] >= 2 and st["rejected_name"] % 2 == 0  # في كل نداء: اقتباس غير موجود + دليل قديم
        assert st["pending_verify"] == 1 and st["duplicate"] >= 1 and st["complete"] == 1
        waha = s.query(Company).filter(Company.name == "شركة الواحة لتوزيع الأغذية").one()
        assert waha.data_status == "complete" and waha.lead_source == "directory:sig_hiring"
        assert "مشرف حركة" in waha.fit_reason and waha.fit_evidence_url == JOB and waha.decision_unit == "إدارة النقل والتوزيع"
        cts = {c.kind: c for c in s.query(Contact).filter_by(company_id=waha.id)}
        assert cts["phone"].value == "+966126651100" and cts["phone"].source_url.startswith("https://waha-food-sig.example")
        assert cts["email"].value == "sales@waha-food-sig.example"
        sig = s.query(CompanySignal).filter_by(company_id=waha.id).one()
        assert sig.kind == "hiring_fleet" and sig.url == JOB and sig.announced_on == riyadh_today()
        old = s.query(Company).filter(Company.name == "مؤسسة النخيل القديمة لدينا").one()
        assert s.query(CompanySignal).filter_by(company_id=old.id).count() == 1  # الدليل أُضيف لسجلها نفسه
        assert s.query(LeadStub).filter_by(run_id=rid, status="pending_verify").one().name == "جهة غير معلنة"
        old_id, waha_id = old.id, waha.id
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        assert 'href="/signals"' in cl.get("/").text
        page = cl.get(f"/signals?run={rid}").text
        assert "اكتشاف من خرائط جوجل" in page and "شركة الواحة لتوزيع الأغذية" in page
        assert "أعلنت هذا الأسبوع عن توظيف مشرف حركة" in page and JOB in page and "إدارة النقل والتوزيع" in page
        assert "+966126651100" in page and "sales@waha-food-sig.example" in page and "غير معلوم" in page
        assert "قيد التحقق" in page and "مكررة — أُضيف الدليل لسجلها" in page
        assert "اتصلت الأسبوع الماضي" in page and "طلب التواصل لاحقًا" in page and "غير مسند" in page  # قبل الإسناد
        assert "google.com/maps/search/?api=1" in page and "تكلفة المنشأة المكتملة" in page
        # التشغيل من اللوحة
        r = cl.post("/signals/run", data={"tracks": ["sig_fleet", "sig_hiring"], "activities": ["__all__"],
                                          "scope_mode": "regions", "regions": ["SA-04"], "limit": "6", "budget": "2"},
                    follow_redirects=False)
        assert r.status_code == 303
    with session_scope() as s:
        nr = s.query(DirectoryRun).order_by(DirectoryRun.id.desc()).first()
        assert nr.sources == ["sig_fleet", "sig_hiring"] and nr.params["scope"] == {"regions": ["SA-04"]}
        nr.status = "done"
    # إعادة الدفعة نفسها: لا تكرار
    with session_scope() as s:
        r2 = D.request(s, sources=["sig_hiring"], params={"activities": ["trade.distributors"],
                                                          "scope": {"places": ["SA-02/jeddah"]}, "stage1_limit": 10},
                       budget=5, user_id=None)
        rid2 = r2.id
    D.run(rid2, crawler=FakeCrawler())
    with session_scope() as s:
        st2 = s.get(DirectoryRun, rid2).stats["sig_hiring"]
        assert st2.get("new", 0) == 0 and st2.get("complete", 0) == 0
        assert s.query(Company).filter(Company.name == "شركة الواحة لتوزيع الأغذية").count() == 1


def test_fuel_calculator_public_and_test_request_excluded():
    from mandoob import goal
    with TestClient(app) as anon:
        page = anon.get("/fleet-calculator").text
        assert "حاسبة تكلفة وقود الأسطول" in page and "أرغب في مراجعة تكلفة أسطولي" in page
        anon.post("/fleet-calculator", data={"vehicles": "40", "spend": "2000", "company_name": "شركة حاسبة حقيقية للنقل",
                                             "phone": "0551239900", "consent": "1", "src": "syarah-site"})
    with session_scope() as s:
        lead = s.query(InboundLead).filter_by(company_name="شركة حاسبة حقيقية للنقل").one()
        assert not lead.is_test and lead.vehicles == 40 and lead.source == "fuel_calculator:syarah-site"
        assert "80,000 ر.س شهريًا" in lead.notes and "960,000" in lead.notes
        base = goal.achieved(s)
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        assert "سيُسجَّل «تجريبيًا»" in adm.get("/fleet-calculator").text
        adm.post("/fleet-calculator", data={"vehicles": "5", "spend": "1000", "company_name": "طلب تجريبي للحاسبة",
                                            "phone": "0551239901", "consent": "1"})
        leads = adm.get("/leads").text
        assert "حاسبة تكلفة وقود الأسطول" in leads and "تجريبي — مستبعد من الأرقام" in leads
    with session_scope() as s:
        t = s.query(InboundLead).filter_by(company_name="طلب تجريبي للحاسبة").one()
        assert t.is_test and s.get(Company, t.company_id).is_test
        assert goal.achieved(s) == base


def test_data_purchase_sample_and_weekly_by_source():
    csv = ("الاسم,المدينة,الهاتف,البريد\n" + "\n".join(f"منشأة عيّنة {i},الرياض,05577{i:05d},s{i}@sample-sig.sa" for i in range(5))
           + "\nمنشأة عيّنة 1,الرياض,0557700001,s1@sample-sig.sa\n")
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        r = adm.post("/prospects/import", data={"provider": "مزوّد تجريبي", "data_updated_on": "2026-11-01",
                                                "sample_cost": "25"},
                     files={"file": ("sample.csv", csv.encode("utf-8"), "text/csv")}, follow_redirects=True)
        assert "عيّنة «مزوّد تجريبي»: 6 سجلًا — 5 في طابور المعالجة و1 مكرر" in r.text
        page = adm.get("/prospects").text
        assert "عيّنات البيانات المشتراة" in page and "مزوّد تجريبي" in page and "غير متاحة" in page
        wk = adm.get("/weekly-report").text
        assert "حسب مصدر العملاء" in wk and "غير متاحة" in wk
    with session_scope() as s:
        dp = s.query(DataPurchase).filter_by(provider="مزوّد تجريبي").one()
        assert dp.cost_usd == 25 and dp.rows == 6 and dp.queued == 5 and str(dp.data_updated_on) == "2026-11-01"


SCHOOL_PAGE = ("مدارس الأفق الأهلية بالرياض: يتولى النقل المدرسي لطلابنا شركة المسار الآمن للنقل المدرسي بموجب عقد. "
               "شركة المسار الآمن للنقل المدرسي تشغّل حافلات مدرسية في الرياض.")
SCHOOL_URL = "https://ofuq-schools-sig.example/transport"


def fake_direct_agent(system, prompt, schema, *, note="agent", **kw):
    record_cost("claude", 1, 0.20, note=note)
    if note == "dir:sig_direct:harvest" and "النقل المدرسي" in prompt or "حافلات المدارس" in prompt:
        items = [{"name": "شركة المسار الآمن للنقل المدرسي", "activity": "نقل مدرسي", "city": "الرياض",
                  "website": "https://masar-amin-sig.example", "found_url": SCHOOL_URL,
                  "quote": "يتولى النقل المدرسي لطلابنا شركة المسار الآمن للنقل المدرسي بموجب عقد",
                  "operator_role": "operator", "linked_entity": "مدارس الأفق الأهلية",
                  "reason": "تشغّل حافلات مدرسية في الرياض ومتعاقدة مع مدارس الأفق الأهلية"}]
        blocks = [NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url=SCHOOL_URL, content=NS(
            type="document", source=NS(type="text", data=SCHOOL_PAGE))))]
        return {"items": items}, blocks, 1
    return {"items": []}, [], 0


def _ov_row(i, name, tp, site):
    return {"id": f"ovd-{i}", "name": name, "bc": None, "tp": tp, "websites": [site], "phones": None, "emails": None,
            "locality": "الرياض", "os": None, "confidence": 0.9, "lic": "CDLA-Permissive-2.0", "ds": "meta",
            "lat": 24.7, "lon": 46.7, "release": "2026-11-18.0"}


def test_direct_activity_search_operator_and_merge(monkeypatch):
    from mandoob import directory as D, overture as OV
    monkeypatch.setattr(ai, "ai_agent", fake_direct_agent)
    rows = [_ov_row(1, "مؤسسة الرواحل للنقل المدرسي", "school_bus_service", "https://rawahel-sig.example"),
            _ov_row(2, "مدارس النخبة العالمية", "private_school", "https://nukhba-intl-sig.example"),
            _ov_row(3, "مقهى لا علاقة له", "coffee_shop", "https://cafe-sig.example")]
    monkeypatch.setattr(OV, "city_rows", lambda pid: rows if pid == "SA-01/riyadh" else [])
    add_site("rawahel-sig.example", "مؤسسة الرواحل للنقل المدرسي", "نقل طالبات ومعلمات بالحافلات في الرياض",
             phone="0114201001", email="info@rawahel-sig.example")
    add_site("masar-amin-sig.example", "شركة المسار الآمن للنقل المدرسي", "حافلات مدرسية في الرياض",
             phone="0114201002", email="ops@masar-amin-sig.example")
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        page = cl.get("/signals").text
        assert "البحث المباشر عن الشركات والمؤسسات حسب النشاط" in page and 'value="group:school_transport"' in page
        assert "النقل المدرسي والجامعي" in page and "مشغّلو صهاريج ووايتات المياه" in page and 'id="act-filter"' in page
        assert "حالة الربط المباشر بخرائط Google" in page
        r = cl.post("/signals/run", data={"tracks": ["sig_direct"], "activities": ["group:school_transport"],
                                          "scope_mode": "places", "places": ["SA-01/riyadh"], "limit": "20", "budget": "3"},
                    follow_redirects=False)
        assert r.status_code == 303
    with session_scope() as s:
        run = s.query(DirectoryRun).order_by(DirectoryRun.id.desc()).first()
        assert run.sources == ["sig_direct"] and run.params["activities"] == ["group:school_transport"]
        rid = run.id
    D.run(rid, crawler=FakeCrawler())
    with session_scope() as s:
        st = s.get(DirectoryRun, rid).stats["sig_direct"]
        raw = s.query(Company).filter(Company.name == "مؤسسة الرواحل للنقل المدرسي").all()
        assert len(raw) == 1 and raw[0].data_status == "complete"  # ظهرت في عدة أنشطة من المجموعة ← سجل واحد
        assert "المنشأة نفسها مشغّل نقل" in (raw[0].operator_evidence or "") and raw[0].operator_status == "self"
        assert st["duplicate"] >= 1  # نفس المنشأة في نشاط آخر من المجموعة دُمجت
        masar = s.query(Company).filter(Company.name == "شركة المسار الآمن للنقل المدرسي").one()
        assert "لصالح مدارس الأفق الأهلية" in masar.operator_evidence and masar.data_status == "complete"
        school = s.query(LeadStub).filter(LeadStub.run_id == rid, LeadStub.name == "مدارس النخبة العالمية").first()
        assert school and "مشغّل المركبات غير مؤكد" in school.operator_note
        assert not s.query(LeadStub).filter(LeadStub.run_id == rid, LeadStub.name == "مقهى لا علاقة له").count()
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        page = cl.get(f"/signals?run={rid}").text
        assert "مؤسسة الرواحل للنقل المدرسي" in page and "+966114201001" in page and "مشغّل المركبات غير مؤكد" in page
        assert "explore.overturemaps.org" in page and "تطابق" in page
