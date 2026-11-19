"""يضيف إلى بيانات العرض التجريبي حملة «الخدمات الميدانية والتوزيع — المنطقة الشرقية» (شركات تجريبية .example)
مع فئة اختبار، وطلب «اطلب عرضًا لأسطولك» من إحالة شريك. يُشغَّل بعد scripts/demo_trial.py."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def main() -> None:
    from sqlalchemy import select

    from fakes import SITES, FakeCrawler
    from mandoob.db import init_db, session_scope
    from mandoob.discovery.base import Candidate
    from mandoob.discovery import runner
    from mandoob.inbox import sync_all
    from mandoob.leads import create_lead
    from mandoob.mail.sandbox import SandboxProvider
    from mandoob.models import Campaign, Category, DiscoveryRun, Offer, OutboundMessage, User
    from mandoob.outreach import queue_offers, send_due
    from mandoob.timeutil import riyadh_today

    init_db()

    def page(domain, home, contact, about=""):
        SITES[f"https://{domain}"] = (f"<html><head><title>{domain}</title></head><body><p>{home}</p>"
                                      "<a href='/contact-us'>اتصل بنا</a><a href='/about'>من نحن</a></body></html>")
        SITES[f"https://{domain}/contact-us"] = f"<html><body>{contact}</body></html>"
        SITES[f"https://{domain}/about"] = f"<html><body><p>{about}</p></body></html>"

    page("cool-air.example", "شركة صيانة تكييف مركزي. لدينا فرق صيانة متنقلة تغطي الدمام والخبر وعقود صيانة دورية للمجمعات.",
         "<p>هاتف 0138001122</p><a href='mailto:info@cool-air.example'>info</a> <a href='mailto:operations@cool-air.example'>o</a>",
         "افتتاح فرع جديد في الجبيل الصناعية بتاريخ 2026-10-05 لخدمة المصانع.")
    page("lift-pro.example", "صيانة وتركيب المصاعد. خدمة طوارئ على مدار الساعة وفنيين متنقلين في المنطقة الشرقية.",
         "<p>0138004455</p><a href='mailto:maintenance@lift-pro.example'>m</a>",
         "مطلوب سائقين ومشرف أسطول لفرق الصيانة — 2026-11-02")
    page("fresh-dairy.example", "مصنع ألبان وتوزيع مواد غذائية. يتم التوصيل عن طريق شركة الريان للنقل المبرد إلى المتاجر.",
         "<p>0138003344</p><a href='mailto:procurement@fresh-dairy.example'>p</a>")
    page("sweet-water.example", "شركة مياه شرب. نمتلك أسطول توزيع من 120 مركبة وتوزيع يومي على المتاجر في الشرقية.",
         "<p>920033445</p><a href='mailto:fleet@sweet-water.example'>fleet</a>")
    page("ac-shop.example", "معرض لبيع أجهزة التكييف المنزلية بأفضل الأسعار.",
         "<p>0138002233</p><a href='mailto:sales@ac-shop.example'>sales</a>")

    with session_scope() as s:
        if s.scalar(select(Campaign).where(Campaign.name.like("الخدمات الميدانية%"))):
            print("موجودة مسبقًا")
            return
        rep = s.scalar(select(User).where(User.email == "fahad@sayaraup.test"))
        if rep:
            rep.sectors = ["transport_logistics", "refrigerated_transport", "field_services", "distribution_supply"]
        fuel = s.scalar(select(Offer).where(Offer.key == "fuel-management"))
        fuel.coverage = ["المنطقة الشرقية", "الرياض"]
        camp = Campaign(name="الخدمات الميدانية والتوزيع — المنطقة الشرقية", status="active", daily_target=60,
                        send_enabled=True, test_recipient="sales-test@sayaraup.test",
                        request_text="ابحث عن شركات لديها فرق ميدانية تزور العملاء يوميًا في المنطقة الشرقية، "
                                     "وخصص ٢٠ شركة لاختبار شركات التوزيع والتوريد")
        s.add(camp)
        s.flush()
        fs = Category(campaign_id=camp.id, sector_key="field_services", label="شركات الخدمات الميدانية",
                      cities=["الدمام", "الخبر", "الجبيل الصناعية"], keywords_ar=["فرق صيانة متنقلة"], quota=40,
                      offer_id=fuel.id, priority_signals=["تدير أسطول مركبات"])
        ds = Category(campaign_id=camp.id, sector_key="distribution_supply", label="شركات التوزيع والتوريد",
                      cities=["الدمام"], keywords_ar=["أسطول التوزيع"], quota=20, offer_id=fuel.id, is_experiment=True)
        s.add_all([fs, ds])
        s.flush()
        run = DiscoveryRun(run_date=riyadh_today(), campaign_id=camp.id, target=60, status="partial", checkpoint={},
                           per_category={}, per_source={}, source_errors={}, log=[])
        s.add(run)
        s.flush()
        ids = (camp.id, fs.id, ds.id, run.id)
    camp_id, fs_id, ds_id, run_id = ids

    def cand(name, domain):
        return Candidate(name=name + " (تجريبية)", source_type="google_places", source_url=f"https://maps.example/{domain}",
                         city="الدمام", website=f"https://{domain}")
    crawler = FakeCrawler()
    for cat_id, items in [(fs_id, [("كول إير لصيانة التكييف", "cool-air.example"), ("ليفت برو للمصاعد", "lift-pro.example"),
                                   ("معرض التكييف", "ac-shop.example")]),
                          (ds_id, [("ألبان الطازج", "fresh-dairy.example"), ("مياه سويت", "sweet-water.example")])]:
        for n, d in items:
            print(n, "→", runner._handle_candidate(run_id, cat_id, cand(n, d), crawler, riyadh_today()))
    print(queue_offers(camp_id), send_due(camp_id, ignore_window=True))
    sync_all()
    with session_scope() as s:
        m = s.scalar(select(OutboundMessage).join(OutboundMessage.company)
                     .where(OutboundMessage.campaign_id == camp_id).order_by(OutboundMessage.id))
        SandboxProvider.inject_reply(from_addr=m.to_address, original_message_id=m.internet_message_id,
                                     thread_id=m.thread_id, subject=m.subject,
                                     text="نعم يهمنا، عندنا ٤٠ سيارة صيانة. نبغى اجتماع الثلاثاء الصباح")
    sync_all()
    with session_scope() as s:
        create_lead(s, company_name="الواحة للتوزيع (تجريبية)", city="الخبر", vehicles=35, activity="distribution_supply",
                    offer_id=None, service_text="", contact_name="سالم", phone="0551112233",
                    email="salem@wahaa-dist.example", preferred_channel="whatsapp", source="partner:tracker-co")
    print("تمت إضافة بيانات الشرائح")


if __name__ == "__main__":
    main()
