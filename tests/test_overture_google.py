"""«اكتشاف عملاء جدد»: Overture Maps Places مصدرًا مستقلًا يحفظ (الهاتف والبريد من الموقع الرسمي فقط، ومطابقة العملاء
الحاليين قبل أي شيء)، و«بحث مباشر في خرائط Google» للعرض فقط — لا يُحفظ منه شيء."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from fakes import FakeCrawler, add_site
from helpers import otp_login
from mandoob.config import settings
from mandoob.db import init_db, session_scope
from mandoob.models import Company, Contact, CompanySource, CostEntry, DirectoryRun, ExistingCustomer, LeadStub, Setting
from mandoob.normalize import normalize_name
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"


def _row(i, name, tp, site, phones=None):
    return {"id": f"ov-{i}", "name": name, "bc": None, "tp": tp, "websites": [site] if site else None,
            "phones": phones, "emails": None, "locality": "الرياض", "os": None, "confidence": 0.9,
            "lic": "CDLA-Permissive-2.0", "ds": "meta", "lat": 24.7 + i / 1000, "lon": 46.7, "release": "2026-11-18.0"}


ROWS = [
    _row(1, "شركة الصقر للشحن للفحص", "freight_and_cargo_service", "https://ovt-saqr-cargo.example", ["+966557730001"]),
    _row(2, "مؤسسة الوديان اللوجستية للفحص", "freight_and_cargo_service", "https://ovt-wadi-logi.example"),
    _row(3, "عميلنا الحالي لفحص أوفرتشر", "freight_and_cargo_service", "https://ovt-current-client.example"),
    _row(4, "شركة قديمة في المنصة لفحص أوفرتشر", "shipping_center", "https://ovt-old-platform.example"),
    _row(5, "دينا نقل عفش", "freight_and_cargo_service", "https://wa.me/966500000000"),   # ليس موقعًا رسميًا
    _row(6, "مقهى الرياض", "coffee_shop", "https://cafe.example"),                           # نشاط آخر
    _row(7, "شركة بلا تواصل لفحص أوفرتشر", "freight_and_cargo_service", "https://ovt-nocontact.example"),
]


def setup_module():
    init_db()
    add_site("ovt-saqr-cargo.example", "شركة الصقر للشحن للفحص", "نقل بضائع بأسطول شاحنات", phone="0117730001",
             email="sales@ovt-saqr-cargo.example")
    add_site("ovt-wadi-logi.example", "مؤسسة الوديان اللوجستية للفحص", "خدمات لوجستية", phone="0117730002")
    add_site("ovt-nocontact.example", "شركة بلا تواصل لفحص أوفرتشر", "خدمات شحن")
    with session_scope() as s:
        s.add(ExistingCustomer(name="عميلنا الحالي لفحص أوفرتشر", name_norm=normalize_name("عميلنا الحالي لفحص أوفرتشر")))
        s.add(Company(name="شركة قديمة في المنصة لفحص أوفرتشر", name_norm=normalize_name("شركة قديمة في المنصة لفحص أوفرتشر"),
                      website="https://ovt-old-platform.example", domain="ovt-old-platform.example", data_status="complete"))


def test_overture_source_saves_only_site_verified_contacts_and_excludes_customers(monkeypatch):
    from mandoob import directory as D, overture as OV
    monkeypatch.setattr(OV, "city_rows", lambda pid: ROWS if pid == "SA-01/riyadh" else [])
    monkeypatch.setattr(settings, "anthropic_api_key", "")  # لا وكيل بحث: المصدر المستقل وحده
    with session_scope() as s:
        r = D.request(s, sources=["overture", "google_places"], params={
            "activities": ["transport.freight"], "scope": {"places": ["SA-01/riyadh"]}, "target_complete": 400}, budget=5,
            user_id=None)
        rid = r.id
    D.run(rid, crawler=FakeCrawler())
    with session_scope() as s:
        r = s.get(DirectoryRun, rid)
        st = r.stats["overture"]
        assert "google_places" not in r.stats and any("للعرض المباشر فقط" in x for x in r.log)
        assert st["discovered"] == 5  # ٧ صفوف: نشاط آخر + رابط واتساب مستبعدان
        assert st["existing_customer"] == 1 and st["duplicate"] == 1 and st["new"] == 3
        assert st["complete"] == 1 and st["incomplete"] == 2
        saqr = s.query(Company).filter_by(name="شركة الصقر للشحن للفحص").one()
        assert saqr.data_status == "complete" and saqr.lead_source == "directory:overture"
        cts = {c.kind: c for c in s.query(Contact).filter_by(company_id=saqr.id)}
        assert cts["phone"].value == "+966117730001" and cts["phone"].source_url.startswith("https://ovt-saqr-cargo.example")
        assert cts["email"].value == "sales@ovt-saqr-cargo.example"
        assert "+966557730001" not in [c.value for c in s.query(Contact).filter_by(company_id=saqr.id)]  # رقم Overture لا يُعتمد
        assert s.query(CompanySource).filter(CompanySource.company_id == saqr.id,
                                             CompanySource.url.like("https://explore.overturemaps.org/%")).count() >= 1
        stub = s.query(LeadStub).filter_by(name="شركة الصقر للشحن للفحص").one()
        assert stub.source == "overture" and stub.external_id == "overture:ov-1"
        assert not s.query(CostEntry).filter(CostEntry.note.like("dir:overture%")).count()  # بلا تكلفة
    # دفعة ثانية: لا تكرار
    with session_scope() as s:
        r2 = D.request(s, sources=["overture"], params={"activities": ["transport.freight"],
                                                         "scope": {"places": ["SA-01/riyadh"]}}, budget=5, user_id=None)
        rid2 = r2.id
    D.run(rid2, crawler=FakeCrawler())
    with session_scope() as s:
        st2 = s.get(DirectoryRun, rid2).stats["overture"]
        assert st2.get("new", 0) == 0 and st2["duplicate"] + st2["existing_customer"] == 5
        assert s.query(Company).filter_by(name="شركة الصقر للشحن للفحص").count() == 1
    # الإعداد اليومي: Overture مضاف وخرائط Google خارج الدفعات التي تحفظ
    with session_scope() as s:
        row = s.get(Setting, D.AUTO_KEY)
        saved = dict(row.value) if row else None
        D.set_auto(s, sources=["web_search", "google_places"])
        cfg = D.get_auto(s)
        assert cfg["sources"][0] == "overture" and "google_places" not in cfg["sources"] and cfg["target_complete"] == 200
        if saved is not None:
            s.get(Setting, D.AUTO_KEY).value = saved


_REAL_CLIENT = httpx.Client


def _google(handler):
    real = _REAL_CLIENT

    def make(*a, **k):
        k.pop("transport", None)
        return real(transport=httpx.MockTransport(handler), **k)
    return make


def test_google_live_search_displays_results_without_saving(monkeypatch):
    from mandoob import google_live as G
    seen = {}

    def handler(req: httpx.Request):
        if req.url.host == "places.googleapis.com":
            seen["key"] = req.headers.get("x-goog-api-key")
            seen["mask"] = req.headers.get("x-goog-fieldmask")
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"places": [
                {"id": "g1", "displayName": {"text": "شركة نقل جوجل الأولى"}, "formattedAddress": "حي السلي، الرياض",
                 "location": {"latitude": 24.65, "longitude": 46.80}, "nationalPhoneNumber": "011 222 3344",
                 "websiteUri": "https://g1.example", "googleMapsUri": "https://maps.google.com/?cid=1", "rating": 4.4,
                 "userRatingCount": 31, "businessStatus": "OPERATIONAL", "primaryTypeDisplayName": {"text": "شركة نقل"}},
                {"id": "g2", "displayName": {"text": "مؤسسة شحن جوجل الثانية"}, "location": {"latitude": 24.7, "longitude": 46.7},
                 "googleMapsUri": "https://maps.google.com/?cid=2"}]})
        if req.url.host == "maps.googleapis.com":
            seen["static"] = str(req.url)
            return httpx.Response(200, content=b"\x89PNG fake", headers={"content-type": "image/png"})
        return httpx.Response(404)
    monkeypatch.setattr(G.httpx, "Client", _google(handler))
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        monkeypatch.setattr(settings, "google_places_api_key", "")
        html = cl.get("/discover/google?activity=transport.freight&city=SA-01/riyadh").text
        assert "غير مضبوط" in html and "GOOGLE_PLACES_API_KEY" in html
        assert "بحث مباشر في خرائط Google" in cl.get("/discover").text
        monkeypatch.setattr(settings, "google_places_api_key", "AIza-test-key")
        before = None
        with session_scope() as s:
            before = s.query(Company).count()
        html = cl.get("/discover/google", params={"activity": "transport.freight", "city": "SA-01/riyadh",
                                                  "q": "شركات نقل في الرياض"}).text
        assert "شركة نقل جوجل الأولى" in html and "011 222 3344" in html and "فتح في Google Maps" in html
        assert "متصل" in html and "«شركات نقل في الرياض» — 2 نتيجة" in html
        assert "AIza-test-key" not in html  # المفتاح لا يصل للمتصفح
        assert seen["key"] == "AIza-test-key" and "places.location" in seen["mask"]
        assert seen["body"]["textQuery"] == "شركات نقل في الرياض" and seen["body"]["regionCode"] == "SA"
        assert "locationBias" in seen["body"]
        src = html.split('src="/discover/google/map?')[1].split('"')[0]
        img = cl.get("/discover/google/map?" + src.replace("&amp;", "&"))
        assert img.status_code == 200 and img.content.startswith(b"\x89PNG") and "markers=" in seen["static"]
        with session_scope() as s:
            assert s.query(Company).count() == before  # لا يُحفظ أي سجل من Google
            assert not s.query(Company).filter(Company.name.in_(["شركة نقل جوجل الأولى", "مؤسسة شحن جوجل الثانية"])).count()
            st = s.get(Setting, G.STATUS_KEY).value
            assert st["ok"] is True and st["count"] == 2 and "شركة نقل جوجل" not in json.dumps(st, ensure_ascii=False)
        # خطأ من Google يُشرح بوضوح
        monkeypatch.setattr(G.httpx, "Client", _google(lambda req: httpx.Response(
            403, json={"error": {"status": "PERMISSION_DENIED", "message": "Places API (New) has not been used in project"}})))
        html = cl.get("/discover/google", params={"q": "مصانع في الدمام", "city": "SA-04/dammam"}).text
        assert "Places API (New) غير مفعّلة" in html and "تعذّر" in html


def test_google_monthly_budget_blocks_and_alerts(monkeypatch):
    from mandoob import google_live as G
    from mandoob.costs import record_cost
    from mandoob.models import Notification
    calls = []
    monkeypatch.setattr(settings, "google_places_api_key", "AIza-test-key")
    monkeypatch.setattr(G.httpx, "Client", _google(lambda req: calls.append(1) or httpx.Response(200, json={"places": []})))
    with session_scope() as s:
        assert G.budget(s)["cap"] == 500.0  # السقف الافتراضي
        spent = G.budget(s)["spent"]
        G.set_budget(s, spent + 1.0)
    G.search("اختبار السقف")
    assert calls  # تحت السقف: يمر
    record_cost("google_places", 1, 2.0, note="google_live")
    n = len(calls)
    with pytest.raises(G.LiveError, match="سقفه الشهري"):
        G.search("بعد السقف")
    assert len(calls) == n  # لم يُرسل طلب لـGoogle
    with session_scope() as s:
        assert s.query(Notification).filter(Notification.title.like("بلغ بحث خرائط Google سقفه الشهري%")).count() >= 1
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        html = cl.get("/discover/google").text
        assert "الميزانية الشهرية لخرائط Google" in html and "حفظ السقف" in html
        r = cl.post("/discover/google/budget", data={"monthly_usd": "500"}, follow_redirects=True)
        assert "سقف خرائط Google الشهري الآن $500" in r.text
    with session_scope() as s:
        assert G.budget(s)["cap"] == 500.0


def test_signals_google_by_activity(monkeypatch):
    from mandoob import google_live as G
    from mandoob.models import Contact as Ct
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body["textQuery"])
        return httpx.Response(200, json={"places": [
            {"id": "x1", "displayName": {"text": "مؤسسة مياه نقية للفحص"}, "nationalPhoneNumber": "055 777 1201",
             "googleMapsUri": "https://maps.google.com/?cid=91", "location": {"latitude": 24.7, "longitude": 46.7}},
            {"id": "x2", "displayName": {"text": "منشأة موجودة لفحص جوجل"}, "nationalPhoneNumber": "055 777 1202",
             "googleMapsUri": "https://maps.google.com/?cid=92"}]})
    monkeypatch.setattr(settings, "google_places_api_key", "AIza-test-key")
    monkeypatch.setattr(G.httpx, "Client", _google(handler))
    with session_scope() as s:
        G.set_budget(s, 500)
        co = Company(name="منشأة موجودة لفحص جوجل", name_norm=normalize_name("منشأة موجودة لفحص جوجل"))
        s.add(co)
        s.flush()
        s.add(Ct(company_id=co.id, kind="phone", value="+966557771202", source_url="https://x.example"))
        n0 = s.query(Company).count()
    from mandoob.activities import ACTIVITIES
    ak = next(k for k, a in ACTIVITIES.items() if "مياه" in a["label"] or "وايت" in a["label"])
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        assert "ابحث في خرائط Google عن النشاط المختار" in cl.get("/signals").text
        html = cl.get("/signals/google", params=[("activities", ak), ("places", "SA-01/riyadh")]).text
        assert len(seen) == 1 and seen[0].endswith("في الرياض")
        assert "مؤسسة مياه نقية للفحص" in html and "جديدة" in html and "موجودة" in html and "+966557771201" in html
        assert "اختر في «النطاق»" in cl.get("/signals/google", params=[("activities", ak)]).text
        many = cl.get("/signals/google", params=[("activities", "__all__"), ("places", "SA-01/riyadh")]).text
        assert "اختر نشاطًا واحدًا على الأقل" in many and len(seen) == 1  # لا صرف على «جميع الأنشطة»
        grp = cl.get("/signals/google", params=[("activities", "group:land_freight"), ("activities", "group:logistics"),
                                                  ("places", "SA-01/riyadh"), ("places", "SA-02/jeddah")]).text
        assert "ضيّق الأنشطة أو المدن" in grp and len(seen) == 1
    with session_scope() as s:
        assert s.query(Company).count() == n0  # لا يُحفظ شيء من Google
