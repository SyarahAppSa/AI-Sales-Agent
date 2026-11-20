"""«اكتشاف عملاء جدد»: مرحلتان — أسماء من الأدلة تُطابق مع قاعدتنا قبل أي تكلفة تفصيلية، ثم الهاتف والبريد من
موقع المنشأة للجديد فقط؛ وميزانية الدفعة؛ وحالة المصادر؛ وتقرير كل مصدر؛ و«شركات مشابهة» و«سبب الترشيح»."""
import io
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from fakes import FakeCrawler, add_site
from helpers import otp_login
from mandoob import ai
from mandoob.config import settings
from mandoob.costs import record_cost
from mandoob.db import init_db, session_scope
from mandoob.models import Company, CompanySignal, Contact, DirectoryRun, ExistingCustomer, LeadStub
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
LISTING = "https://transport-directory.example/riyadh"
PAGE = ("دليل شركات النقل في الرياض: شركة الوادي للشحن البري — نقل بضائع بالشاحنات. "
        "مؤسسة الرمال للنقليات — شاحنات تبريد. شركة القمة للمقاولات العامة. مصنع الواحة للمياه. "
        "شركة قديمة لدينا للنقل.")
CALLS = {"harvest": 0, "complete": 0}


def fake_agent(system, prompt, schema, *, note="agent", **kw):
    record_cost("claude", 1, 0.40, note=note)  # تكلفة النداء تُسجّل كما في الحقيقة
    if note.endswith(":harvest"):
        CALLS["harvest"] += 1
        items = [("شركة الوادي للشحن البري", "https://wadi-freight.example"), ("مؤسسة الرمال للنقليات", ""),
                 ("شركة قديمة لدينا للنقل", ""), ("مصنع الواحة للمياه", "https://waha-water.example"),
                 ("شركة مختلقة لا تظهر في الصفحة", "")]
        out = {"items": [{"name": n, "activity": "نقل", "city": "الرياض", "website": w, "listing_url": LISTING,
                          "vehicle_hint": ""} for n, w in items]}
        blk = NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url=LISTING, content=NS(
            type="document", source=NS(type="text", data=PAGE))))
        return out, [blk], 1
    if note.endswith(":complete"):
        CALLS["complete"] += 1
        url = "https://rimal-transport.example/contact"
        p = {"name": "مؤسسة الرمال للنقليات", "entity_type": "establishment", "activity": "نقل", "city": "الرياض",
             "address": "", "address_url": "", "website": "https://rimal-transport.example", "vehicle_evidence": "",
             "evidence_url": "", "evidence_quote": "", "operator_status": "unknown", "discovered_via": "بحث",
             "contacts": [{"kind": "phone", "value": "0551230099", "source_url": url}]}
        blk = NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url=url, content=NS(
            type="document", source=NS(type="text", data="مؤسسة الرمال للنقليات — جوال 0551230099"))))
        return {"prospects": [p]}, [blk], 1
    if note == "dir:reasons":
        url = "https://jobs.example/fleet-manager"
        blk = NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url=url, content=NS(
            type="document", source=NS(type="text", data="وظيفة شاغرة: مدير أسطول لشركة الوادي للشحن البري في الرياض"))))
        return {"items": [{"kind": "hiring_fleet", "title": "إعلان توظيف مدير أسطول", "date": "2026-10-05", "url": url,
                           "quote": "وظيفة شاغرة: مدير أسطول لشركة الوادي"},
                          {"kind": "new_branch", "title": "مختلق", "date": "", "url": url, "quote": "نص لا يوجد في الصفحة إطلاقًا"}]}, [blk], 1
    return {"items": [], "prospects": []}, [], 0


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ai, "ai_agent", fake_agent)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "voice_calls_enabled", False)
    monkeypatch.setattr(settings, "auto_outreach_enabled", False)


def setup_module():
    init_db()
    add_site("wadi-freight.example", "شركة الوادي للشحن البري", "نقل بضائع بأسطول شاحنات في الرياض",
             phone="0114567890", email="info@wadi-freight.example")
    add_site("waha-water.example", "مصنع الواحة للمياه", "مياه معبأة وتوصيل في الرياض", phone="0115550101")
    with session_scope() as s:
        s.add(Company(name="شركة قديمة لدينا للنقل", name_norm=__import__("mandoob.normalize", fromlist=["x"]).normalize_name(
            "شركة قديمة لدينا للنقل"), activity="نقل", city="الرياض", data_status="complete", lead_source="discovery"))


def test_two_stage_run_dedupes_before_cost_crawls_free_then_budgeted_completion():
    from mandoob import directory as D
    with session_scope() as s:
        r = D.request(s, sources=["claude_directory"], params={"activities": ["transport.freight"],
                                                              "scope": {"places": ["SA-01/riyadh"]}, "stage1_limit": 4},
                      budget=5, user_id=None)
        rid = r.id
    D.run(rid, crawler=FakeCrawler())
    with session_scope() as s:
        r = s.get(DirectoryRun, rid)
        st = r.stats["claude_directory"]
        stubs = {x.name: x for x in s.scalars(select(LeadStub).where(LeadStub.run_id == rid))}
        assert "شركة مختلقة لا تظهر في الصفحة" not in stubs and st["rejected_name"] >= 1   # اسم غير موجود في الدليل
        assert stubs["شركة قديمة لدينا للنقل"].status == "duplicate"                       # مكرر قبل أي تكلفة تفصيلية
        assert stubs["شركة الوادي للشحن البري"].status == "complete"                       # الزاحف وحده (بلا تكلفة)
        assert stubs["مصنع الواحة للمياه"].status == "incomplete"                          # هاتف بلا بريد
        assert stubs["مؤسسة الرمال للنقليات"].status == "incomplete"                       # الوكيل وجد الهاتف فقط
        wadi = s.get(Company, stubs["شركة الوادي للشحن البري"].company_id)
        assert wadi.lead_source == "directory:claude_directory" and wadi.fit_evidence_url == LISTING
        assert all(c.source_url for c in wadi.contacts)                                    # رابط لكل معلومة
        waha = s.get(Company, stubs["مصنع الواحة للمياه"].company_id)
        assert waha.retry_enrich_at is not None                                            # يُعاد البحث عن البريد لاحقًا
        assert r.status == "done" and r.stats["_spent"] > 0
    # الوكيل التفصيلي استُدعي للناقص فقط (لا للمكرر ولا لمن اكتمل بالزاحف)
    assert CALLS["complete"] == 2


def test_budget_stops_paid_calls():
    from mandoob import directory as D
    before = dict(CALLS)
    with session_scope() as s:
        r = D.request(s, sources=["claude_directory"], params={"activities": ["construction.contractors", "industry.food_factories"],
                                                              "scope": {"places": ["SA-01/riyadh", "SA-02/jeddah"]},
                                                              "stage1_limit": 500}, budget=0.5, user_id=None)
        rid = r.id
    D.run(rid, crawler=FakeCrawler())
    with session_scope() as s:
        r = s.get(DirectoryRun, rid)
        assert r.status == "stopped" and any("ميزانيتها" in x for x in r.log)
    assert CALLS["harvest"] - before["harvest"] <= 2


def test_sources_status_import_similar_reasons_and_daily_report():
    from mandoob import directory as D
    with session_scope() as s:
        s.add(ExistingCustomer(name="عميل حالي للنقل", name_norm=__import__("mandoob.normalize", fromlist=["x"]).normalize_name("عميل حالي للنقل")))
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        page = adm.get("/discover").text
        assert "يعمل" in page and "غير متاح آليًا من الخادم" in page and "منصة مقاول" in page
        csv = "name,city,activity,website,phone,source_url\nعميل حالي للنقل,الرياض,نقل,,,https://modon.example/1\n" \
              "مصنع جديد للبلاستيك,جدة,مصنع بلاستيك,https://plast.example,,https://modon.example/2\n"
        r = adm.post("/discover/import", data={"label": "مدن-اختبار"},
                     files={"file": ("f.csv", io.BytesIO(csv.encode()), "text/csv")}, follow_redirects=True)
        assert "جديد 1" in r.text and "من عملائنا 1" in r.text
        with session_scope() as s:
            wadi = s.scalar(select(Company).where(Company.name == "شركة الوادي للشحن البري"))
            wid = wadi.id
        r = adm.post(f"/companies/{wid}/similar", data={"regions": ["SA-12"], "limit": "10", "budget": "2"},
                     follow_redirects=False)
        assert r.status_code == 303
        with session_scope() as s:
            run = s.scalars(select(DirectoryRun).order_by(DirectoryRun.id.desc())).first()
            assert run.params["similar_to"] == wid and run.params["scope"] == {"regions": ["SA-12"]}
            assert any("نقل" in q for q in run.params["queries"])
            run.status = "done"
        assert D.find_reasons(wid) == 1                                                    # المختلق لم يُحفظ
        page = adm.get(f"/companies/{wid}").text
        assert "سبب ترشيح العميل" in page and "إعلان توظيف مدير أسطول" in page and "jobs.example/fleet-manager" in page
        adm.post(f"/companies/{wid}/reasons/add", data={"kind": "distribution_center", "title": "افتتاح مركز توزيع في الخرج",
                                                        "when": "2026-09-01", "url": "https://news.example/dc"})
        with session_scope() as s:
            assert s.scalar(select(func.count(CompanySignal.id)).where(CompanySignal.company_id == wid)) == 2
        # التقرير اليومي لكل مصدر + «وصل الفريق إلى المسؤول»
        adm.post(f"/companies/{wid}/contact/update", data={"status": "contacted", "note": "كلمت مدير الأسطول",
                                                           "reached_dm": "1"}, headers={"referer": f"http://testserver/companies/{wid}"})
        rep = adm.get("/daily-report").text
        assert "اكتشاف العملاء حسب المصدر" in rep and "أدلة وقوائم الويب" in rep
        with session_scope() as s:
            row = next(x for x in D.daily_by_source(s) if x["source"].startswith("أدلة وقوائم الويب"))
            assert row["discovered"] >= 4 and row["new"] >= 3 and row["complete"] >= 1 and row["reached_dm"] == 1
            assert row["cost_per_complete"] is not None
        x = adm.get("/daily-report.xlsx")
        assert x.status_code == 200


def test_web_and_social_sources_keep_trace_fallback_on_failure_daily_schedule_and_retry(monkeypatch):
    """بحث الويب + حسابات التواصل: الاسم يُقبل من نتيجة بحث حقيقية، ومرجع الخرائط يُحفظ والمصدر «بحث الويب»؛
    تعذّر مصدر ينقل للتالي؛ الجدولة اليومية مرة واحدة؛ والعودة للناقص دون إعادة البحث من البداية."""
    from mandoob import directory as D
    from mandoob.models import CompanySource
    add_site("nada-water.example", "مصنع الندى للمياه", "مياه معبأة نوصلها للمنازل في بريدة",
             phone="0163330000", email="sales@nada-water.example")
    calls = {"social": 0}

    def agent(system, prompt, schema, *, note="agent", **kw):
        record_cost("claude", 1, 0.2, note=note)
        if note == "dir:social:harvest":
            calls["social"] += 1
            raise ai.AIUnavailable("مهلة")                                          # المصدر تعذر ← التالي
        if note == "dir:web_search:harvest":
            assert "يوزع" in prompt or "توصيل" in prompt or "in " in prompt        # عبارة تصف طريقة العمل
            res = NS(type="web_search_tool_result", content=[
                NS(url="https://nada-water.example/", title="مصنع الندى للمياه — توصيل للمنازل في بريدة")])
            return {"items": [{"name": "مصنع الندى للمياه", "activity": "مياه معبأة", "city": "بريدة",
                               "website": "https://nada-water.example", "found_url": "https://nada-water.example/",
                               "maps_url": "https://www.google.com/maps/place/nada", "social_url": "https://x.com/nadawater",
                               "vehicle_hint": "نوصلها للمنازل"},
                              {"name": "منشأة وهمية بلا نتيجة", "activity": "x", "city": "بريدة", "website": "",
                               "found_url": "https://nowhere.example/", "maps_url": "", "social_url": "", "vehicle_hint": ""}]}, [res], 1
        return fake_agent(system, prompt, schema, note=note, **kw)
    monkeypatch.setattr(ai, "ai_agent", agent)
    with session_scope() as s:
        r = D.request(s, sources=["social", "web_search"], params={"activities": ["industry.water_factories"],
                                                                   "scope": {"places": ["SA-05/buraydah"]}, "stage1_limit": 1},
                      budget=3, user_id=None)
        rid = r.id
    D.run(rid, crawler=FakeCrawler())
    with session_scope() as s:
        r = s.get(DirectoryRun, rid)
        assert calls["social"] == 1 and any("تعذر" in x and "انتقال للتالي" in x for x in r.log)
        st = s.scalar(select(LeadStub).where(LeadStub.run_id == rid, LeadStub.name == "مصنع الندى للمياه"))
        assert st.source == "web_search" and st.maps_url.startswith("https://www.google.com/maps") and st.query
        assert not s.scalar(select(LeadStub).where(LeadStub.name == "منشأة وهمية بلا نتيجة"))
        assert st.status == "complete"
        co = s.get(Company, st.company_id)
        kinds = {x.source_type for x in s.scalars(select(CompanySource).where(CompanySource.company_id == co.id))}
        assert {"web_search", "maps_ref", "social"} <= kinds and co.lead_source == "directory:web_search"
        assert all(c.source_url.startswith("https://nada-water.example") for c in co.contacts)
        row = next(x for x in D.daily_by_source(s) if x["key"] == "web_search")
        assert row["complete"] >= 1 and row["maps_refs"] >= 1
    # الجدولة اليومية: مرة واحدة في اليوم بعد وقتها
    with session_scope() as s:
        for x in s.scalars(select(DirectoryRun).where(DirectoryRun.status.in_(["pending", "running"]))):
            x.status = "done"
        D.set_auto(s, enabled=True, time="00:00", budget_usd=1)
        a = D.schedule_daily(s)
        assert a is not None and a.params["auto"] and a.params["target_complete"] == 400
        a.status = "done"
        assert D.schedule_daily(s) is None
    # «قيد الاستكمال»: العودة للناقص فقط دون مرحلة ١ جديدة
    from datetime import timedelta
    from mandoob.timeutil import now_utc
    with session_scope() as s:
        waha = s.scalar(select(Company).where(Company.name == "مصنع الواحة للمياه"))
        waha.retry_enrich_at = now_utc() - timedelta(minutes=1)
        stubs_before = s.scalar(select(func.count(LeadStub.id)))
    retry_calls = []

    def agent2(system, prompt, schema, *, note="agent", **kw):
        retry_calls.append(note)
        url = "https://waha-water.example/contact-us"
        blk = NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url=url, content=NS(
            type="document", source=NS(type="text", data="مصنع الواحة للمياه — info@waha-water.example"))))
        p = {"name": "مصنع الواحة للمياه", "entity_type": "factory", "activity": "مياه", "city": "الرياض", "address": "",
             "address_url": "", "website": "https://waha-water.example", "vehicle_evidence": "", "evidence_url": "",
             "evidence_quote": "", "operator_status": "unknown", "discovered_via": "x",
             "contacts": [{"kind": "email", "value": "info@waha-water.example", "source_url": url}]}
        return {"prospects": [p]}, [blk], 1
    monkeypatch.setattr(ai, "ai_agent", agent2)
    n = D.retry_incomplete({}, lambda: None)
    with session_scope() as s:
        assert n >= 1 and retry_calls and all(x.endswith(":retry") for x in retry_calls)
        assert s.scalar(select(Company).where(Company.name == "مصنع الواحة للمياه")).data_status == "complete"
        assert s.scalar(select(func.count(LeadStub.id))) == stubs_before                  # لا بحث من البداية
