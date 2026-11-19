"""تعبئة ٥٠٠ عميل تجريبي لاختبار البحث والتصفية والتوزيع والصلاحيات — دون أي إرسال أو اتصال.

- كل سجل عليه علامة «بيانات تجريبية» (is_test) وحالة «لم يتم التواصل».
- مستبعد من حملات البريد والمكالمات والمتابعات الآلية ومن هدف الـ٤٠٠ اليومي (يتحقق الخادم من العلامة في كل مسار إرسال).
- البريد clientNNN@example.test (نطاق محجوز لا يستقبل بريدًا) والهاتف +000… (ليس مفتاح دولة، غير قابل للاتصال).
- إعادة التشغيل تُكمل الناقص فقط: المفتاح الثابت لكل عميل هو بريده.

التشغيل: python -m mandoob.seed_test
"""
from __future__ import annotations

import json
import random

from sqlalchemy import func, select

from .db import init_db, session_scope
from .models import Activity, Call, Company, Contact, OutboundMessage, User
from .normalize import normalize_name

TOTAL = 500
SOURCE = "test://seed"

CITIES = ["الرياض", "جدة", "الدمام", "مكة المكرمة", "المدينة المنورة", "الخبر", "الطائف", "بريدة", "تبوك", "أبها",
          "خميس مشيط", "حائل", "جازان", "نجران", "الجبيل", "الأحساء", "ينبع", "القطيف", "عنيزة", "الخرج"]
CITY_WEIGHTS = [16, 14, 10, 6, 5, 5, 4, 4, 3, 3, 3, 3, 3, 2, 3, 3, 2, 2, 2, 3]

NAMES = ["الأفق", "النخبة", "الريم", "الواحة", "المسار", "الصفوة", "البيان", "الرواد", "الندى", "السنابل", "الوفاء",
         "الأمانة", "الجزيرة", "الخليج", "السهم", "المدار", "النماء", "الإتقان", "الركائز", "الفرسان", "الهدف", "الضياء",
         "المنار", "الشروق", "الأصيل", "التميز", "الريادة", "الساحل", "العزم", "الأنوار", "البشائر", "القمة", "المجد",
         "الواعد", "الدرة", "السحاب", "الفجر", "النجم", "الوسام", "البراعم"]
FIRST = ["محمد", "عبدالله", "فهد", "خالد", "سعد", "ناصر", "عبدالرحمن", "سلطان", "تركي", "ماجد", "فيصل", "بندر",
         "سامي", "يوسف", "إبراهيم", "علي", "حسن", "عمر", "صالح", "نايف", "منى", "نورة", "سارة", "هند", "ريم"]
LAST = ["العتيبي", "القحطاني", "الشهري", "الحربي", "الغامدي", "الزهراني", "الدوسري", "المطيري", "الشمري", "العنزي",
        "السبيعي", "الرشيدي", "البقمي", "الجهني", "العمري", "الأحمدي", "المالكي", "السهلي", "الخالدي", "اليامي"]

# (المفتاح، التسمية، العدد، قالب الاسم، أنشطة، نطاق المركبات، مفتاح الشريحة)
GROUPS = [
    ("fleet_ops", "شركات لديها مركبات تشغيلية أو فرق ميدانية", 150, "شركة {n} {a}",
     ["لصيانة التكييف", "للمصاعد", "لمكافحة الحشرات", "لأنظمة الأمن والسلامة", "للنظافة وإدارة المرافق",
      "للاتصالات والشبكات", "للمقاولات العامة", "للخدمات الكهربائية", "لصيانة المباني", "للتشغيل والصيانة"],
     (3, 80), "field_services"),
    ("small_est", "مؤسسات صغيرة تستخدم سيارة واحدة أو أكثر", 100, "مؤسسة {n} {a}",
     ["للتجارة", "للمقاولات", "لقطع الغيار", "للمواد الغذائية", "للخدمات العامة", "لتوريد المياه", "للإلكترونيات",
      "للمفروشات", "للطباعة والإعلان", "للحلويات"], (1, 6), "vehicle_users"),
    ("factory", "مصانع تستخدم مركبات للنقل أو التوزيع", 100, "مصنع {n} {a}",
     ["للمنتجات الغذائية", "للألبان", "للمياه المعبأة", "للبلاستيك", "للحديد", "للمنظفات", "للأثاث", "للمخبوزات",
      "للورق والكرتون", "للعصائر"], (4, 120), "distribution_supply"),
    ("logistics", "منشآت نقليات وشحن وتوصيل", 100, "{n} {a}",
     ["للنقليات", "للشحن البري", "لخدمات التوصيل", "للنقل المبرد", "للخدمات اللوجستية", "لتوصيل الطلبات",
      "لنقل الأثاث", "لنقل الموظفين", "لتأجير الشاحنات", "للنقل الثقيل"], (2, 200), "transport_logistics"),
    ("individual", "أفراد وأصحاب أنشطة يستخدمون مركباتهم", 50, "{p} — {a}",
     ["سطحة لنقل السيارات", "فني تكييف متنقل", "توصيل طلبات", "نقل عفش", "صهريج مياه", "سباك متنقل",
      "كهربائي متنقل", "غسيل سيارات متنقل", "توصيل مواد بناء", "مندوب توزيع"], (1, 2), "vehicle_users"),
]


def _plan() -> list[dict]:
    """قائمة ثابتة (نفس النتيجة في كل تشغيل) بـ٥٠٠ عميل مرتبين بالرقم."""
    rnd = random.Random(20261119)
    out, used, i = [], set(), 0
    for key, label, count, tpl, acts, (vmin, vmax), sector in GROUPS:
        for _ in range(count):
            i += 1
            for _try in range(50):
                act = rnd.choice(acts)
                if key == "individual":
                    name = tpl.format(p=f"{rnd.choice(FIRST)} {rnd.choice(LAST)}", a=act)
                else:
                    name = tpl.format(n=rnd.choice(NAMES), a=act)
                city = rnd.choices(CITIES, CITY_WEIGHTS)[0]
                if (name, city) not in used:
                    used.add((name, city))
                    break
            if key in ("small_est", "individual"):
                vehicles = 1 if rnd.random() < (0.55 if key == "small_est" else 0.8) else rnd.randint(2, vmax)
            else:
                vehicles = int(round(vmin + (vmax - vmin) * rnd.random() ** 2.2))  # أكثرها صغير، وقليل كبير
            out.append({"n": i, "group": key, "group_label": label, "name": name, "city": city,
                        "activity": ("ال" + act[2:]) if act.startswith("لل") else (act[1:] if act.startswith("ل") else act),
                        "vehicles": max(vehicles, 1), "sector": sector,
                        "email": f"client{i:03d}@example.test", "phone": f"+000{i:07d}",
                        "assign": rnd.random() < 0.6})
    assert len(out) == TOTAL and len({p["email"] for p in out}) == TOTAL
    return out


def seed(s) -> dict:
    reps = list(s.scalars(select(User).where(User.role == "rep", User.active.is_(True)).order_by(User.id)))
    existing = {v for (v,) in s.execute(select(Contact.value).where(Contact.kind == "email",
                                                                    Contact.value.like("client%@example.test")))}
    added = assigned = 0
    for p in _plan():
        if p["email"] in existing:
            continue
        owner = reps[(p["n"] - 1) % len(reps)] if (reps and p["assign"]) else None
        c = Company(name=p["name"], name_norm=normalize_name(p["name"]), activity=p["activity"],
                    sector_key=p["sector"], city=p["city"], data_status="complete", stage="new", is_test=True,
                    vehicles_estimate=p["vehicles"], lead_source="test_seed", operator_status="self",
                    fit_reason=f"بيانات تجريبية — {p['group_label']} — {p['vehicles']} مركبة",
                    owner_id=owner.id if owner else None, counted_on=None)
        s.add(c)
        s.flush()
        c.contacts.append(Contact(kind="phone", value=p["phone"], raw_value=p["phone"], source_url=SOURCE,
                                  source_type="test", verified=True, preferred=True, label="تجريبي"))
        c.contacts.append(Contact(kind="email", value=p["email"], raw_value=p["email"], source_url=SOURCE,
                                  source_type="test", verified=True, preferred=True, label="تجريبي"))
        s.add(Activity(company_id=c.id, kind="data", title="أُضيف كسجل تجريبي",
                       details=f"{p['group_label']} — مستبعد من الإرسال والاتصال والمتابعة الآلية"))
        added += 1
        assigned += 1 if owner else 0
    s.flush()
    # إن لم يكن هناك موظفون وقت التعبئة الأولى، يُسند الجزء المخطط (≈٦٠٪) عند أول إعادة تشغيل بعد إضافتهم
    if reps and not s.scalar(select(func.count(Company.id)).where(Company.is_test.is_(True), Company.owner_id.isnot(None))):
        plan = {p["email"]: p for p in _plan()}
        for c in s.scalars(select(Company).where(Company.is_test.is_(True))):
            em = next((ct.value for ct in c.contacts if ct.kind == "email"), None)
            p = plan.get(em)
            if p and p["assign"]:
                c.owner_id = reps[(p["n"] - 1) % len(reps)].id
                assigned += 1
    return {"added": added, "assigned": assigned, "staff": len(reps)}


def verify(s) -> dict:
    test_ids = select(Company.id).where(Company.is_test.is_(True))
    by_group = {}
    for label, n in s.execute(select(Company.fit_reason, func.count()).where(Company.is_test.is_(True))
                              .group_by(Company.fit_reason)):
        g = label.split(" — ")[1] if " — " in label else label
        by_group[g] = by_group.get(g, 0) + n
    return {
        "test_customers": s.scalar(select(func.count()).select_from(test_ids.subquery())),
        "unique_emails": s.scalar(select(func.count(func.distinct(Contact.value))).where(
            Contact.kind == "email", Contact.company_id.in_(test_ids))),
        "assigned": s.scalar(select(func.count(Company.id)).where(Company.is_test.is_(True), Company.owner_id.isnot(None))),
        "unassigned": s.scalar(select(func.count(Company.id)).where(Company.is_test.is_(True), Company.owner_id.is_(None))),
        "single_vehicle": s.scalar(select(func.count(Company.id)).where(Company.is_test.is_(True), Company.vehicles_estimate == 1)),
        "cities": s.scalar(select(func.count(func.distinct(Company.city))).where(Company.is_test.is_(True))),
        "not_contacted": s.scalar(select(func.count(Company.id)).where(Company.is_test.is_(True), Company.stage == "new")),
        "messages_sent": s.scalar(select(func.count(OutboundMessage.id)).where(
            OutboundMessage.company_id.in_(test_ids), OutboundMessage.sent_at.isnot(None))),
        "messages_queued": s.scalar(select(func.count(OutboundMessage.id)).where(
            OutboundMessage.company_id.in_(test_ids), OutboundMessage.status.in_(["queued", "sending"]))),
        "messages_any": s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.company_id.in_(test_ids))),
        "calls": s.scalar(select(func.count(Call.id)).where(Call.company_id.in_(test_ids))),
        "counted_in_daily_target": s.scalar(select(func.count(Company.id)).where(
            Company.is_test.is_(True), Company.counted_on.isnot(None))),
        "by_group": by_group,
    }


def main() -> None:
    init_db()
    with session_scope() as s:
        res = seed(s)
    with session_scope() as s:
        res["verify"] = verify(s)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
