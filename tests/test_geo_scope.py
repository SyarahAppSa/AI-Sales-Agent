"""استهداف مناطق المملكة في «البحث عن العملاء»: المملكة كاملة، أو مناطق كاملة، أو مدن بعينها — في البحث الفوري والمجدول."""
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from helpers import otp_login
from mandoob import ai, geo, prospecting as P
from mandoob.config import settings
from mandoob.db import init_db, session_scope
from mandoob.discovery.agent import AgentSource
from mandoob.discovery.runner import run_campaign_discovery
from mandoob.models import Campaign, Company, DiscoveryRun, Setting
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
SEEN: list[str] = []
COUNTER = {"n": 0}


def fake_agent(system, prompt, schema, **kw):
    """يعيد منشأة واحدة لكل نداء، عنوانها في المدينة المطلوبة، وصفحتها «مقروءة» فعلًا."""
    city = prompt.split("المدينة: ")[1].split("\n")[0]
    SEEN.append(city)
    place = city.split("،")[0]
    COUNTER["n"] += 1
    n = COUNTER["n"]
    url = f"https://geo-test-{n}.example/contact"
    page = f"مؤسسة اختبار {n} — العنوان: حي النخيل، {place}. هاتف 05{n:08d} بريد info@geo-test-{n}.example توصيل بسياراتنا"
    p = {"name": f"مؤسسة اختبار {n}", "entity_type": "establishment", "activity": "توصيل", "city": place,
         "address": f"حي النخيل، {place}", "address_url": url, "website": f"https://geo-test-{n}.example",
         "vehicle_evidence": "توصيل بسياراتها", "evidence_url": url, "evidence_quote": "توصيل بسياراتنا",
         "operator_status": "self", "discovered_via": "بحث",
         "contacts": [{"kind": "phone", "value": f"05{n:08d}", "source_url": url},
                      {"kind": "email", "value": f"info@geo-test-{n}.example", "source_url": url}]}
    block = NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url=url, content=NS(
        type="document", source=NS(type="text", data=page))))
    return {"prospects": [p]}, [block], 1


class NoCrawl:
    def crawl(self, website):
        from mandoob.discovery.crawler import SiteResult
        return SiteResult(website=website, error="blocked")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ai, "ai_agent", fake_agent)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "voice_calls_enabled", False)
    monkeypatch.setattr(settings, "auto_outreach_enabled", False)
    SEEN.clear()


def _save(c, **data):
    base = {"activities": ["transport.parcels"], "daily_target": "400", "time": "06:00"}
    r = c.post("/prospecting", data={**base, **data}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        cfg = P.get_config(s)
        return cfg, cfg["campaign_id"]


DAY = {"n": 100}


def _day():
    """يوم جديد لكل تشغيل اختبار (تشغيل مستقل بلا نقطة توقف سابقة)."""
    from datetime import timedelta
    from mandoob.timeutil import riyadh_today
    DAY["n"] += 1
    return riyadh_today() + timedelta(days=DAY["n"])


def _run(cid, n):
    SEEN.clear()
    run_campaign_discovery(cid, sources={"ai_agent": AgentSource()}, crawler=NoCrawl(), max_new=n, force=True,
                           day=_day())
    return [x.split("،")[0] for x in SEEN]


def test_directory_has_13_regions_with_capitals_and_stable_ids():
    assert len(geo.REGIONS) == 13
    caps = {r["ar"]: r["capital"] for r in geo.REGIONS.values()}
    assert caps["منطقة الحدود الشمالية"] == "عرعر" and caps["منطقة الجوف"] == "سكاكا"
    assert caps["منطقة القصيم"] == "بريدة" and caps["منطقة الباحة"] == "الباحة" and caps["منطقة نجران"] == "نجران"
    assert "SA-01/al-kharj" in geo.REGIONS["SA-01"]["places"] and geo.PLACES["SA-07/duba"]["ar"] == "ضباء"


def test_all_kingdom_saved_and_reopened_shows_13_regions_once():
    init_db()
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        cfg, _ = _save(c, scope_mode="all")
        assert cfg["scope"]["all"] and "جميع مناطق المملكة (١٣)" in cfg["scope_summary"]
        page = c.get("/prospecting").text
        assert "جميع مناطق المملكة (١٣)" in page
        for r in geo.REGIONS.values():
            assert page.count(f"> {r['ar']} كاملة</label>") == 1
        assert 'value="all" checked' in page


@pytest.mark.parametrize("rid", ["SA-12", "SA-08", "SA-11", "SA-10"])
def test_each_new_region_alone_queries_its_own_places(rid):
    init_db()
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        cfg, cid = _save(c, scope_mode="custom", regions=[rid])
    assert cfg["scope"]["regions"] == [rid]
    cities = _run(cid, len(geo.REGIONS[rid]["places"]) * 2)
    names = {geo.PLACES[p]["ar"] for p in geo.REGIONS[rid]["places"]}
    assert cities and set(cities) <= names


def test_riyadh_city_only_vs_riyadh_region_including_kharj():
    init_db()
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        _, cid = _save(c, scope_mode="custom", places=["SA-01/riyadh"])
        with session_scope() as s:
            s.get(Campaign, cid).categories[0].cursor = 0
        city_only = _run(cid, 6)
        _, cid = _save(c, scope_mode="custom", regions=["SA-01"])
        with session_scope() as s:
            for cat in s.get(Campaign, cid).categories:
                cat.cursor = 0
        region = _run(cid, 6)
    assert set(city_only) == {"الرياض"}
    assert "الخرج" in region and "الرياض" in region and len(set(region)) >= 3


def test_jouf_full_plus_jeddah_only_and_partial_region_exclusion():
    init_db()
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        cfg, cid = _save(c, scope_mode="custom", regions=["SA-12"], places=["SA-02/jeddah"])
        places = geo.scope_places(cfg["scope"])
        assert "SA-12/sakaka" in places and "SA-02/jeddah" in places and "SA-02/taif" not in places
        assert cfg["scope_summary"].startswith("منطقة الجوف كاملة + جدة فقط")
        cities = _run(cid, 12)
        assert "جدة" in cities and "سكاكا" in cities and "الطائف" not in cities
        # منطقة الرياض كاملة عدا الخرج ← «اختيار جزئي» والاستثناء محفوظ
        riyadh = [p for p in geo.REGIONS["SA-01"]["places"] if p != "SA-01/al-kharj"]
        cfg, _ = _save(c, scope_mode="custom", regions=["SA-01"], places=riyadh)
        assert cfg["scope"]["exclude"] == ["SA-01/al-kharj"] and "جزئي" in cfg["scope_summary"]
        assert "SA-01/al-kharj" not in geo.scope_places(cfg["scope"])
        # الاسم المكتوب يُربط بالدليل، والفارغ يُرفض صراحة
        cfg, _ = _save(c, scope_mode="custom", extra_cities="ضباء — منطقة تبوك")
        assert cfg["scope"]["places"] == ["SA-07/duba"]
        r = c.post("/prospecting", data={"activities": ["transport.parcels"], "scope_mode": "custom",
                                         "daily_target": "400"}, follow_redirects=True)
        assert "حدد نطاق البحث" in r.text
        r = c.post("/prospecting", data={"activities": ["transport.parcels"], "scope_mode": "custom",
                                         "extra_cities": "دبي", "daily_target": "400"}, follow_redirects=True)
        assert "غير موجود في دليل الأماكن" in r.text


def test_dubai_rejected_duba_accepted_and_two_branches_count_once(monkeypatch):
    assert geo.resolve_city("Dubai") is None and geo.resolve_city("دبي، الإمارات") is None
    assert geo.resolve_city("Duba, Tabuk") == "SA-07/duba"
    init_db()
    calls = {"n": 0}

    def agent(system, prompt, schema, **kw):
        calls["n"] += 1
        url = "https://twobranch.example/branches"
        page = ("شركة الفرعين للتوصيل — فرع ضباء: حي الساحل، ضباء. فرع تبوك: حي الريان، تبوك. "
                "0551231234 info@twobranch.example — مكتب دبي: Dubai Marina")
        if calls["n"] == 1:
            ps = [("مكتب دبي للتوصيل", "Dubai", "Dubai Marina, Dubai", "https://dubai-office.example/"),
                  ("شركة الفرعين للتوصيل", "ضباء", "حي الساحل، ضباء", url)]
        else:
            ps = [("شركة الفرعين للتوصيل", "تبوك", "حي الريان، تبوك", url)]
        out = []
        for name, city, addr, u in ps:
            out.append({"name": name, "entity_type": "company", "activity": "توصيل", "city": city, "address": addr,
                        "address_url": u, "website": "https://twobranch.example" if "twobranch" in u else u,
                        "vehicle_evidence": "", "evidence_url": "", "evidence_quote": "", "operator_status": "unknown",
                        "discovered_via": "بحث",
                        "contacts": [{"kind": "phone", "value": "0551231234", "source_url": url},
                                     {"kind": "email", "value": "info@twobranch.example", "source_url": url}]})
        block = NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url=url, content=NS(
            type="document", source=NS(type="text", data=page))))
        return {"prospects": out}, [block], 1

    monkeypatch.setattr(ai, "ai_agent", agent)
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        _, cid = _save(c, scope_mode="custom", regions=["SA-07"])
    with session_scope() as s:
        before = s.scalar(select(func.count(Company.id)).where(Company.campaign_id == cid))
        for cat in s.get(Campaign, cid).categories:
            cat.cursor = 0
    day = _day()
    run_campaign_discovery(cid, sources={"ai_agent": AgentSource()}, crawler=NoCrawl(), max_new=5, force=True, day=day)
    with session_scope() as s:
        rows = list(s.scalars(select(Company).where(Company.campaign_id == cid, Company.name.like("%الفرعين%"))))
        assert len(rows) == 1
        co = rows[0]
        assert co.place_id == "SA-07/duba" and co.region_id == "SA-07"
        assert [b["city"] for b in (co.branches or [])] == ["تبوك"]
        assert not s.scalar(select(Company.id).where(Company.name.like("%دبي%")))
        after = s.scalar(select(func.count(Company.id)).where(Company.campaign_id == cid))
        assert after - before == 1
        run = s.scalar(select(DiscoveryRun).where(DiscoveryRun.campaign_id == cid, DiscoveryRun.run_date == day))
        assert any("Dubai" in x or "غير سعودي" in x for x in run.log)


def test_scheduled_and_run_now_use_saved_scope_and_report_by_region(monkeypatch):
    from mandoob import jobs
    init_db()
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        _, cid = _save(c, scope_mode="custom", regions=["SA-11"], places=["SA-02/jeddah"], scheduled="1")
    with session_scope() as s:
        others = [x.id for x in s.scalars(select(Campaign).where(Campaign.status == "active", Campaign.id != cid))]
        for x in others:
            s.get(Campaign, x).status = "paused"
    import mandoob.discovery.runner as runner
    real = runner.run_campaign_discovery
    used = []

    def wrapped(c_id, **kw):
        used.append((c_id, kw.get("force", False)))
        kw.setdefault("sources", {"ai_agent": AgentSource()})
        kw.setdefault("crawler", NoCrawl())
        kw.setdefault("max_new", 4)
        return real(c_id, **kw)

    monkeypatch.setattr(runner, "run_campaign_discovery", wrapped)
    monkeypatch.setattr(runner, "enrich_incomplete", lambda **kw: 0)
    jobs.discovery_tick(force=True)  # المجدول (اللوحة مغلقة)
    allowed = {geo.PLACES[p]["ar"] for p in geo.REGIONS["SA-11"]["places"]} | {"جدة"}
    sched_cities = [x.split("،")[0] for x in SEEN]
    assert used and used[0][0] == cid and set(sched_cities) <= allowed
    SEEN.clear()
    with session_scope() as s:
        P.request_run(s, cid, 2, None)
    jobs.discovery_request_tick()  # «ابحث الآن»
    assert used[-1] == (cid, True) and set(x.split("،")[0] for x in SEEN) <= allowed
    with session_scope() as s:
        st = P.status(s)
        regs = {r["label"]: r for r in st["regions"]}
        assert regs["منطقة الباحة"]["searched"] > 0 and regs["منطقة مكة المكرمة"]["searched"] > 0
        assert sum(r["complete"] for r in st["regions"]) == st["done_today"]
        assert st["nexts"] and st["nexts"][0]["place"]
        cfg = P.get_config(s)
        s.get(Campaign, cid).status = "paused"
        for x in others:
            s.get(Campaign, x).status = "active"
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        page = c.get("/prospecting").text
        assert "نتائج البحث حسب المنطقة" in page and "أين يكمل البحث في الدفعة القادمة" in page
        assert "وُجدت نتائج" in page


def test_previous_single_city_config_keeps_its_city():
    init_db()
    with session_scope() as s:
        row = s.get(Setting, P.SETTING)
        old = dict(row.value) if row else {}
        legacy = {k: v for k, v in old.items() if k != "scope"}
        legacy.update(cities=["الرياض"], regions=[])
        if row:
            row.value = legacy
        else:
            s.add(Setting(key=P.SETTING, value=legacy))
    with session_scope() as s:
        cfg = P.get_config(s)
        assert geo.scope_places(cfg["scope"]) == ["SA-01/riyadh"] and "الرياض فقط" in cfg["scope_summary"]
    # حملة سابقة بلا نطاق جغرافي (مثل «البحث الآلي — الرياض») تبقى على مدنها
    with session_scope() as s:
        from mandoob.models import Category
        camp = Campaign(name="البحث الآلي — الرياض (سابقة)", status="paused", daily_target=5, send_enabled=False)
        s.add(camp)
        s.flush()
        s.add(Category(campaign_id=camp.id, sector_key="delivery", label="x", cities=["الرياض"],
                       keywords_ar=["توصيل"], quota=5))
        cid = camp.id
    cities = _run(cid, 1)
    assert set(cities) == {"الرياض"}
