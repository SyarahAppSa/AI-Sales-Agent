"""تجربة كاملة قبل الإطلاق: فئة واحدة (تأجير السيارات) ومدينة واحدة (الرياض).

الوضعان:
  --offline  (افتراضي) مصادر وزاحف محليان بشركات تجريبية واضحة (نطاقات .example) — للتحقق من كل المسارات
             بدون شبكة ولا مفاتيح. البريد عبر «صندوق التجربة».
  --live     مصادر حقيقية (Google Places + Brave + Claude) وزحف مواقع الشركات الحقيقية، والإرسال من البريد
             المرتبط فعليًا إلى بريد نملكه (--to). يتطلب المفاتيح والشبكة.

الخطوات: اكتشاف ← عرض الشركات ومصادرها ← إرسال العرض بالمرفق إلى بريدنا ← رد «مهتم، اتصلوا بي الأحد بعد الظهر»
← ظهور الرد ومهمة المندوب ← إعادة التشغيل والتأكد من عدم التكرار ← تقرير «الشركات التي أُرسلت إليها عروض».
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--to", help="بريد نملكه لاستلام العروض في التجربة", default="sales-test@sayaraup.test")
    ap.add_argument("--quota", type=int, default=5)
    args = ap.parse_args()

    from sqlalchemy import func, select

    from mandoob.auth import ensure_admin, hash_password
    from mandoob.config import settings
    from mandoob.db import init_db, session_scope
    from mandoob.discovery.runner import run_campaign_discovery
    from mandoob.inbox import sync_all
    from mandoob.mail.sandbox import SandboxProvider
    from mandoob.models import (Attachment, Campaign, Category, Company, DiscoveryRun, MailAccount, Offer,
                                OutboundMessage, Task, User)
    from mandoob.normalize import format_phone_display
    from mandoob.outreach import queue_offers, send_due
    from mandoob.reports import OfferFilters, offers_report
    from mandoob.timeutil import fmt, riyadh_today
    from mandoob.targeting import parse_request

    init_db()
    ensure_admin(settings.admin_email, settings.admin_password)
    say = lambda *a: print(*a, flush=True)  # noqa: E731

    # ---------------------------------------------------------------- الإعداد
    with session_scope() as s:
        if not s.scalar(select(User).where(User.role == "manager")):
            s.add_all([
                User(name="مدير المبيعات", email="manager@sayaraup.test", password_hash=hash_password("demo1234"),
                     role="manager"),
                User(name="سارة العتيبي", email="sara@sayaraup.test", password_hash=hash_password("demo1234"),
                     role="rep", sectors=["car_rental"], cities=["الرياض"]),
                User(name="فهد القحطاني", email="fahad@sayaraup.test", password_hash=hash_password("demo1234"),
                     role="rep", sectors=["transport_logistics", "refrigerated_transport"]),
                User(name="نورة الشهري", email="noura@sayaraup.test", password_hash=hash_password("demo1234"),
                     role="rep", sectors=["delivery"], cities=["جدة", "الدمام"]),
            ])
        pdf = settings.data_dir / "sayaraup-vehicle-expenses.pdf"
        if not pdf.exists():
            pdf.write_bytes(_sample_pdf())
        if not s.scalar(select(Offer).where(Offer.key == "vehicle-expense-tracking")):
            att = Attachment(title="الملف التعريفي — متابعة مصروفات المركبات", filename="sayaraup-vehicle-expenses.pdf",
                             data=pdf.read_bytes(), size_bytes=pdf.stat().st_size, sha256="demo")
            att2 = Attachment(title="الملف التعريفي — إدارة استهلاك الوقود", filename="sayaraup-fuel-management.pdf",
                              data=pdf.read_bytes(), size_bytes=pdf.stat().st_size, sha256="demo")
            s.add_all([att, att2])
            s.flush()
            s.add_all([
                Offer(key="vehicle-expense-tracking", service_name="متابعة مصروفات المركبات",
                      subject_template="متابعة مصروفات أسطول {company}",
                      value_points="- لوحة واحدة لمصروفات كل مركبة (وقود، صيانة، مخالفات، تجديدات)\n"
                                   "- تنبيهات قبل مواعيد الصيانة وانتهاء الوثائق\n- تقارير شهرية للتكلفة لكل مركبة",
                      approved_body="السلام عليكم فريق {company}،\n\nنعرف أن شركات {activity} في {city} تتابع مصروفات "
                                    "عشرات المركبات يوميًا. منصة سيارة اب تجمع مصروفات كل مركبة — الوقود والصيانة "
                                    "والمخالفات والتجديدات — في لوحة واحدة، مع تنبيهات قبل مواعيد الصيانة وانتهاء الوثائق.\n\n"
                                    "أرفقنا ملفًا تعريفيًا مختصرًا.",
                      attachment_id=att.id, approved=True),
                Offer(key="fuel-management", service_name="إدارة استهلاك الوقود",
                      subject_template="خفض تكلفة الوقود لأسطول {company}",
                      value_points="- رصد استهلاك الوقود لكل مركبة وسائق\n- كشف الاستهلاك غير الطبيعي",
                      approved_body="السلام عليكم فريق {company}،\n\nتساعد سيارة اب شركات {activity} في {city} على رصد "
                                    "استهلاك الوقود لكل مركبة وسائق وكشف الاستهلاك غير الطبيعي مبكرًا.",
                      attachment_id=att2.id, approved=True),
            ])
            s.flush()
        if not s.scalar(select(MailAccount)):
            s.add(MailAccount(provider="sandbox" if not args.live else os.environ.get("MAIL_PROVIDER", "microsoft365"),
                              sender_address=os.environ.get("SENDER", "sales@sayaraup.test"), sender_name="سيارة اب",
                              signature_html="مع التحية،<br><b>فريق المبيعات — سيارة اب</b><br>sayaraup.com"))
        # الحملة الكاملة من طلب المدير (مسودة) — للعرض فقط
        if not s.scalar(select(Campaign).where(Campaign.name.like("حملة النقل%"))):
            parsed = parse_request("ابحث يوميًا عن شركات النقل والخدمات اللوجستية والتوصيل وتأجير السيارات في الرياض "
                                   "وجدة والدمام. أعطِ الأولوية للشركات التي تدير أسطول مركبات، واستبعد الوسطاء "
                                   "وعملاء «سيارة اب» الحاليين. ٢٠٠ شركة نقل وخدمات لوجستية، و١٢٠ شركة توصيل، "
                                   "و٨٠ شركة تأجير سيارات")
            fuel = s.scalar(select(Offer).where(Offer.key == "fuel-management"))
            veh = s.scalar(select(Offer).where(Offer.key == "vehicle-expense-tracking"))
            big = Campaign(name=parsed["name"], request_text="(طلب المدير)", status="draft",
                           daily_target=parsed["daily_target"], exclusions=parsed["exclusions"],
                           exclude_existing_customers=parsed["exclude_existing_customers"], send_enabled=True)
            s.add(big)
            s.flush()
            for c in parsed["categories"]:
                s.add(Category(campaign_id=big.id, sector_key=c["sector_key"], label=c["label"], cities=c["cities"],
                               keywords_ar=c["keywords_ar"], keywords_en=c["keywords_en"], priority=c["priority"],
                               priority_signals=c["priority_signals"], quota=c["quota"],
                               offer_id=(fuel.id if c["sector_key"] == "transport_logistics" else veh.id)))
        camp = s.scalar(select(Campaign).where(Campaign.name.like("تجربة:%")))
        if camp is None:
            veh = s.scalar(select(Offer).where(Offer.key == "vehicle-expense-tracking"))
            camp = Campaign(name="تجربة: تأجير السيارات — الرياض", status="active", daily_target=args.quota,
                            exclusions=["وسطاء الشحن والنقل", "منصات الحجز والمقارنة"], send_enabled=True,
                            test_recipient=args.to, followup_days=4, approved_at=None)
            s.add(camp)
            s.flush()
            s.add(Category(campaign_id=camp.id, sector_key="car_rental", label="تأجير السيارات", cities=["الرياض"],
                           keywords_ar=["شركة تأجير سيارات", "تأجير سيارات للشركات"],
                           keywords_en=["car rental company"], priority=1, quota=args.quota,
                           priority_signals=["تدير أسطول مركبات"], offer_id=veh.id))
        camp_id = camp.id

    # ---------------------------------------------------------------- 1) الاكتشاف
    say("\n=== 1) الاكتشاف: تأجير السيارات في الرياض ===")
    if args.live:
        run_id = run_campaign_discovery(camp_id)
    else:
        from fakes import BrokenSource, FakeCrawler, ListSource, add_site, cand
        _fixtures(add_site)
        src = {"google_places": ListSource("google_places", [cand(n + " (تجريبية)", d, place_id=p) for n, d, p in FIXTURE_CANDS]),
               "web_search": BrokenSource()}
        run_id = run_campaign_discovery(camp_id, sources=src, crawler=FakeCrawler())
    with session_scope() as s:
        run = s.get(DiscoveryRun, run_id)
        say(f"مكتملة: {run.complete_count} من {run.target} — قيد الاستكمال: {run.incomplete_count} — مكررة: "
            f"{run.duplicate_count} — مستبعدة: {run.rejected_count}")
        if run.shortfall_reason:
            say("العجز وسببه:\n" + run.shortfall_reason)
        for c in s.scalars(select(Company).where(Company.campaign_id == camp_id).order_by(Company.id)):
            ph, em = c.primary("phone"), c.primary("email")
            say(f"\n• {c.name} [{ 'مكتملة' if c.data_status == 'complete' else 'قيد الاستكمال'}] — المسؤول: "
                f"{c.owner.name if c.owner else '—'}")
            say(f"  الهاتف: {format_phone_display(ph.value) if ph else '—'}  ← {ph.source_url if ph else ''}")
            say(f"  البريد: {em.value if em else '—'}  ← {em.source_url if em else ''}")
            say(f"  سبب الترشيح: {c.fit_reason}")

    # ---------------------------------------------------------------- 2) الإرسال
    say(f"\n=== 2) الإرسال إلى بريد نملكه: {args.to} ===")
    q = queue_offers(camp_id)
    st = send_due(camp_id, ignore_window=True)
    say(f"جُهزت {q['queued']} رسالة — أُرسلت {st['sent']} — فشلت {st['failed']}")
    time.sleep(1)
    say(f"مزامنة الصندوق: {sync_all()}")

    # ---------------------------------------------------------------- 3) الرد
    with session_scope() as s:
        m = s.scalar(select(OutboundMessage).where(OutboundMessage.campaign_id == camp_id,
                                                   OutboundMessage.kind == "offer").order_by(OutboundMessage.id))
        mid, cname = m.id, m.company.name
        if not args.live:
            SandboxProvider.inject_reply(from_addr=m.to_address, original_message_id=m.internet_message_id,
                                         thread_id=m.thread_id, subject=m.subject,
                                         text="مهتم، اتصلوا بي الأحد بعد الظهر")
    if args.live:
        input(f"\nرُدّ الآن من {args.to} على رسالة «{cname}» بنص: مهتم، اتصلوا بي الأحد بعد الظهر — ثم اضغط Enter...")
    say(f"\n=== 3) الرد ===\nمزامنة الصندوق: {sync_all()}")
    with session_scope() as s:
        m = s.get(OutboundMessage, mid)
        t = s.scalar(select(Task).where(Task.company_id == m.company_id))
        say(f"الشركة: {m.company.name} — المرحلة: {m.company.stage} — المتابعة الآلية أُوقفت: {m.followups_stopped}")
        if t:
            say(f"مهمة للمندوب {t.owner.name if t.owner else '—'}: {t.title}\n  الموعد: {fmt(t.due_at)} ({t.window_label})")

    # ---------------------------------------------------------------- 4) إعادة التشغيل
    say("\n=== 4) إعادة تشغيل الحملة (نفس اليوم واليوم التالي) ===")
    with session_scope() as s:
        before_c = s.scalar(select(func.count(Company.id)))
        before_m = s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.kind == "offer"))
    for day in (riyadh_today(), riyadh_today() + timedelta(days=1)):
        if args.live:
            run_campaign_discovery(camp_id, day=day)
        else:
            src = {"google_places": ListSource("google_places", [cand(n + " (تجريبية)", d, place_id=p) for n, d, p in FIXTURE_CANDS])}
            run_campaign_discovery(camp_id, day=day, sources=src, crawler=FakeCrawler())
    q2 = queue_offers(camp_id)
    st2 = send_due(camp_id, ignore_window=True)
    with session_scope() as s:
        after_c = s.scalar(select(func.count(Company.id)))
        after_m = s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.kind == "offer"))
        say(f"الشركات: {before_c} ← {after_c} | العروض: {before_m} ← {after_m} | جُهز {q2['queued']} وأُرسل {st2['sent']}")
        mgr = s.scalar(select(User).where(User.role.in_(["manager", "admin"])))
        rep = offers_report(s, mgr, OfferFilters(campaign_id=camp_id))
        say(f"\n=== 5) تقرير «الشركات التي أُرسلت إليها عروض» ===\nشركات فريدة: {rep.unique_companies} — "
            f"إجمالي العروض: {rep.total_offers}")
        for r in rep.rows:
            say(f"  {r.company.name} | {r.msg.to_address} | {fmt(r.msg.sent_at)} | {r.offer_name} | {r.msg.status} | "
                f"{r.last_reply.intent_label if r.last_reply else 'لا يوجد رد'} | {r.owner_name}")


FIXTURE_CANDS = [
    ("ألفا لتأجير السيارات", "alpha-rent.example", "P1"),
    ("أوميغا منصة حجز", "omega-broker.example", "P2"),
    ("دلتا لتأجير السيارات", "delta-rent.example", "P3"),
    ("شركة ألفا لتأجير السيارات المحدودة", "alpha-rent.example", "P1b"),
    ("بيتا لتأجير السيارات", "beta-cars.example", "P4"),
    ("جاما للتأجير", "gamma-lease.example", "P5"),
    ("كابا لتأجير الأساطيل", "kappa-fleet.example", "P7"),
    ("لامدا لتأجير السيارات", "lambda-rent.example", "P8"),
]


def _fixtures(add_site):
    add_site("alpha-rent.example", "ألفا لتأجير السيارات", "أسطول من 450 سيارة وفروع في الرياض", "011 410 2030",
             "sales@alpha-rent.example")
    add_site("beta-cars.example", "بيتا لتأجير السيارات", "تأجير سيارات للشركات وأساطيل طويلة الأجل في الرياض",
             "920001234", "info@beta-cars.example")
    add_site("gamma-lease.example", "جاما للتأجير", "تأجير سيارات يومي وشهري، أسطول حديث", "0551234567",
             "procurement@gamma-lease.example")
    add_site("delta-rent.example", "دلتا لتأجير السيارات", "تأجير سيارات في الرياض", "0114445566", None)
    add_site("omega-broker.example", "أوميغا منصة حجز", "منصة حجز ومقارنة اسعار تأجير السيارات (وسيط)",
             "0119998877", "info@omega-broker.example")
    add_site("kappa-fleet.example", "كابا لتأجير الأساطيل", "تأجير أساطيل السيارات للشركات — أكثر من 1200 مركبة",
             "0112901234", "business@kappa-fleet.example")
    add_site("lambda-rent.example", "لامدا لتأجير السيارات", "تأجير سيارات اقتصادية وفاخرة في الرياض", "0533332211",
             "contact@lambda-rent.example")


def _sample_pdf() -> bytes:
    text = "SayaraUp - Vehicle Expense Tracking (sample brochure)"
    stream = f"BT /F1 18 Tf 40 760 Td ({text}) Tj ET".encode()
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
            b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    return out


if __name__ == "__main__":
    main()
