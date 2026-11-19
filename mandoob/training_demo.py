"""بيانات تدريبية: ١٠٠ شركة وهمية تملأ كل شاشات المنصة لشرحها للموظفين.

كل شيء يُكتب مباشرة في قاعدة البيانات كسجلات تاريخية — لا يُرسل أي بريد ولا يُجرى أي اتصال:
- الشركات عليها علامة «بيانات تجريبية» (is_test) ومصدرها training_demo، فكل مسارات الإرسال والاتصال ترفضها.
- الرسائل تُخزَّن بحالاتها النهائية (أُرسل/تم التسليم/مرتد) مباشرة، ولا تمر بطابور الإرسال أبدًا.
- الحملة التدريبية موقوفة والإرسال فيها معطّل، فلا يشغّلها الاكتشاف اليومي ولا الإرسال.
- يُفعَّل «وضع العرض التدريبي» لتظهر الأرقام في لوحة التحكم والتقارير؛ ويُطفأ من لوحة التحكم.

التشغيل: python -m mandoob.training_demo        (إضافة، ولا تكرر إن كانت موجودة)
          python -m mandoob.training_demo --remove  (حذف البيانات التدريبية كلها)
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta

from sqlalchemy import func, select

from .db import init_db, session_scope
from .models import (Activity, Call, Campaign, Category, Company, CompanyRelation, CompanySignal, Consent, Contact,
                     DiscoveryRun, InboundLead, InboundMessage, Meeting, Offer, OutboundMessage, Setting, Task, User)
from .normalize import normalize_name
from .timeutil import now_utc, riyadh_to_utc, riyadh_today

SOURCE = "training_demo"
SENDER = "sales@example.test"

PART1 = ["رؤية", "آفاق", "نخبة", "قمم", "مسارات", "ركائز", "سواعد", "دروب", "روافد", "منابع", "أصول", "ثوابت",
         "معالم", "بوابة", "واحة", "جسور", "ميادين", "سنام", "مدى", "نبراس"]
PART2 = ["الخليج", "الشمال", "الجنوب", "الوسطى", "الصحراء", "المملكة", "العاصمة", "الشرق", "الساحل", "الحجاز"]

SEGMENTS = [  # (المفتاح، الفئة، العدد، أنشطة، نطاق المركبات، المدن)
    ("field_services", "شركات الخدمات الميدانية", 40,
     ["للصيانة والتكييف", "لصيانة المصاعد", "لمكافحة الحشرات", "لأنظمة الأمن والسلامة", "للتشغيل والصيانة"],
     (5, 60), ["الرياض", "الدمام", "الخبر", "جدة", "الجبيل"]),
    ("distribution_supply", "شركات التوزيع والتوريد", 25,
     ["للمواد الغذائية", "للمياه المعبأة", "للألبان", "للأدوية", "للمخبوزات"], (8, 120),
     ["جدة", "الرياض", "مكة المكرمة", "المدينة المنورة", "الدمام"]),
    ("transport_logistics", "النقل والخدمات اللوجستية", 25,
     ["للنقليات", "للشحن البري", "للنقل المبرد", "للخدمات اللوجستية", "لتأجير الشاحنات"], (10, 200),
     ["الرياض", "جدة", "الدمام", "القصيم", "تبوك"]),
    ("vehicle_users", "أي نشاط يستخدم مركبات", 10,
     ["للمقاولات", "للتجارة", "لتوريد مواد البناء", "للخدمات العامة", "لنقل الأثاث"], (1, 8),
     ["الرياض", "أبها", "حائل", "الطائف", "الأحساء"]),
]

OFFER_TEXT = ("السلام عليكم فريق {name}،\n\nنساعد شركات {activity} في {city} على متابعة استهلاك الوقود لكل مركبة "
              "وكشف الاستهلاك غير الطبيعي مبكرًا، من لوحة واحدة.\n\nهل يناسبكم اتصال تعريفي لمدة عشر دقائق هذا الأسبوع؟\n\n"
              "مع التحية،\nفريق المبيعات — سيارة آب")

REPLIES = [  # (النية، التسمية، النص، (أيام بعد الإرسال للموعد، الساعة)، المهمة)
    ("callback_request", "طلب اتصال", "مهتمين، اتصلوا بي الأحد بعد الظهر", (0, 13), "الاتصال بالعميل"),
    ("callback_request", "طلب اتصال", "ممكن تكلموني بكرة الساعة ١١ الصبح", (1, 11), "الاتصال بالعميل"),
    ("interested", "مهتم", "يهمنا الموضوع، عندنا أكثر من ٤٠ سيارة. نبغى نعرف أكثر", (0, 10), "الاتصال بالعميل المهتم"),
    ("quote_request", "طلب عرض سعر", "أرسلوا لنا عرض سعر لـ٢٥ مركبة", (0, 12), "إعداد وإرسال عرض سعر"),
    ("details_request", "طلب تفاصيل", "ممكن ترسلون ملف تعريفي بالتفاصيل؟", (1, 10), "إرسال التفاصيل المطلوبة"),
    ("meeting_request", "طلب اجتماع", "نبغى اجتماع الثلاثاء الصباح مع مدير التشغيل", (2, 10), "حجز اجتماع مع العميل"),
    ("not_interested", "غير مهتم", "شكرًا، لسنا بحاجة حاليًا", None, None),
    ("unsubscribe", "طلب إيقاف التواصل", "الرجاء إيقاف المراسلات", None, None),
]


def _dt(day_offset: int, hour: int, minute: int = 0) -> datetime:
    d = riyadh_today() + timedelta(days=day_offset)
    return riyadh_to_utc(datetime(d.year, d.month, d.day, hour, minute))


def _act(s, cid, kind, title, details="", at=None, ref_type=None, ref_id=None):
    s.add(Activity(company_id=cid, kind=kind, title=title, details=details, at=at or now_utc(),
                   ref_type=ref_type, ref_id=ref_id))


def exists(s) -> int:
    return s.scalar(select(func.count(Company.id)).where(Company.lead_source == SOURCE)) or 0


def create(s) -> dict:
    if exists(s):
        return {"created": 0, "existing": exists(s)}
    rnd = random.Random(42)
    reps = list(s.scalars(select(User).where(User.role == "rep", User.active.is_(True)).order_by(User.id)))
    # عرضان تدريبيان لا يطابقان أي شريحة حقيقية (لا يُختاران لعميل حقيقي أبدًا)
    offers = []
    for key, name in [("training-fuel", "إدارة استهلاك الوقود (تدريبي)"),
                      ("training-expenses", "متابعة مصروفات المركبات (تدريبي)")]:
        o = s.scalar(select(Offer).where(Offer.key == key))
        if o is None:
            o = Offer(key=key, service_name=name, subject_template="{service} لأسطول {company}",
                      value_points="- مثال تدريبي", approved_body=OFFER_TEXT, approved=True, segments=["__training__"])
            s.add(o)
            s.flush()
        offers.append(o)
    camp = Campaign(name="حملة تدريبية — شركات تستخدم المركبات", status="paused", send_enabled=False,
                    daily_target=100, request_text="بيانات تدريبية لشرح المنصة للموظفين — موقوفة ولا ترسل")
    s.add(camp)
    s.flush()
    cats = {}
    for i, (key, label, n, _, _, cities) in enumerate(SEGMENTS):
        c = Category(campaign_id=camp.id, sector_key=key, label=label + " (تدريبي)", cities=cities, quota=n,
                     priority=i + 1, offer_id=offers[i % 2].id, is_experiment=(key == "vehicle_users"))
        s.add(c)
        s.flush()
        cats[key] = c
    today = riyadh_today()
    companies, used = [], set()
    idx = 0
    for key, label, n, acts, (vmin, vmax), cities in SEGMENTS:
        for _ in range(n):
            idx += 1
            while True:
                act = rnd.choice(acts)
                name = f"{'مؤسسة' if key == 'vehicle_users' else 'شركة'} {rnd.choice(PART1)} {rnd.choice(PART2)} {act}"
                if name not in used:
                    used.add(name)
                    break
            city = rnd.choice(cities)
            found_days_ago = 0 if idx % 3 == 0 else rnd.randint(1, 9)
            found = _dt(-found_days_ago, rnd.randint(6, 8), rnd.randint(0, 59))
            owner = reps[(idx - 1) % len(reps)] if reps else None
            c = Company(name=name, name_norm=normalize_name(name), activity=act[1:] if act.startswith("ل") and not act.startswith("لل") else ("ال" + act[2:] if act.startswith("لل") else act),
                        sector_key=key, city=city, website=None, data_status="complete", stage="new", is_test=True,
                        lead_source=SOURCE, campaign_id=camp.id, category_id=cats[key].id, first_found_at=found,
                        last_seen_at=found, completed_at=found, counted_on=today - timedelta(days=found_days_ago),
                        vehicles_estimate=rnd.randint(vmin, vmax), owner_id=owner.id if owner else None,
                        operator_status=rnd.choice(["self", "self", "self", "unknown", "contracted"]),
                        fit_reason=f"بيانات تدريبية — تذكر الشركة في موقعها أنها تشغّل أسطولًا من {rnd.randint(vmin, vmax)} مركبة في {city}",
                        fit_phrase=rnd.choice(["فرق صيانة متنقلة", "أسطول التوزيع", "توزيع يومي على المتاجر", "شاحنات مبردة",
                                               "سياراتنا"]),
                        fit_evidence_url="https://example.test/training", fit_score=round(rnd.uniform(0.6, 0.95), 2),
                        decision_unit=rnd.choice(["إدارة الأسطول", "إدارة التشغيل", "إدارة المشتريات"]))
            s.add(c)
            s.flush()
            c.contacts.append(Contact(kind="phone", value=f"+0009{idx:06d}", raw_value=f"+0009{idx:06d}",
                                      source_url="https://example.test/training/contact", source_type="test",
                                      verified=True, preferred=True, label="تجريبي"))
            if idx % 10 != 1:  # ١٠ شركات ينقصها البريد → «قيد الاستكمال»
                c.contacts.append(Contact(kind="email", value=f"train{idx:03d}@example.test", raw_value=f"train{idx:03d}@example.test",
                                          source_url="https://example.test/training/contact", source_type="test",
                                          verified=True, preferred=True, label="المشتريات"))
            else:
                c.data_status, c.counted_on, c.completed_at = "incomplete", None, None
            _act(s, c.id, "discovered", "اكتُشفت الشركة عبر البحث الآلي (تدريبي)",
                 f"الحملة: {camp.name}\nسبب الترشيح: {c.fit_reason}", at=found)
            if owner:
                _act(s, c.id, "assigned", f"أُسندت الشركة إلى المندوب {owner.name}", at=found + timedelta(minutes=5))
            companies.append(c)
    s.flush()

    complete = [c for c in companies if c.data_status == "complete"]
    contacted = complete[20:]  # ٢٠ مكتملة «لم يتم التواصل» (مؤهلة)، والباقي أُرسل لها عرض
    stats = {"offers": 0, "delivered": 0, "bounced": 0, "replies": 0, "tasks": 0, "meetings": 0, "won": 0,
             "calls": 0, "signals": 0, "leads": 0}
    for j, c in enumerate(contacted):
        offer = offers[j % 2]
        sent_days_ago = 0 if j % 4 == 0 else rnd.randint(1, 6)
        sent = _dt(-sent_days_ago, rnd.randint(9, 15), rnd.randint(0, 59))
        em = next(ct.value for ct in c.contacts if ct.kind == "email")
        bounced = j % 17 == 5
        m = OutboundMessage(company_id=c.id, campaign_id=camp.id, category_id=c.category_id, offer_id=offer.id,
                            kind="offer", dedupe_key=f"training:{c.id}:offer", from_address=SENDER, to_address=em,
                            intended_to=em, subject=f"{offer.service_name.replace(' (تدريبي)', '')} لأسطول {c.name}",
                            body_text=OFFER_TEXT.format(name=c.name, activity=c.activity, city=c.city),
                            body_html="<div dir='rtl' style='font-family:Tahoma'>" + OFFER_TEXT.format(
                                name=c.name, activity=c.activity, city=c.city).replace("\n", "<br>") + "</div>",
                            status="bounced" if bounced else "delivered", sent_at=sent,
                            delivered_at=None if bounced else sent + timedelta(minutes=2),
                            bounced_at=sent + timedelta(minutes=3) if bounced else None,
                            bounce_reason="550 5.1.1 عنوان المستلم غير موجود (تدريبي)" if bounced else None,
                            internet_message_id=f"<training-{c.id}@example.test>", thread_id=f"training-{c.id}",
                            status_detail="سجل تدريبي — لم يُرسل فعليًا", followups_stopped=True)
        s.add(m)
        s.flush()
        stats["offers"] += 1
        stats["bounced" if bounced else "delivered"] += 1
        c.stage = "contacted"
        c.last_action, c.last_action_at = f"أُرسل عرض: {offer.service_name}", sent
        c.next_action, c.next_action_at = "انتظار رد العميل", sent + timedelta(days=4)
        _act(s, c.id, "email_sent", f"أُرسل عرض: {m.subject}", f"من {SENDER} إلى {em}", at=sent, ref_type="outbound", ref_id=m.id)
        if bounced:
            _act(s, c.id, "email_bounced", f"ارتد البريد: {m.subject}", m.bounce_reason, at=m.bounced_at, ref_type="outbound", ref_id=m.id)
            continue
        _act(s, c.id, "email_delivered", f"تم التسليم: {m.subject}", at=m.delivered_at, ref_type="outbound", ref_id=m.id)
        if j % 3 == 0 and stats["replies"] < 22:
            intent, label, text, when, task_title = REPLIES[stats["replies"] % len(REPLIES)]
            got = sent + timedelta(hours=rnd.randint(2, 20))
            if got > now_utc():
                got = now_utc() - timedelta(minutes=rnd.randint(5, 90))
            due = None
            if when:
                due = _dt(when[0], when[1])
                if stats["replies"] % 5 == 0:
                    due = _dt(-1, 11)  # متأخرة
            r = InboundMessage(provider_message_id=f"training-reply-{c.id}", from_address=em, subject=f"RE: {m.subject}",
                               body_text=text, received_at=got, message_type="reply", outbound_id=m.id, company_id=c.id,
                               intent=intent, intent_label=label, requested_at=due, summary=text, processed=True,
                               requested_window=None)
            s.add(r)
            s.flush()
            m.replied_at = got
            stats["replies"] += 1
            _act(s, c.id, "reply", f"رد العميل ({label})", text, at=got, ref_type="inbound", ref_id=r.id)
            c.last_action, c.last_action_at = f"رد العميل: {label}", got
            if intent == "unsubscribe":
                c.stage, c.do_not_contact = "do_not_contact", True
                c.next_action = c.next_action_at = None
                s.add(Consent(company_id=c.id, channel="email", basis="opt_out", granted=False, evidence=text))
                _act(s, c.id, "opt_out", "سُجّل طلب إيقاف التواصل", text, at=got)
                continue
            if intent == "not_interested":
                c.stage, c.next_action, c.next_action_at = "lost", None, None
                continue
            c.stage = "interested"
            t = Task(company_id=c.id, owner_id=c.owner_id, kind="meeting" if intent == "meeting_request" else "call",
                     title=f"{task_title} — {c.name}",
                     details=f"الهاتف: {next(ct.value for ct in c.contacts if ct.kind == 'phone')}\nنص الرد:\n{text}",
                     due_at=due, window_label=None, source_inbound_id=r.id)
            s.add(t)
            s.flush()
            stats["tasks"] += 1
            c.next_action, c.next_action_at = task_title, due
            _act(s, c.id, "task", f"أُنشئت مهمة: {t.title}", at=got + timedelta(seconds=5), ref_type="task", ref_id=t.id)
    s.flush()

    # اجتماعات وصفقات ومكالمات وإشارات
    interested = [c for c in companies if c.stage == "interested"]
    for k, c in enumerate(interested[:7]):
        start = _dt(k % 3 + 1, 10 + k % 4)
        if c.owner_id:
            mt = Meeting(company_id=c.id, owner_id=c.owner_id, starts_at=start, ends_at=start + timedelta(minutes=30),
                         attendee_email=next((ct.value for ct in c.contacts if ct.kind == "email"), None),
                         calendar_event_id=f"training-evt-{c.id}", created_at=now_utc() - timedelta(hours=k))
            s.add(mt)
        c.stage = "meeting"
        c.next_action, c.next_action_at = "عقد الاجتماع التعريفي", start
        for t in s.scalars(select(Task).where(Task.company_id == c.id, Task.status == "open")):
            t.status, t.done_at = "done", now_utc()
        _act(s, c.id, "meeting", "حُجز اجتماع تعريفي (تدريبي)", f"الموعد: {start}", at=now_utc() - timedelta(hours=k))
        stats["meetings"] += 1
    for c in interested[7:10]:
        c.stage, c.deal_value = "won", float(rnd.choice([18000, 32000, 54000]))
        c.next_action = c.next_action_at = None
        for t in s.scalars(select(Task).where(Task.company_id == c.id, Task.status == "open")):
            t.status, t.done_at = "done", now_utc()
        _act(s, c.id, "won", "صفقة رابحة (تدريبي)", f"القيمة: {c.deal_value:,.0f} ريال")
        stats["won"] += 1
    for k, c in enumerate([c for c in contacted if c.stage in ("contacted", "interested")][:6]):
        answered = k % 3 != 2
        s.add(Consent(company_id=c.id, channel="call", basis="explicit", granted=True, evidence="موافقة تدريبية"))
        cl = Call(company_id=c.id, to_number=next(ct.value for ct in c.contacts if ct.kind == "phone"), provider="training",
                  provider_call_id=f"training-call-{c.id}", status="completed" if answered else "no_answer", answered=answered,
                  outcome=rnd.choice(["interested", "callback", "transfer_to_rep"]) if answered else "voicemail",
                  summary="عرّف المساعد بنفسه واستأذن العميل، ثم شرح متابعة استهلاك الوقود. طلب العميل اتصالًا من المندوب."
                  if answered else "لم يتم الرد.", next_step="اتصال المندوب لتأكيد الاحتياج" if answered else None,
                  duration_sec=rnd.randint(80, 240) if answered else 0, created_at=now_utc() - timedelta(hours=k + 1),
                  ended_at=now_utc() - timedelta(hours=k + 1) + timedelta(minutes=3))
        s.add(cl)
        s.flush()
        _act(s, c.id, "call_result", f"نتيجة المكالمة: {cl.outcome}", cl.summary, at=cl.ended_at, ref_type="call", ref_id=cl.id)
        stats["calls"] += 1
    sig_kinds = [("warehouse_opening", "افتتاح مستودع"), ("new_city", "توسع إلى مدينة جديدة"),
                 ("contract_win", "فوز بعقد تشغيل"), ("hiring_drivers", "توظيف سائقين"), ("hiring_fleet", "توظيف مشرف أسطول")]
    for k, c in enumerate(complete[:20:2]):
        kind, label = sig_kinds[k % len(sig_kinds)]
        s.add(CompanySignal(company_id=c.id, kind=kind, label=label, title=f"{label} — إعلان تدريبي في {c.city}",
                            announced_on=today - timedelta(days=rnd.randint(3, 60)), url="https://example.test/news"))
        c.signal_boost = 0.15
        stats["signals"] += 1
    # ناقل متعاقد وفرع
    if len(complete) > 40:
        s.add(CompanyRelation(company_id=complete[3].id, related_id=complete[40].id, kind="carrier",
                              evidence="يتم التوصيل عن طريق ناقل متعاقد (تدريبي)", source_url="https://example.test/training"))
        complete[3].operator_status = "contracted"
    for k, (nm, city, src) in enumerate([("شركة الواحة للتوزيع (تدريبي)", "الخبر", "partner:tracker-co"),
                                         ("مؤسسة الفجر للمقاولات (تدريبي)", "الرياض", "website_form:website"),
                                         ("مصنع النخيل للألبان (تدريبي)", "القصيم", "campaign:linkedin-nov"),
                                         ("شركة المدى للصيانة (تدريبي)", "جدة", "website_form:website")]):
        c = Company(name=nm, name_norm=normalize_name(nm), activity="طلب عرض لأسطوله", city=city, data_status="complete",
                    stage="interested", is_test=True, lead_source=src, vehicles_estimate=[35, 6, 60, 18][k],
                    fit_reason="بيانات تدريبية — طلب عرضًا لأسطوله عبر نموذج «اطلب عرضًا لأسطولك»",
                    owner_id=reps[k % len(reps)].id if reps else None)
        s.add(c)
        s.flush()
        c.contacts.append(Contact(kind="phone", value=f"+0008{k:06d}", raw_value="", source_url="form:training",
                                  source_type="test", verified=True, preferred=True))
        lead = InboundLead(company_id=c.id, company_name=nm, city=city, vehicles=c.vehicles_estimate,
                           contact_name=["سالم", "فهد", "نورة", "ماجد"][k], phone=f"+0008{k:06d}",
                           preferred_channel=["whatsapp", "phone", "email", "phone"][k], source=src,
                           created_at=now_utc() - timedelta(hours=k * 3 + 1))
        s.add(lead)
        due = _dt(0 if k < 2 else 1, 10 + k)
        s.add(Task(company_id=c.id, owner_id=c.owner_id, kind="call", title=f"التواصل مع طالب عرض — {nm}",
                   details="طلب عرض من النموذج (تدريبي)", due_at=due, window_label="خلال ساعة عمل"))
        c.next_action, c.next_action_at = "التواصل مع طالب العرض", due
        _act(s, c.id, "inbound", "طلب عرض لأسطوله (تدريبي)", f"المصدر: {src}")
        stats["leads"] += 1
    # تشغيل الاكتشاف لليوم (للسجل والتقرير اليومي)
    today_complete = sum(1 for c in complete if c.counted_on == today)
    s.add(DiscoveryRun(run_date=today, campaign_id=camp.id, status="partial", target=100, complete_count=today_complete,
                       incomplete_count=sum(1 for c in companies if c.data_status == "incomplete"), duplicate_count=7,
                       rejected_count=12, checkpoint={}, per_category={},
                       per_source={"ai_agent": {"candidates": 58, "complete_count": today_complete - 6, "incomplete_count": 6,
                                                "duplicate_count": 5, "rejected_count": 9},
                                   "google_places": {"candidates": 21, "complete_count": 6, "incomplete_count": 4,
                                                     "duplicate_count": 2, "rejected_count": 3}},
                       source_errors={}, shortfall=100 - today_complete,
                       shortfall_reason="بيانات تدريبية — مثال على سبب العجز: استُنفدت عبارات البحث لبعض الفئات",
                       finished_at=now_utc(), log=["سجل تدريبي"]))
    _set_demo_view(s, True)
    s.flush()
    return {"created": exists(s), "staff": len(reps), **stats}


def _set_demo_view(s, on: bool) -> None:
    row = s.get(Setting, "demo_view")
    if row:
        row.value = {"on": on}
    else:
        s.add(Setting(key="demo_view", value={"on": on}))


def remove(s) -> int:
    ids = [cid for (cid,) in s.execute(select(Company.id).where(Company.lead_source.in_([SOURCE, "partner:tracker-co",
                                                                                          "website_form:website",
                                                                                          "campaign:linkedin-nov"]),
                                                               Company.is_test.is_(True)))]
    for model, col in [(Activity, Activity.company_id), (Task, Task.company_id), (Meeting, Meeting.company_id),
                       (Call, Call.company_id), (CompanySignal, CompanySignal.company_id), (Consent, Consent.company_id),
                       (InboundLead, InboundLead.company_id), (InboundMessage, InboundMessage.company_id),
                       (OutboundMessage, OutboundMessage.company_id)]:
        s.query(model).filter(col.in_(ids)).delete(synchronize_session=False)
    s.query(CompanyRelation).filter(CompanyRelation.company_id.in_(ids) | CompanyRelation.related_id.in_(ids)).delete(
        synchronize_session=False)
    for c in s.scalars(select(Company).where(Company.id.in_(ids))):
        s.delete(c)
    for camp in s.scalars(select(Campaign).where(Campaign.name.like("حملة تدريبية%"))):
        s.query(DiscoveryRun).filter(DiscoveryRun.campaign_id == camp.id).delete(synchronize_session=False)
        s.delete(camp)
    _set_demo_view(s, False)
    s.flush()
    return len(ids)


def main() -> None:
    init_db()
    with session_scope() as s:
        if "--remove" in sys.argv:
            print(json.dumps({"removed": remove(s)}, ensure_ascii=False))
            return
        res = create(s)
    with session_scope() as s:
        test_ids = select(Company.id).where(Company.is_test.is_(True))
        res["queued_or_sending"] = s.scalar(select(func.count(OutboundMessage.id)).where(
            OutboundMessage.company_id.in_(test_ids), OutboundMessage.status.in_(["queued", "sending"])))
        res["active_training_campaigns"] = s.scalar(select(func.count(Campaign.id)).where(
            Campaign.name.like("حملة تدريبية%"), Campaign.status == "active"))
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
