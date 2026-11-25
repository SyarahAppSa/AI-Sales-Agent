"""لوحة صدارة الفريق (للمالك ومن يحق له رؤية تقرير الفريق) + عبارة تحفيزية يومية + تقدّم الموظف الشخصي.
النقاط: هدف الموظف 60 تواصل يوميًا = 10 نقاط (كل 6 تواصلات نقطة). في الأسبوع والشهر يُضرب الهدف في أيام العمل
(الأحد–الخميس) المنقضية من الفترة."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select

DAILY_GOAL = 60
PHRASES = [
 "كل مكالمة اليوم خطوة أقرب لعميل جديد في سيارة اب 🚗",
 "العميل اللي ما رد أمس، ممكن يكون صفقتك اليوم 📞",
 "الإصرار يصنع الفرق — 60 تواصل اليوم وتشوف النتيجة 💪",
 "ابتسامتك تُسمع في المكالمة، خلّها حاضرة 😊",
 "كل «لا» تقرّبك من «نعم» — كمّل 🔥",
 "اليوم فرصة جديدة تكون الأول في لوحة الصدارة 🏆",
 "اسأل عن احتياج العميل قبل ما تعرض — الاستماع نصف البيع 👂",
 "عميل واحد مشترك اليوم أفضل من عشرة «بعدين» 🎯",
 "رتّب متابعاتك أول الدوام، وابدأ بالأسخن 📋",
 "خلك واضح ومختصر: العميل يحب اللي يحترم وقته ⏱️",
 "التميز عادة يومية، مو صدفة ✨",
 "كل ملاحظة تكتبها اليوم توفر على زميلك وقت بكرة 📝",
 "النجاح مجموع جهود صغيرة تتكرر كل يوم 🌱",
 "اتصالك اليوم ممكن يكون بداية شراكة طويلة 🤝",
 "ركّز على الأسطول الكبير: عميل واحد ممكن يساوي عشرة 🚚",
 "لا تأجل المكالمة الصعبة — ابدأ فيها وارتاح 💡",
 "إنجازك اليوم يبني سمعتك بكرة 🌟",
 "المتابعة في وقتها تكسب ثقة العميل ⏰",
 "كل عرض ترسله بذرة؛ تابعها لين تثمر 🌳",
 "فريق قوي = أهداف تتحقق. أنت جزء منه 💜",
 "خلك فضولي: سؤال واحد زيادة ممكن يفتح صفقة 🔍",
 "الحماس معدي — خله يبدأ منك اليوم ⚡",
 "اهدأ، ابتسم، واتصل. الباقي يجي 😌",
 "العميل يتذكر شعوره معك أكثر من كلامك 💬",
 "هدفك اليوم 60 تواصل — أول 10 أهم 10، ابدأ الحين 🚀",
 "لا تقارن نفسك بغيرك، قارنها بنفسك أمس 📈",
 "كل رقم في قائمتك فرصة تنتظر صوتك 📱",
 "الالتزام يتفوق على الموهبة لما الموهبة ما تلتزم 🏅",
 "سجّل النتيجة بعد كل مكالمة — دقيقة توفر ساعة ✅",
 "نهاية الشهر تُكتب من بدايته — خلك قوي اليوم 💥",
 "شكرًا على جهدك — اليوم يوم جديد ونجاح جديد 🌞",
]


def today_phrase(day: date) -> str:
    """نفس العبارة طوال اليوم لكل الفريق، وتتغير يوميًا."""
    return PHRASES[day.toordinal() % len(PHRASES)]


LEVELS = [  # (نسبة الهدف الدنيا، الوجه، الصنف، الوصف، النجوم)
    (1.0, "😍", "star", "متصدر — حقق الهدف", 5),
    (0.75, "🔥", "fire", "قريب من الهدف", 4),
    (0.5, "😎", "ok", "أداء قوي", 3),
    (0.25, "🙂", "ok", "بداية جيدة", 2),
    (0.0001, "😐", "low", "يحتاج دفعة", 1),
    (0.0, "😢", "sad", "لم يبدأ بعد", 0),
]


def level(total: int, goal: int) -> dict:
    p = total / goal if goal else 0
    for lo, face, cls, label, st in LEVELS:
        if p >= lo:
            return {"face": face, "cls": cls, "label": label, "stars": "★" * st + "☆" * (5 - st)}
    return {"face": "😢", "cls": "sad", "label": "لم يبدأ بعد", "stars": "☆" * 5}


def work_days(a: date, b: date) -> int:
    """أيام العمل (الأحد–الخميس) من a إلى b شاملة، ولا تقل عن يوم."""
    n, d = 0, a
    while d <= b:
        if d.weekday() not in (4, 5):  # الجمعة والسبت
            n += 1
        d += timedelta(days=1)
    return max(n, 1)


PERIODS = [("day", "اليوم"), ("week", "هذا الأسبوع"), ("month", "هذا الشهر")]


def period_range(key: str, today: date) -> tuple[date, date]:
    if key == "week":
        return today - timedelta(days=(today.weekday() + 1) % 7), today  # من الأحد
    if key == "month":
        return today.replace(day=1), today
    return today, today


def score(total: int, goal: int) -> float:
    return round(min(10.0, total / goal * 10), 1) if goal else 0.0


def board(s, user, key: str = "day") -> dict:
    from .models import User
    from .reports import team_activity
    from .timeutil import riyadh_today
    t = riyadh_today()
    key = key if key in dict(PERIODS) else "day"
    a, b = period_range(key, t)
    goal = DAILY_GOAL * (1 if key == "day" else work_days(a, b))
    r = team_activity(s, user, a, b)
    active = {u.id for u in s.scalars(select(User).where(User.active.is_(True), User.role.in_(("rep", "manager"))))}
    rows = [x for x in r["reps"] if x.get("uid") in active]
    for x in rows:
        x.update(level(x["total"], goal))
        x["points"] = score(x["total"], goal)
        x["pct"] = min(100, round(x["total"] / goal * 100)) if goal else 0
    rows.sort(key=lambda x: (-x["total"], -x["contacted"], x["name"]))
    return {"rows": rows, "goal": goal, "key": key, "PERIODS": PERIODS, "phrase": today_phrase(t), "from": a, "to": b}


def personal(s, user) -> dict:
    """تقدّم الموظف اليوم: عدد محاولات التواصل وتوزيعها، والباقي للهدف، والمستوى، وعبارة اليوم."""
    from .contact import ATTEMPT_STATUSES
    from .models import Company, ContactLog
    from .timeutil import riyadh_day_bounds_utc, riyadh_today
    t = riyadh_today()
    t0, t1 = riyadh_day_bounds_utc(t)
    by = dict(s.execute(select(ContactLog.status, func.count(ContactLog.id)).join(Company, Company.id == ContactLog.company_id)
                        .where(ContactLog.user_id == user.id, ContactLog.at >= t0, ContactLog.at < t1,
                               ContactLog.status.in_(ATTEMPT_STATUSES), Company.is_test.is_(False))
                        .group_by(ContactLog.status)).all())
    total = sum(by.values())
    first = (user.name or "").split()[0] if user.name else ""
    return {"total": total, "goal": DAILY_GOAL, "left": max(DAILY_GOAL - total, 0), "by": by,
            "pct": min(100, round(total / DAILY_GOAL * 100)), "phrase": today_phrase(t), "first": first,
            **level(total, DAILY_GOAL)}
