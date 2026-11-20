"""سيناريو القبول الكامل (بدون شبكة): فئة واحدة ومدينة واحدة، إرسال إلى بريد نملكه (صندوق التجربة)،
رد بطلب اتصال يوم الأحد، ثم إعادة التشغيل للتأكد من عدم تكرار الشركة أو العرض."""
from datetime import timedelta

from sqlalchemy import func, select

from fakes import BrokenSource, FakeCrawler, ListSource, add_site, cand
from mandoob.db import init_db, session_scope
from mandoob.discovery.runner import run_campaign_discovery
from mandoob.inbox import sync_all
from mandoob.mail.sandbox import SandboxProvider
from mandoob.models import (Attachment, Campaign, Category, Company, DiscoveryRun, InboundMessage, MailAccount,
                            Notification, Offer, OutboundMessage, Suppression, Task, User)
from mandoob.outreach import queue_offers, send_due
from mandoob.reports import OfferFilters, export_offers_xlsx, needs_followup_today, offers_report
from mandoob.timeutil import riyadh_today, to_riyadh
from mandoob.auth import hash_password
from mandoob.config import settings

MINI_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj "
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF")

STATE = {}


def setup_module():
    init_db()
    add_site("alpha-rent.example", "ألفا لتأجير السيارات", "أسطول من 450 سيارة وفروع في الرياض", "011 410 2030",
             "sales@alpha-rent.example")
    add_site("beta-cars.example", "بيتا لتأجير السيارات", "تأجير سيارات للشركات وأساطيل طويلة الأجل في الرياض",
             "920001234", "info@beta-cars.example")
    add_site("gamma-lease.example", "جاما للتأجير", "تأجير سيارات يومي وشهري، أسطول حديث", "0551234567",
             "procurement@gamma-lease.example")
    add_site("delta-rent.example", "دلتا لتأجير السيارات", "تأجير سيارات في الرياض", "0114445566", None)
    add_site("omega-broker.example", "أوميغا منصة حجز", "منصة حجز ومقارنة اسعار تأجير السيارات (وسيط)",
             "0119998877", "info@omega-broker.example")
    add_site("bounce-co.bounce.test", "شركة الارتداد لتأجير السيارات", "تأجير سيارات وأسطول", "0112223344",
             "sales@bounce-co.bounce.test")

    pdf = settings.data_dir / "fleet-rental-offer.pdf"
    pdf.write_bytes(MINI_PDF)
    with session_scope() as s:
        s.add_all([User(name="سارة المندوبة", email="sara@sayaraup.test", password_hash=hash_password("x"),
                        role="rep", sectors=["car_rental"], cities=["الرياض"]),
                   User(name="خالد المندوب", email="khalid@sayaraup.test", password_hash=hash_password("x"),
                        role="rep", sectors=["delivery"])])
        att = Attachment(title="عرض متابعة مصروفات المركبات", filename="fleet-rental-offer.pdf",
                         stored_path=str(pdf), size_bytes=len(MINI_PDF), sha256="x")
        s.add(att)
        s.flush()
        offer = Offer(key="vehicle-expense-tracking", service_name="متابعة مصروفات المركبات",
                      subject_template="{service} لأسطول {company}",
                      value_points="- رؤية موحدة لمصروفات كل مركبة\n- تنبيهات الصيانة والتجديد",
                      approved_body="السلام عليكم فريق {company}،\n\nنساعد شركات {activity} في {city} على متابعة "
                                    "مصروفات كل مركبة في الأسطول من لوحة واحدة.",
                      attachment_id=att.id, approved=True)
        s.add(offer)
        s.flush()
        camp = Campaign(name="تجربة: تأجير السيارات — الرياض", status="active", daily_target=3,
                        exclusions=["وسطاء الشحن والنقل", "منصات الحجز والمقارنة"], send_enabled=True,
                        test_recipient="sales-test@sayaraup.test", followup_days=4)
        s.add(camp)
        s.flush()
        s.add(Category(campaign_id=camp.id, sector_key="car_rental", label="تأجير السيارات", cities=["الرياض"],
                       keywords_ar=["شركة تأجير سيارات"], keywords_en=[], priority=1, quota=3, offer_id=offer.id))
        s.add(MailAccount(provider="sandbox", sender_address="sales@sayaraup.test", sender_name="سيارة اب",
                          signature_html="مع التحية،<br><b>فريق المبيعات — سيارة اب</b>"))
        STATE["camp"] = camp.id


def _sources(include_bounce=False):
    places = ListSource("google_places", [
        cand("ألفا لتأجير السيارات", "alpha-rent.example", place_id="P1"),
        cand("أوميغا منصة حجز", "omega-broker.example", place_id="P2"),
        cand("دلتا لتأجير السيارات", "delta-rent.example", place_id="P3"),
        cand("شركة ألفا لتأجير السيارات المحدودة", "alpha-rent.example", place_id="P1b"),  # مكرر بنطاقه
        cand("بيتا لتأجير السيارات", "beta-cars.example", place_id="P4"),
        cand("جاما للتأجير", "gamma-lease.example", place_id="P5"),
    ] + ([cand("شركة الارتداد لتأجير السيارات", "bounce-co.bounce.test", place_id="P6")] if include_bounce else []))
    return {"google_places": places, "web_search": BrokenSource()}


def test_1_discovery_counts_only_complete_and_survives_broken_source():
    run_id = run_campaign_discovery(STATE["camp"], sources=_sources(), crawler=FakeCrawler())
    with session_scope() as s:
        run = s.get(DiscoveryRun, run_id)
        complete = list(s.scalars(select(Company).where(Company.data_status == "complete")))
        assert {c.domain for c in complete} == {"alpha-rent.example", "beta-cars.example", "gamma-lease.example"}
        assert run.complete_count == 3 and run.shortfall == 0 and run.status == "done"
        inc = s.scalar(select(Company).where(Company.domain == "delta-rent.example"))
        assert inc.data_status == "incomplete"  # هاتف فقط → قيد استكمال البيانات
        assert s.scalar(select(Company).where(Company.domain == "omega-broker.example")) is None  # وسيط مستبعد
        alpha = s.scalar(select(Company).where(Company.domain == "alpha-rent.example"))
        assert len(alpha.sources) == 2  # مصدران مدمجان في سجل واحد
        email = alpha.primary("email")
        assert email.value == "sales@alpha-rent.example"
        assert email.source_url == "https://alpha-rent.example/contact-us"
        assert alpha.primary("phone").value == "+966114102030"
        assert alpha.fit_reason and alpha.counted_on == riyadh_today()
        assert alpha.owner.name == "سارة المندوبة"  # التوزيع حسب القطاع والمدينة
        assert run.checkpoint  # نقطة التوقف محفوظة


def test_2_send_offers_to_owned_mailbox_with_attachment():
    stats = queue_offers(STATE["camp"])
    assert stats["queued"] == 3
    sent = send_due(ignore_window=True)
    assert sent["sent"] == 3
    with session_scope() as s:
        msgs = list(s.scalars(select(OutboundMessage).where(OutboundMessage.kind == "offer")))
        for m in msgs:
            assert m.to_address == "sales-test@sayaraup.test"  # وضع التجربة
            assert m.intended_to.endswith(".example")
            assert m.status == "sent" and m.sent_at and m.attachment_sha256 and m.attachment_id
            eml = SandboxProvider.read_sent(m.provider_message_id)
            atts = [p for p in eml.iter_attachments()]
            assert [a.get_filename() for a in atts] == ["fleet-rental-offer.pdf"]
            assert atts[0].get_content() == MINI_PDF
            assert m.company.name in m.subject
            assert "هل يناسبكم اتصال تعريفي" in m.body_text
        STATE["alpha_msg"] = next(m.id for m in msgs if m.company.domain == "alpha-rent.example")
    st = sync_all()
    assert st["delivered"] == 3
    with session_scope() as s:
        assert s.get(OutboundMessage, STATE["alpha_msg"]).status == "delivered"


def test_3_reply_creates_task_for_owner_on_sunday():
    with session_scope() as s:
        m = s.get(OutboundMessage, STATE["alpha_msg"])
        SandboxProvider.inject_reply(from_addr=m.to_address, original_message_id=m.internet_message_id,
                                     thread_id=m.thread_id, subject=m.subject,
                                     text="مهتم، اتصلوا بي الأحد بعد الظهر\n\n> النص الأصلي...")
    st = sync_all()
    assert st["replies"] == 1
    with session_scope() as s:
        m = s.get(OutboundMessage, STATE["alpha_msg"])
        assert m.replied_at and m.followups_stopped
        c = m.company
        assert c.stage == "interested"
        task = s.scalar(select(Task).where(Task.company_id == c.id))
        due = to_riyadh(task.due_at)
        assert due.weekday() == 6 and due.hour == 13  # الأحد 1:00 م بتوقيت الرياض
        assert task.owner.name == "سارة المندوبة"
        assert "+966114102030" in task.details and "مهتم، اتصلوا بي الأحد بعد الظهر" in task.details
        note = s.scalar(select(Notification).where(Notification.company_id == c.id))
        assert "مهتم" in note.body
        reply = s.scalar(select(InboundMessage).where(InboundMessage.company_id == c.id,
                                                      InboundMessage.message_type == "reply"))
        assert reply.intent == "callback_request" and reply.outbound_id == m.id
        rep = s.scalar(select(User).where(User.name == "سارة المندوبة"))
        # تظهر في «تحتاج متابعة» يوم الأحد
        items = needs_followup_today(s, rep, to_riyadh(task.due_at).date())
        assert any(it["company"].id == c.id for it in items)


def test_4_rerun_does_not_duplicate_company_or_offer():
    with session_scope() as s:
        before = s.scalar(select(func.count(Company.id)))
    # نفس اليوم (استئناف) ثم اليوم التالي بمصادر جديدة تعيد نفس الشركات
    run_campaign_discovery(STATE["camp"], sources=_sources(), crawler=FakeCrawler())
    tomorrow = riyadh_today() + timedelta(days=1)
    run_id = run_campaign_discovery(STATE["camp"], day=tomorrow, sources=_sources(), crawler=FakeCrawler())
    with session_scope() as s:
        assert s.scalar(select(func.count(Company.id))) == before
        run = s.get(DiscoveryRun, run_id)
        assert run.complete_count == 0 and run.duplicate_count >= 3
        assert "استُنفدت" in run.shortfall_reason or run.shortfall > 0
        alpha = s.scalar(select(Company).where(Company.domain == "alpha-rent.example"))
        assert alpha.times_seen >= 3 and alpha.counted_on == riyadh_today()
    assert queue_offers(STATE["camp"])["queued"] == 0
    assert send_due(ignore_window=True)["sent"] == 0
    with session_scope() as s:
        assert s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.kind == "offer")) == 3
        # نفس الشركة في حملة ثانية بنفس العرض: لا يُعاد الإرسال
        offer = s.scalar(select(Offer))
        camp2 = Campaign(name="حملة ثانية", status="active", send_enabled=True, test_recipient="x@sayaraup.test")
        s.add(camp2)
        s.flush()
        cat2 = Category(campaign_id=camp2.id, sector_key="car_rental", label="تأجير", cities=["الرياض"], quota=5,
                        offer_id=offer.id)
        s.add(cat2)
        s.flush()
        gamma = s.scalar(select(Company).where(Company.domain == "gamma-lease.example"))
        gamma.campaign_id, gamma.category_id, gamma.stage = camp2.id, cat2.id, "new"
        STATE["camp2"] = camp2.id
    assert queue_offers(STATE["camp2"])["queued"] == 0


def test_5_offers_report_reflects_reality():
    with session_scope() as s:
        admin = User(name="مدير", email="m@sayaraup.test", password_hash="x", role="manager")
        s.add(admin)
        s.flush()
        rep = offers_report(s, admin, OfferFilters())
        assert rep.unique_companies == 3 and rep.total_offers == 3
        replied = offers_report(s, admin, OfferFilters(reply="replied"))
        assert [r.company.domain for r in replied.rows] == ["alpha-rent.example"]
        assert replied.rows[0].last_reply.intent_label == "طلب اتصال"
        assert offers_report(s, admin, OfferFilters(status="delivered")).total_offers == 3
        sara = s.scalar(select(User).where(User.name == "سارة المندوبة"))
        assert offers_report(s, sara, OfferFilters()).unique_companies == 3  # كلها مسندة لها
        khalid = s.scalar(select(User).where(User.name == "خالد المندوب"))
        assert offers_report(s, khalid, OfferFilters()).total_offers == 0  # لا يرى شركات غيره
        x = export_offers_xlsx(rep)
        assert x[:2] == b"PK"


def test_6_unsubscribe_and_bounce():
    with session_scope() as s:
        beta = s.scalar(select(OutboundMessage).join(Company).where(Company.domain == "beta-cars.example"))
        SandboxProvider.inject_reply(from_addr=beta.to_address, original_message_id=beta.internet_message_id,
                                     thread_id=beta.thread_id, subject=beta.subject,
                                     text="الرجاء إيقاف المراسلات وعدم التواصل معنا")
    sync_all()
    with session_scope() as s:
        c = s.scalar(select(Company).where(Company.domain == "beta-cars.example"))
        assert c.do_not_contact and c.stage == "do_not_contact"
        assert s.scalar(select(Suppression).where(Suppression.value == "info@beta-cars.example"))
    # الارتداد: نطلق إرسالًا حقيقيًا (بدون وضع التجربة) لشركة بنطاق bounce.test
    with session_scope() as s:
        camp = s.get(Campaign, STATE["camp"])
        camp.test_recipient = None
        bc = s.scalar(select(Company).where(Company.domain == "bounce-co.bounce.test"))
        assert bc is None or bc.data_status == "complete"
    run_campaign_discovery(STATE["camp"], day=riyadh_today() + timedelta(days=2),
                           sources=_sources(include_bounce=True), crawler=FakeCrawler())
    with session_scope() as s:
        camp = s.get(Campaign, STATE["camp"])
        camp.daily_target = 10
    queue_offers(STATE["camp"])
    send_due(ignore_window=True)
    sync_all()
    with session_scope() as s:
        m = s.scalar(select(OutboundMessage).join(Company).where(Company.domain == "bounce-co.bounce.test"))
        assert m is not None and m.status == "bounced" and "550" in m.bounce_reason
        assert m.to_address == "sales@bounce-co.bounce.test"
