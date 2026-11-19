"""البحث الآلي عن العملاء: الوكيل يجمع البيانات، والمنصة تقبل فقط ما ظهر نصًا في صفحة قرأها الوكيل."""
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mandoob import ai
from mandoob.config import settings
from mandoob.db import init_db, session_scope
from mandoob.discovery.agent import AgentSource, parse_agent_output, value_on_pages, FetchedPage
from mandoob.discovery.runner import run_campaign_discovery
from mandoob.models import Campaign, Category, Company, DiscoveryRun
from mandoob.timeutil import riyadh_today

# صفحات «قرأها» الوكيل (تحاكي نتائج أداة web_fetch)
PAGES = {
    "https://faisal-tow.example/contact": "سطحات الفيصل — نقل وسحب السيارات داخل الرياض على مدار الساعة. "
                                          "جوال: 0551234000 — البريد: info@faisal-tow.example",
    "https://najd-hvac.example/ar/contact": "نجد للتكييف: فرق صيانة متنقلة تزور العملاء يوميًا. هاتف 011 234 5678 "
                                            "ومراسلات الأعمال sales@najd-hvac.example",
    "https://riyadh-bakery.example/about": "مخابز الرياض المركزية: نوزّع منتجاتنا يوميًا بأسطول مبرد من 40 سيارة.",
    "https://riyadh-bakery.example/contact": "اتصل بنا: 920011223",
    "https://movers-pro.example/contact": "نقل عفش مع الفك والتركيب بسياراتنا المغلقة. 0509998887 — "
                                          "orders@movers-pro.example",
    "https://green-water.example/contact": "مياه الخضراء — توصيل مياه الشرب بأسطول التوزيع الخاص بنا. "
                                           "966112229988+ ، care@green-water.example",
}


def block(url):
    return NS(type="web_fetch_tool_result",
              content=NS(type="web_fetch_result", url=url,
                         content=NS(type="document", source=NS(type="text", data=PAGES[url]))))


def prospect(name, url, quote, contacts, entity="establishment", website=None, evidence=None):
    return {"name": name, "entity_type": entity, "activity": "نشاط يستخدم مركبات", "city": "الرياض",
            "website": website or url.split("/")[0] + "//" + url.split("/")[2], "vehicle_evidence": evidence or quote,
            "evidence_url": url, "evidence_quote": quote, "operator_status": "self", "discovered_via": "بحث",
            "contacts": contacts}


BATCHES = [
    ({"prospects": [
        prospect("سطحات الفيصل", "https://faisal-tow.example/contact", "نقل وسحب السيارات داخل الرياض",
                 [{"kind": "phone", "value": "0551234000", "source_url": "https://faisal-tow.example/contact"},
                  {"kind": "email", "value": "info@faisal-tow.example", "source_url": "https://faisal-tow.example/contact"}],
                 entity="sole_proprietor"),
        prospect("نجد للتكييف", "https://najd-hvac.example/ar/contact", "فرق صيانة متنقلة تزور العملاء يوميًا",
                 [{"kind": "phone", "value": "+966112345678", "source_url": "https://najd-hvac.example/ar/contact"},
                  {"kind": "email", "value": "sales@najd-hvac.example", "source_url": "https://najd-hvac.example/ar/contact"}]),
        # بريد مختلَق لا يظهر في الصفحة → يُرفض، والسجل يبقى «قيد الاستكمال»
        prospect("مخابز الرياض المركزية", "https://riyadh-bakery.example/about", "نوزّع منتجاتنا يوميًا بأسطول مبرد",
                 [{"kind": "phone", "value": "920011223", "source_url": "https://riyadh-bakery.example/contact"},
                  {"kind": "email", "value": "info@riyadh-bakery.example", "source_url": "https://riyadh-bakery.example/contact"}]),
        prospect("فرد بسيارته", "https://faisal-tow.example/contact", "x" * 10, [], entity="private_individual"),
    ]}, ["https://faisal-tow.example/contact", "https://najd-hvac.example/ar/contact",
         "https://riyadh-bakery.example/about", "https://riyadh-bakery.example/contact"]),
    ({"prospects": [
        # تكرار: نفس نطاق «نجد» باسم مختلف → مكرر لا يُحتسب
        prospect("شركة نجد لأنظمة التكييف", "https://najd-hvac.example/ar/contact", "فرق صيانة متنقلة",
                 [{"kind": "phone", "value": "0112345678", "source_url": "https://najd-hvac.example/ar/contact"}]),
        prospect("موفرز برو لنقل العفش", "https://movers-pro.example/contact", "نقل عفش مع الفك والتركيب بسياراتنا المغلقة",
                 [{"kind": "phone", "value": "0509998887", "source_url": "https://movers-pro.example/contact"},
                  {"kind": "email", "value": "orders@movers-pro.example", "source_url": "https://movers-pro.example/contact"}]),
        prospect("مياه الخضراء", "https://green-water.example/contact", "توصيل مياه الشرب بأسطول التوزيع الخاص بنا",
                 [{"kind": "phone", "value": "+966112229988", "source_url": "https://green-water.example/contact"},
                  {"kind": "email", "value": "care@green-water.example", "source_url": "https://green-water.example/contact"}]),
    ]}, ["https://najd-hvac.example/ar/contact", "https://movers-pro.example/contact", "https://green-water.example/contact"]),
]
CALLS = {"n": 0, "prompts": []}


def fake_agent(system, prompt, schema, **kw):
    CALLS["prompts"].append(prompt)
    if kw.get("note") == "complete":
        return {"prospects": []}, [], 0
    i = min(CALLS["n"], len(BATCHES) - 1)
    CALLS["n"] += 1
    out, urls = BATCHES[i] if CALLS["n"] <= len(BATCHES) else ({"prospects": []}, [])
    return out, [block(u) for u in urls], 3


class NoCrawl:
    def crawl(self, website):
        from mandoob.discovery.crawler import SiteResult
        return SiteResult(website=website, error="blocked")


@pytest.fixture(autouse=True)
def _agent(monkeypatch):
    monkeypatch.setattr(ai, "ai_agent", fake_agent)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    yield


def test_value_must_appear_on_fetched_page():
    pages = [FetchedPage("https://a.example/contact", "هاتف ٠١١ ٢٣٤ ٥٦٧٨ — sales@a.example")]
    assert value_on_pages("phone", "+966112345678", "https://a.example/contact", pages, []) == "https://a.example/contact"
    assert value_on_pages("email", "sales@a.example", "https://a.example/contact", pages, [])
    assert value_on_pages("email", "info@a.example", "https://a.example/contact", pages, []) is None
    # القيمة في صفحة نطاق آخر لا تكفي
    assert value_on_pages("email", "sales@a.example", "https://b.example/x", pages, []) is None


def test_two_batches_of_real_style_results_without_repeats():
    init_db()
    with session_scope() as s:
        camp = Campaign(name="البحث الآلي — الرياض", status="active", daily_target=400, send_enabled=False)
        s.add(camp)
        s.flush()
        s.add(Category(campaign_id=camp.id, sector_key="vehicle_users", label="أي نشاط يستخدم مركبات",
                       cities=["الرياض"], keywords_ar=["صيانة متنقلة", "نقل عفش", "توزيع مبرد"], quota=400))
        cid = camp.id
    agent = AgentSource()
    day = riyadh_today()
    run_campaign_discovery(cid, sources={"ai_agent": agent}, crawler=NoCrawl(), max_new=2, day=day)
    with session_scope() as s:
        got = {c.name: c for c in s.scalars(select(Company).where(Company.campaign_id == cid))}
        complete = [c for c in got.values() if c.data_status == "complete"]
        # الدفعة تتوقف بعد عميلين مكتملين، لكن بقية نتائج نداء الوكيل نفسه تُحفظ (لا هدر)
        assert {c.name for c in complete} == {"سطحات الفيصل", "نجد للتكييف"}
        najd = got["نجد للتكييف"]
        assert najd.primary("email").source_url == "https://najd-hvac.example/ar/contact"
        assert najd.fit_phrase == "فرق صيانة متنقلة تزور العملاء يوميًا"
        assert najd.fit_evidence_url == "https://najd-hvac.example/ar/contact"
        bakery = got["مخابز الرياض المركزية"]
        assert bakery.data_status == "incomplete"  # البريد المختلَق رُفض
        assert not any(ct.kind == "email" for ct in bakery.contacts)
        assert "فرد بسيارته" not in got
        assert agent.last.rejected_values and any(r.get("value") == "info@riyadh-bakery.example"
                                                  for r in agent.last.rejected_values)
    # الدفعة الثانية: نتائج جديدة دون تكرار، والوكيل يُعطى الأسماء المعروفة ليتجنبها
    run_campaign_discovery(cid, sources={"ai_agent": AgentSource(known_fn=lambda: ["najd-hvac.example"])},
                           crawler=NoCrawl(), max_new=2, day=day)
    with session_scope() as s:
        names = [c.name for c in s.scalars(select(Company).where(Company.campaign_id == cid))]
        assert names.count("نجد للتكييف") == 1 and "شركة نجد لأنظمة التكييف" not in names
        assert {"موفرز برو لنقل العفش", "مياه الخضراء"} <= set(names)
        run = s.scalar(select(DiscoveryRun).where(DiscoveryRun.campaign_id == cid))
        assert run.duplicate_count >= 1
    assert "najd-hvac.example" in CALLS["prompts"][-1] or any("najd-hvac.example" in p for p in CALLS["prompts"])


def test_today_page_and_auto_search_screen():
    from mandoob.web.app import app
    with TestClient(app) as c:
        from helpers import otp_login
        otp_login(c, "admin@sayaraup.test", "admin")
        r = c.get("/today")
        assert r.status_code == 200 and "عملاء اليوم" in r.text and "مكررات مستبعدة" in r.text
        assert "نجد للتكييف" in r.text and "https://najd-hvac.example/ar/contact" in r.text
        x = c.get("/today.xlsx")
        assert x.status_code == 200 and x.content[:2] == b"PK"
        r = c.get("/auto-search")
        assert r.status_code == 200 and "البحث الآلي عن العملاء" in r.text


def test_saudi_city_and_same_entity_rules():
    from mandoob.segments import saudi_city
    from mandoob.discovery.agent import same_entity
    assert saudi_city("Riyadh, Saudi Arabia") == "الرياض" and saudi_city("حي الروضة، جدة") == "جدة"
    assert saudi_city("Dubai") is None and saudi_city("المملكة العربية السعودية") is None
    pages = [FetchedPage("https://facebook.example/najdhvac", "صفحة نجد للتكييف — صيانة متنقلة 0551112233"),
             FetchedPage("https://other.example/list", "دليل شركات: مؤسسة أخرى 0551112233")]
    assert same_entity("https://facebook.example/najdhvac", "نجد للتكييف", "https://najd-hvac.example", pages)
    assert same_entity("https://najd-hvac.example/contact", "نجد للتكييف", "https://najd-hvac.example", pages)
    assert not same_entity("https://other.example/list", "نجد للتكييف", "https://najd-hvac.example", pages)


def test_contact_must_belong_to_same_entity():
    """من تدقيق المصادر: بريد بنطاق مطوّر الموقع، وهاتف من موقع منشأة أخرى، لا يحتسبان."""
    from mandoob.companies import contact_belongs, is_complete
    from mandoob.models import Company, Contact
    c = Company(name="شركة المتنقل للخدمات", activity="ورشة متنقلة", city="الرياض", website="https://almtngl-ksa.com",
                lead_source="discovery", is_test=False)
    ph = Contact(kind="phone", value="+966561115652", source_url="https://almtngl-ksa.com/contact-us/", verified=True)
    em = Contact(kind="email", value="info@tahalufapp.com", source_url="https://almtngl-ksa.com/contact-us/", verified=True)
    c.contacts = [ph, em]
    assert contact_belongs(c, ph) and not contact_belongs(c, em) and not is_complete(c)
    em.value = "info@almtngl-ksa.com"
    assert is_complete(c)
    em.value = "owner123@gmail.com"  # مجاني منشور على موقعها: مقبول
    assert is_complete(c)
    em.source_url = "https://some-directory.com/x"  # مجاني من موقع آخر: مرفوض
    assert not is_complete(c)
    em.source_url = "https://almtngl-ksa.com/"
    ph.source_url = "https://alfahscenter.com/page"  # هاتف من موقع منشأة أخرى
    assert not is_complete(c)


def test_batch_spreads_across_sectors_and_records_vehicle_indicator(monkeypatch):
    """دفعة على عدة قطاعات تتوزع بينها؛ المنشأة بلا دليل مركبات تبقى ضمن نشاطها بمؤشر «غير مؤكد»."""
    pages = {"https://alnoor-school.example/contact": "مدارس النور الأهلية — هاتف 0114441122 — info@alnoor-school.example",
             "https://clean-laundry.example/": "مغسلة النقاء: نستلم الملابس ونوصلها بسياراتنا. 0503332211 "
                                               "care@clean-laundry.example"}
    seen = []

    def agent(system, prompt, schema, **kw):
        seen.append(prompt)
        if "مدارس" in prompt:
            url = "https://alnoor-school.example/contact"
            p = {"name": "مدارس النور الأهلية", "entity_type": "company", "activity": "مدرسة أهلية", "city": "الرياض",
                 "website": "https://alnoor-school.example", "vehicle_evidence": "", "evidence_url": "",
                 "evidence_quote": "", "operator_status": "unknown", "discovered_via": "بحث",
                 "contacts": [{"kind": "phone", "value": "0114441122", "source_url": url},
                              {"kind": "email", "value": "info@alnoor-school.example", "source_url": url}]}
        else:
            url = "https://clean-laundry.example/"
            p = {"name": "مغسلة النقاء", "entity_type": "establishment", "activity": "مغسلة ملابس", "city": "الرياض",
                 "website": "https://clean-laundry.example", "vehicle_evidence": "تستلم الملابس وتوصلها بسياراتها",
                 "evidence_url": url, "evidence_quote": "نستلم الملابس ونوصلها بسياراتنا", "operator_status": "self",
                 "discovered_via": "بحث",
                 "contacts": [{"kind": "phone", "value": "0503332211", "source_url": url},
                              {"kind": "email", "value": "care@clean-laundry.example", "source_url": url}]}
        b = NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url=url, content=NS(
            type="document", source=NS(type="text", data=pages[url]))))
        return {"prospects": [p]}, [b], 2

    monkeypatch.setattr(ai, "ai_agent", agent)
    init_db()
    with session_scope() as s:
        camp = Campaign(name="البحث عن العملاء — اختبار القطاعات", status="paused", daily_target=400, send_enabled=False)
        s.add(camp)
        s.flush()
        s.add(Category(campaign_id=camp.id, sector_key="education", label="التعليم والتدريب", cities=["الرياض"],
                       keywords_ar=["مدارس أهلية"], quota=200))
        s.add(Category(campaign_id=camp.id, sector_key="community", label="الخدمات المحلية", cities=["الرياض"],
                       keywords_ar=["مغسلة ملابس توصيل"], quota=200))
        cid = camp.id
    run_campaign_discovery(cid, sources={"ai_agent": AgentSource()}, crawler=NoCrawl(), max_new=2, force=True)
    assert len(seen) == 2 and "مدارس" in seen[0] and "مغسلة" in seen[1]  # القطاع الأول لم يستهلك الدفعة كلها
    with session_scope() as s:
        got = {c.name: c for c in s.scalars(select(Company).where(Company.campaign_id == cid))}
        school, laundry = got["مدارس النور الأهلية"], got["مغسلة النقاء"]
        assert school.data_status == "complete" and school.vehicle_indicator == "unconfirmed"
        assert school.entity_type == "company" and "غير مؤكد" in school.fit_reason
        assert laundry.vehicle_indicator == "confirmed" and laundry.entity_type == "establishment"
        assert laundry.fit_score > school.fit_score  # الأولوية لمن يعلن التوصيل أو الأسطول
