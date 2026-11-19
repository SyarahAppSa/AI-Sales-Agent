"""الاستهداف حسب طريقة تشغيل المركبات (القسم ٩)."""
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from fakes import FakeCrawler, ListSource, SITES
from mandoob.auth import hash_password
from mandoob.db import init_db, session_scope
from mandoob.discovery.base import Candidate
from mandoob.discovery.runner import run_campaign_discovery
from mandoob.models import (Campaign, Category, Company, CompanyRelation, CompanySignal, Consent, InboundLead,
                            Meeting, Offer, OutboundMessage, Task, User)
from mandoob.outreach import queue_offers, send_due
from mandoob.reports import weekly_segments
from mandoob.timeutil import now_utc, riyadh_today

S = {}


def page(domain, home_text, contact_text, extra_pages=None):
    SITES[f"https://{domain}"] = (f"<html><head><title>{domain}</title></head><body><p>{home_text}</p>"
                                  f"<a href='/contact-us'>اتصل بنا</a><a href='/about'>من نحن</a></body></html>")
    SITES[f"https://{domain}/contact-us"] = f"<html><body>{contact_text}</body></html>"
    SITES[f"https://{domain}/about"] = f"<html><body><p>{(extra_pages or '')}</p></body></html>"


def c(name, domain, city="الدمام", pid=None):
    return Candidate(name=name, source_type="google_places", source_url=f"https://maps.example/{pid or domain}",
                     external_id=pid, city=city, website=f"https://{domain}" if domain else None)


def setup_module():
    init_db()
    page("cool-air.example", "شركة صيانة تكييف مركزي. لدينا فرق صيانة متنقلة تغطي الدمام والخبر وعقود صيانة دورية.",
         "<p>هاتف 0138001122</p><a href='mailto:info@cool-air.example'>info</a> "
         "<a href='mailto:operations@cool-air.example'>operations</a>",
         "افتتاح فرع جديد في الجبيل الصناعية بتاريخ 2026-10-05 لخدمة المصانع.")
    page("ac-shop.example", "معرض لبيع أجهزة التكييف المنزلية بأفضل الأسعار.",
         "<p>0138002233</p><a href='mailto:sales@ac-shop.example'>sales</a>")
    page("fresh-dairy.example", "مصنع ألبان وتوزيع مواد غذائية. يتم التوصيل عن طريق شركة الريان للنقل المبرد إلى المتاجر.",
         "<p>0138003344</p><a href='mailto:procurement@fresh-dairy.example'>p</a>")
    page("sweet-water.example", "شركة مياه شرب. نمتلك أسطول توزيع من 120 مركبة وتوزيع يومي على المتاجر في الشرقية.",
         "<p>920033445</p><a href='mailto:fleet@sweet-water.example'>fleet</a>")
    page("cool-air-riyadh.example", "النخبة لصيانة التكييف — فرق صيانة متنقلة في الرياض.",
         "<p>0114005566</p><a href='mailto:info@cool-air-riyadh.example'>i</a>")
    page("nokhba-dmm.example", "النخبة لصيانة التكييف فرع الدمام — فرق صيانة متنقلة.",
         "<p>0138006677</p><a href='mailto:info@nokhba-dmm.example'>i</a>")
    with session_scope() as s:
        from mandoob.models import MailAccount
        if not s.scalar(select(MailAccount).where(MailAccount.active.is_(True))):
            s.add(MailAccount(provider="sandbox", sender_address="sales@sayaraup.test", sender_name="سيارة آب"))
        s.add(User(name="فهد الشرقية", email="fahad.east@sayaraup.test", password_hash=hash_password("x"), role="rep",
                   sectors=["field_services", "distribution_supply"], cities=[]))
        east = Offer(key="east-fuel", service_name="إدارة استهلاك الوقود — الشرقية", subject_template="{service} لـ {company}",
                     approved_body="السلام عليكم فريق {company}", approved=True, coverage=["المنطقة الشرقية"])
        s.add(east)
        s.flush()
        camp = Campaign(name="الخدمات الميدانية والتوزيع — الشرقية", status="active", daily_target=40, send_enabled=True,
                        test_recipient="test@sayaraup.test")
        s.add(camp)
        s.flush()
        fs = Category(campaign_id=camp.id, sector_key="field_services", label="شركات الخدمات الميدانية",
                      cities=["الدمام"], keywords_ar=["شركة صيانة تكييف"], quota=20, offer_id=east.id, is_experiment=True)
        ds = Category(campaign_id=camp.id, sector_key="distribution_supply", label="شركات التوزيع والتوريد",
                      cities=["الدمام"], keywords_ar=["شركة توزيع"], quota=20, offer_id=east.id)
        s.add_all([fs, ds])
        s.flush()
        S.update(camp=camp.id, fs=fs.id, ds=ds.id, east=east.id)


def _run(cands_by_cat):
    # تشغيل لكل فئة على حدة بمصدر خاص بها
    from mandoob.discovery import runner
    for cat_id, cands in cands_by_cat.items():
        for cand in cands:
            runner._handle_candidate(S.setdefault("run", _run_id()), cat_id, cand, FakeCrawler(), riyadh_today())


def _run_id():
    from mandoob.models import DiscoveryRun
    with session_scope() as s:
        r = DiscoveryRun(run_date=riyadh_today() - timedelta(days=30), campaign_id=S["camp"], checkpoint={},
                         per_category={}, per_source={}, source_errors={}, log=[])
        s.add(r)
        s.flush()
        return r.id


def test_1_field_service_by_operational_phrase():
    _run({S["fs"]: [c("كول إير للتكييف", "cool-air.example"), c("معرض التكييف", "ac-shop.example")]})
    with session_scope() as s:
        co = s.scalar(select(Company).where(Company.domain == "cool-air.example"))
        assert co.data_status == "complete" and co.sector_key == "field_services"
        assert co.fit_phrase == "فرق صيانة متنقلة" and co.fit_evidence_url == "https://cool-air.example"
        assert "فرق صيانة متنقلة" in co.fit_reason
        assert co.decision_unit == "إدارة التشغيل والصيانة"
        em = co.primary("email")
        assert em.value == "operations@cool-air.example" and em.label == "التشغيل"  # بريد الإدارة الأقرب للقرار
        # متجر تكييف بلا دليل على فرق ميدانية: لا يُضاف
        assert s.scalar(select(Company).where(Company.domain == "ac-shop.example")) is None
        sig = s.scalar(select(CompanySignal).where(CompanySignal.company_id == co.id))
        assert sig.kind == "new_city" and str(sig.announced_on) == "2026-10-05"
        assert sig.url == "https://cool-air.example/about"
        assert co.signal_boost > 0
        S["cool"] = co.id


def test_2_operator_contracted_creates_carrier_and_self_operated():
    _run({S["ds"]: [c("ألبان الطازج", "fresh-dairy.example"), c("مياه سويت", "sweet-water.example")]})
    with session_scope() as s:
        dairy = s.scalar(select(Company).where(Company.domain == "fresh-dairy.example"))
        assert dairy.operator_status == "contracted"
        rel = s.scalar(select(CompanyRelation).where(CompanyRelation.company_id == dairy.id,
                                                     CompanyRelation.kind == "carrier"))
        carrier = rel.related
        assert "الريان للنقل" in carrier.name and carrier.lead_source == f"carrier_of:{dairy.id}"
        assert carrier.data_status == "incomplete" and rel.source_url == "https://fresh-dairy.example"
        water = s.scalar(select(Company).where(Company.domain == "sweet-water.example"))
        assert water.operator_status == "self" and water.fit_phrase
        assert water.primary("email").value == "fleet@sweet-water.example"


def test_3_branch_links_to_headquarters_and_is_not_counted():
    _run({S["fs"]: [c("النخبة لصيانة التكييف", "cool-air-riyadh.example", city="الرياض"),
                    c("النخبة لصيانة التكييف - فرع الدمام", "nokhba-dmm.example", city="الدمام")]})
    with session_scope() as s:
        hq = s.scalar(select(Company).where(Company.domain == "cool-air-riyadh.example"))
        br = s.scalar(select(Company).where(Company.domain == "nokhba-dmm.example"))
        assert br.parent_id == hq.id and br.counted_on is None
        assert s.scalar(select(CompanyRelation).where(CompanyRelation.company_id == br.id,
                                                      CompanyRelation.kind == "branch"))


def test_4_coverage_and_signal_priority_in_sending():
    stats = queue_offers(S["camp"])
    with session_scope() as s:
        msgs = list(s.scalars(select(OutboundMessage).where(OutboundMessage.campaign_id == S["camp"])
                              .order_by(OutboundMessage.id)))
        domains = [m.company.domain for m in msgs]
        # الرياض خارج نطاق عرض «الشرقية» — والفرع لا يُرسل له
        assert "cool-air-riyadh.example" not in domains and "nokhba-dmm.example" not in domains
        assert domains[0] == "cool-air.example"  # إشارة التوسع أعطتها أولوية
        assert set(domains) == {"cool-air.example", "fresh-dairy.example", "sweet-water.example"}
    assert any("نطاق" in k for k in stats["skipped"])
    assert send_due(S["camp"], ignore_window=True)["sent"] == 3


def test_5_request_quote_page_routes_to_specialist():
    from mandoob.web.app import app
    with TestClient(app) as cl:
        r = cl.get("/request-quote?ref=tracker-co")
        assert r.status_code == 200 and "اطلب عرضًا لأسطولك" in r.text
        r = cl.post("/request-quote", data={"company_name": "شركة الواحة للتوزيع", "city": "الخبر", "vehicles": "35",
                                            "activity": "distribution_supply", "offer_id": "", "contact_name": "سالم",
                                            "phone": "0551112233", "email": "salem@wahaa-dist.example",
                                            "preferred_channel": "whatsapp", "consent": "on", "ref": "tracker-co"},
                    follow_redirects=False)
        assert r.status_code == 303 and "done=1" in r.headers["location"]
        # البرامج الآلية (الحقل المخفي) لا تُسجَّل
        cl.post("/request-quote", data={"company_name": "spam", "city": "x", "contact_name": "x", "phone": "0551112299",
                                        "consent": "on", "website_url": "http://spam"})
    with session_scope() as s:
        leads = list(s.scalars(select(InboundLead)))
        assert len(leads) == 1 and leads[0].source == "partner:tracker-co"
        co = s.get(Company, leads[0].company_id)
        assert co.lead_source == "partner:tracker-co" and co.vehicles_estimate == 35 and co.stage == "interested"
        assert co.owner.name == "فهد الشرقية"  # المندوب المختص بالشريحة
        task = s.scalar(select(Task).where(Task.company_id == co.id))
        assert "واتساب" in task.title and "35" in task.details
        assert s.scalar(select(Consent).where(Consent.company_id == co.id, Consent.basis == "explicit"))


def test_6_weekly_report_meetings_per_100():
    with session_scope() as s:
        co = s.get(Company, S["cool"])
        owner = s.scalar(select(User).where(User.name == "فهد الشرقية"))
        s.add(Meeting(company_id=co.id, owner_id=owner.id, starts_at=now_utc(), ends_at=now_utc()))
    with session_scope() as s:
        w = weekly_segments(s)
        rows = dict(w["rows"])
        fs = rows["field_services"]
        assert fs["contacted"] == 1 and fs["meetings"] == 1 and fs["meetings_per_100"] == 100.0
        assert fs["experiment"] is True and fs["small_sample"] is True
        assert rows["distribution_supply"]["contacted"] == 2 and rows["distribution_supply"]["meetings_per_100"] == 0
        assert w["split"]["experiment"] >= 20
        assert w["leads"].get("إحالة شريك: tracker-co") == 1


def test_7_region_targeting_parse():
    from mandoob.targeting import parse_request_rules
    r = parse_request_rules("ابحث عن شركات لديها فرق ميدانية تزور العملاء يوميًا في المنطقة الشرقية مع المدن الصناعية")
    cat = r["categories"][0]
    assert cat["sector_key"] == "field_services"
    assert "الدمام" in cat["cities"] and "الجبيل الصناعية" in cat["cities"]
    assert any("فرق صيانة متنقلة" in k for k in cat["keywords_ar"])
