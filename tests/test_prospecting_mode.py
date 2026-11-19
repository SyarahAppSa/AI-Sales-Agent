"""المرحلة الحالية: المنصة تبحث وتجمع البيانات فقط، والفريق يتواصل بنفسه.

- الاتصال الآلي والإرسال التلقائي معطّلان (الكود محفوظ خلف إعداد) — وكود الدخول يصل كالمعتاد.
- أداة «البحث عن العملاء»: المدن والمناطق والأنشطة والهدف اليومي، والتشغيل الفوري عبر العامل أو المجدول.
- الإسناد الجماعي دون تعارض بين موظفَين، وبوابة الموظف: النسخ والتصدير وتحديث الحالة، وتقرير العروض.
"""
import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from helpers import ensure_mail, otp_login
from mandoob.config import settings
from mandoob.db import init_db, session_scope
from mandoob.models import Call, Campaign, Category, Company, Contact, OutboundMessage, User
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
R1, R2 = "fahad.rep@syarahapp.test", "sara.rep@syarahapp.test"
S = {}


@pytest.fixture(autouse=True)
def current_phase(monkeypatch):
    """الوضع الحالي: لا مكالمات ولا إرسال تلقائي."""
    monkeypatch.setattr(settings, "voice_calls_enabled", False)
    monkeypatch.setattr(settings, "auto_outreach_enabled", False)


def setup_module():
    init_db()
    ensure_mail()
    with session_scope() as s:
        ids = []
        for i, (name, dom, city) in enumerate([("شركة المسار للنقل", "masar-trans.example", "الرياض"),
                                               ("مؤسسة الوفاق للتوصيل", "wefaq-deliv.example", "جدة"),
                                               ("مصنع الندى للمياه", "nada-water.example", "الدمام")]):
            c = Company(name=name, name_norm=f"pm-{i}", activity="نقل وتوصيل", city=city, website=f"https://{dom}",
                        domain=dom, data_status="complete", lead_source="discovery", stage="new")
            s.add(c)
            s.flush()
            c.contacts.append(Contact(kind="phone", value=f"+96655000010{i}", source_url=f"https://{dom}/contact",
                                      verified=True, preferred=True))
            c.contacts.append(Contact(kind="email", value=f"info@{dom}", source_url=f"https://{dom}/contact",
                                      verified=True, preferred=True))
            ids.append(c.id)
        S["ids"] = ids


def test_1_calls_and_auto_outreach_are_off_but_login_code_still_arrives():
    from mandoob import jobs
    from mandoob.outreach import queue_followups, queue_offers, send_due
    from mandoob.voice import check_call_eligibility, start_call
    with session_scope() as s:
        camp = Campaign(name="حملة قديمة بإرسال مفعّل", status="active", send_enabled=True)
        s.add(camp)
        s.flush()
        s.add(Category(campaign_id=camp.id, sector_key="delivery", label="x", cities=[], quota=5))
        s.get(Company, S["ids"][0]).campaign_id = camp.id
        cid = camp.id
    assert queue_offers(cid)["queued"] == 0
    assert send_due(ignore_window=True) == {"sent": 0, "failed": 0, "held": 0}
    assert queue_followups() == 0 and jobs.outreach_tick() == {}
    with session_scope() as s:
        c = s.get(Company, S["ids"][0])
        ok, why = check_call_eligibility(s, c)
        assert not ok and "معطّل" in why
        with pytest.raises(PermissionError):
            start_call(s, c, service_name="x", offer_points="", rep_name="")
        c.campaign_id = None
        s.get(Campaign, cid).status = "paused"
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")  # كود الدخول يصل بالبريد كالمعتاد
        page = c.get(f"/companies/{S['ids'][0]}").text
        assert "المساعد الصوتي" not in page and "حجز اجتماع" not in page
        r = c.post(f"/companies/{S['ids'][0]}/action", data={"action": "ai_call"}, follow_redirects=False)
        assert r.status_code == 303
        assert c.post("/webhooks/voice", content=b"{}").status_code == 404
        assert c.post("/outreach/run").status_code == 403
        assert c.post("/settings/voice", data={"enabled": "on"}).status_code == 403
        assert "صندوق التجربة" not in c.get("/").text
    with session_scope() as s:
        assert s.scalar(select(func.count(Call.id))) == 0
        assert s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.company_id.in_(S["ids"]))) == 0


def test_2_prospecting_tool_saves_target_and_runs_on_the_worker(monkeypatch):
    from mandoob import jobs, prospecting
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        r = c.get("/prospecting")
        assert r.status_code == 200 and "البحث عن العملاء" in r.text and "الهدف اليومي" in r.text
        assert "جميع الأنشطة في السعودية" in r.text and "ابحث عن نشاط" in r.text and "مدارس أهلية" in r.text
        # قطاع كامل (التعليم) + نشاط محدد (المغاسل) + قطاعان آخران
        r = c.post("/prospecting", data={"activities": ["sector:education", "community.laundries",
                                                        "industry.food_factories", "field_services.hvac",
                                                        "food.restaurants"],
                                         "scope_mode": "custom", "places": ["SA-01/riyadh", "SA-02/jeddah"], "regions": ["SA-04"],
                                         "daily_target": "400", "scheduled": "1", "time": "05:30"},
                   follow_redirects=False)
        assert r.status_code == 303
    with session_scope() as s:
        cfg = prospecting.get_config(s)
        camp = s.get(Campaign, cfg["campaign_id"])
        cats = [x for x in camp.categories if x.active]
        assert camp.daily_target == 400 and sum(x.quota for x in cats) == 400 and len(cats) == 5
        edu = next(x for x in cats if x.sector_key == "education")
        assert "مدارس أهلية" in edu.keywords_ar and "حضانة أطفال" in edu.keywords_ar  # القطاع كاملًا
        com = next(x for x in cats if x.sector_key == "community")
        assert com.keywords_ar[0].startswith("مغسلة") and not any("خيرية" in q for q in com.keywords_ar)  # نشاط وحده
        assert camp.status == "active" and camp.send_enabled is False
        assert "الدمام" in cats[0].cities and "الجبيل" in cats[0].cities and "الرياض" in cats[0].cities
        assert jobs.get_setting("schedule")["discovery_time"] == "05:30"
        cid = camp.id
    # «ابحث الآن»: طلب يلتقطه العامل على الخادم (لا يعتمد على المتصفح)
    calls = []
    import mandoob.discovery.runner as runner
    monkeypatch.setattr(runner, "run_campaign_discovery", lambda cid, **kw: calls.append((cid, kw)) or 99)
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        c.post("/prospecting/run", data={"batch": "5"})
        assert "بانتظار الخادم" in c.get("/prospecting").text
    assert jobs.discovery_request_tick() is True
    assert calls == [(cid, {"max_new": 5, "force": True})]
    with session_scope() as s:
        req = prospecting.pending_request(s)
        assert req["state"] == "done" and req["run_id"] == 99
    assert jobs.discovery_request_tick() is False  # لا يُعاد تشغيل الطلب نفسه
    with TestClient(app) as c:  # إيقاف الجدولة حتى لا تؤثر على بقية الاختبارات
        otp_login(c, ADMIN, "admin")
        c.post("/prospecting", data={"activities": ["transport"], "scope_mode": "custom", "places": ["SA-01/riyadh"], "daily_target": "400",
                                     "time": "06:00"})
    with session_scope() as s:
        assert s.get(Campaign, cid).status == "paused"
    # «جميع الأنشطة في السعودية»: كل القطاعات، والهدف موزع عليها
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        c.post("/prospecting", data={"all_activities": "1", "scope_mode": "custom", "places": ["SA-01/riyadh"],
                                     "daily_target": "400", "time": "06:00"})
    with session_scope() as s:
        from mandoob.activities import ACTIVITIES, SECTORS
        cfg = prospecting.get_config(s)
        cats = [x for x in s.get(Campaign, cid).categories if x.active]
        assert cfg["all"] and len(cfg["activities"]) == len(ACTIVITIES) and len(cats) == len(SECTORS)
        assert sum(x.quota for x in cats) == 400 and max(x.quota for x in cats) - min(x.quota for x in cats) <= 1


def test_3_bulk_assign_portal_copy_export_status_and_offers_report():
    a, b, c3 = S["ids"]
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        c.post("/staff/add", data={"name": "فهد", "email": R1, "profile": "custom", "customize": "on", "scope": "own",
                                   "see_phone": "on", "see_email": "on", "update_status": "on", "add_note": "on",
                                   "export": "on"})
        c.post("/staff/add", data={"name": "سارة", "email": R2, "profile": "sales_rep"})
        with session_scope() as s:
            S["r1"] = s.scalar(select(User.id).where(User.email == R1))
            S["r2"] = s.scalar(select(User.id).where(User.email == R2))
        page = c.get("/companies?status=complete&kind=real").text
        assert 'name="company_ids"' in page and "إسناد المحدد" in page
        r = c.post("/companies/assign", data={"owner_id": str(S["r1"]), "company_ids": [str(a), str(b), str(c3)]},
                   follow_redirects=False)
        assert r.status_code == 303
        # لا يُنقل المسند لموظف آخر دون تأكيد صريح — يمنع تواصل موظفَين مع المنشأة نفسها
        c.post("/companies/assign", data={"owner_id": str(S["r2"]), "company_ids": [str(a)]})
        with session_scope() as s:
            assert s.get(Company, a).owner_id == S["r1"]
        c.post("/companies/assign", data={"owner_id": str(S["r2"]), "company_ids": [str(c3)], "reassign": "1"})
        with session_scope() as s:
            assert s.get(Company, c3).owner_id == S["r2"] and s.get(Company, a).assigned_at is not None

    with TestClient(app) as c:
        otp_login(c, R1, "portal")
        home = c.get("/portal").text
        assert "شركة المسار للنقل" in home and "مؤسسة الوفاق للتوصيل" in home and "مصنع الندى" not in home
        assert "+966 55 000 0100" in home or "550000100" in home.replace(" ", "")
        assert "نسخ كل الأرقام" in home and "https://masar-trans.example/contact" in home
        x = c.get("/portal/export.xlsx")
        assert x.status_code == 200
        from openpyxl import load_workbook
        rows = [[str(v or "") for v in r] for r in load_workbook(io.BytesIO(x.content)).active.iter_rows(values_only=True)]
        flat = " ".join(" ".join(r) for r in rows)
        assert "شركة المسار للنقل" in flat and "info@masar-trans.example" in flat and "https://masar-trans.example/contact" in flat
        assert "مصنع الندى" not in flat
        # الموظف تواصل بنفسه ثم سجّل الحالة
        r = c.post(f"/portal/companies/{a}/status", data={"stage": "offer_sent", "offer_at": "2026-11-19T10:30",
                                                          "offer_note": "عرض تأجير عبر البريد"}, follow_redirects=False)
        assert r.status_code == 303
        c.post(f"/portal/companies/{b}/status", data={"stage": "needs_followup", "next_title": "اتصال ثانٍ",
                                                      "next_at": "2026-11-22T11:00"})
        assert c.get(f"/portal/companies/{c3}").status_code == 404  # ليست له
    with session_scope() as s:
        ca, cb = s.get(Company, a), s.get(Company, b)
        assert ca.stage == "offer_sent" and ca.offer_sent_by_id == S["r1"] and ca.offer_sent_at.hour == 7  # 10:30 الرياض
        assert cb.stage == "needs_followup" and cb.contacted_by_id == S["r1"]
    with TestClient(app) as c:  # موظفة بلا صلاحية تصدير
        otp_login(c, R2, "portal")
        assert c.get("/portal/export.xlsx").status_code == 403
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        rep = c.get("/offers-report").text
        assert "عروض أرسلها الموظفون" in rep and "شركة المسار للنقل" in rep and "فهد" in rep
        assert "عرض تأجير عبر البريد" in rep
        x = c.get("/offers-report.xlsx")
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(x.content))
        assert "عروض أرسلها الموظفون" in wb.sheetnames
        vals = " ".join(str(v or "") for r in wb["عروض أرسلها الموظفون"].iter_rows(values_only=True) for v in r)
        assert "شركة المسار للنقل" in vals and "فهد" in vals
    with session_scope() as s:  # صفر رسائل وصفر مكالمات طوال المسار
        assert s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.company_id.in_(S["ids"]))) == 0
        assert s.scalar(select(func.count(Call.id))) == 0


def test_4_display_hides_contacts_that_do_not_belong_to_the_company():
    with session_scope() as s:
        c = s.get(Company, S["ids"][0])
        c.contacts.append(Contact(kind="email", value="dev@webagency.example", source_url="https://masar-trans.example/",
                                  verified=True, preferred=True))
        s.flush()
        assert c.primary("email").value == "info@masar-trans.example"


def test_5_stalled_request_resumes_but_live_one_is_left_alone(monkeypatch):
    """عامل أُوقف أثناء التحديث: نبضه ينقطع فيُستأنف الطلب؛ أما الطلب الحي (نبض حديث) فلا يُمس."""
    from mandoob import jobs, prospecting
    from mandoob.models import JobLock
    from mandoob.timeutil import now_utc
    from datetime import timedelta as td
    import mandoob.discovery.runner as runner
    ran = []
    monkeypatch.setattr(runner, "run_campaign_discovery", lambda cid, **kw: ran.append(cid) or 7)
    with session_scope() as s:
        lk = s.get(JobLock, "discovery")
        if lk is None:
            s.add(JobLock(name="discovery", locked_until=now_utc() + td(hours=9), holder="old-worker:1"))
        else:
            lk.locked_until, lk.holder = now_utc() + td(hours=9), "old-worker:1"
        prospecting._put(s, prospecting.REQUEST, {"state": "running", "campaign_id": 1, "max_new": 5,
                                                  "heartbeat": now_utc().isoformat(timespec="seconds")})
    assert jobs.discovery_request_tick() is False and ran == []  # حي: لا يُمس
    with session_scope() as s:
        prospecting._put(s, prospecting.REQUEST, {"state": "running", "campaign_id": 1, "max_new": 5,
                                                  "heartbeat": (now_utc() - td(minutes=10)).isoformat(timespec="seconds")})
    assert jobs.recover_on_start() is True
    assert jobs.discovery_request_tick() is True and ran == [1]  # متوقف: يُستأنف رغم قفل العامل السابق
    with session_scope() as s:
        r = prospecting.pending_request(s)
        assert r["state"] == "done" and r.get("resumed_after_stall")
        prospecting._put(s, prospecting.REQUEST, {"state": "done"})
