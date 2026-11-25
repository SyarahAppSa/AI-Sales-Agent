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


BONUS = {"offer_sent": 2, "new_request": 3, "subscribed": 5}  # تُحسب كتواصلات إضافية في النقاط
MILESTONES = [(60, "😍🏆", "حققت الهدف! 60 تواصل — أنت بطل اليوم", "كل تواصل بعد الحين زيادة في رصيدك 🔥"),
              (45, "🔥🎉", "وصلت 45 — الهدف صار قريب!", "باقي لك 15 تواصل بس 💪"),
              (30, "😎🎉", "وصلت 30 — نص الطريق يا بطل!", "باقي لك 30 تواصل وتحقق هدف اليوم 💪"),
              (15, "🙂🎉", "وصلت 15 — بداية قوية!", "كمّل بنفس الحماس، باقي لك 45 💪")]
BADGES = [("fast", "⚡ أسرع بداية", "أول تواصل اليوم قبل 9 الصبح"),
          ("follow", "📋 ملك المتابعة", "لا توجد متابعات متأخرة"),
          ("offers", "🎯 صائد العروض", "أكثر من أرسل عروضًا اليوم"),
          ("subs", "🏆 نجم الاشتراكات", "سجّل اشتراكًا اليوم")]
BRAVO_KEY = "bravo"


def bonus(by: dict) -> int:
    return sum(BONUS[k] * int(by.get(k, 0) or 0) for k in BONUS)


def score(total: int, goal: int, extra: int = 0) -> float:
    return round(min(10.0, (total + extra) / goal * 10), 1) if goal else 0.0


def _daily(s, uids, days: int = 120) -> dict:
    """{uid: {يوم الرياض: {الحالة: عدد}}} لمحاولات التواصل الحقيقية + أول وقت تواصل اليوم."""
    from .contact import ATTEMPT_STATUSES
    from .models import Company, ContactLog
    from .timeutil import now_utc, to_riyadh
    since = now_utc() - timedelta(days=days)
    out: dict = {}
    q = (select(ContactLog.user_id, ContactLog.at, ContactLog.status).join(Company, Company.id == ContactLog.company_id)
         .where(ContactLog.at >= since, ContactLog.status.in_(ATTEMPT_STATUSES), Company.is_test.is_(False)))
    if uids is not None:
        q = q.where(ContactLog.user_id.in_(list(uids)))
    for uid, at, st in s.execute(q):
        if at is None:
            continue
        r = to_riyadh(at)
        d = out.setdefault(uid, {}).setdefault(r.date(), {"_n": 0, "_first": None, "_times": []})
        d["_n"] += 1
        d[st] = d.get(st, 0) + 1
        d["_times"].append(r)
        if d["_first"] is None or r < d["_first"]:
            d["_first"] = r
    return out


def streak(days: dict, today: date) -> int:
    """أيام العمل المتتالية التي حقق فيها الهدف (اليوم يُحسب إن تحقق، ولا يكسر السلسلة إن لم يكتمل بعد)."""
    n, d = 0, today
    if (days.get(today) or {}).get("_n", 0) >= DAILY_GOAL:
        n += 1
    d = today - timedelta(days=1)
    for _ in range(120):
        if d.weekday() in (4, 5):
            d -= timedelta(days=1)
            continue
        if (days.get(d) or {}).get("_n", 0) >= DAILY_GOAL:
            n += 1
            d -= timedelta(days=1)
        else:
            break
    return n


def best(days: dict, today: date) -> int:
    return max([v["_n"] for k, v in days.items() if k != today] + [0])


def _overdue(s, uid) -> int:
    from .models import Company, Task
    from .timeutil import riyadh_day_bounds_utc, riyadh_today
    start, _ = riyadh_day_bounds_utc(riyadh_today())
    return s.scalar(select(func.count(Task.id)).join(Company, Company.id == Task.company_id).where(
        Task.status == "open", Task.owner_id == uid, Task.due_at < start, Company.is_test.is_(False))) or 0


def badges(s, uid, today_row: dict, top_offers: int) -> list[str]:
    out = []
    f = today_row.get("_first")
    if f is not None and f.hour < 9:
        out.append("fast")
    if today_row.get("_n") and _overdue(s, uid) == 0:
        out.append("follow")
    if top_offers and today_row.get("offer_sent", 0) == top_offers:
        out.append("offers")
    if today_row.get("subscribed"):
        out.append("subs")
    return out


def suspicious(times: list, n: int = 10, minutes: int = 3) -> bool:
    """تحديثات كثيرة في وقت قصير: n تحديثات خلال دقائق قليلة."""
    t = sorted(times)
    return any((t[i + n - 1] - t[i]).total_seconds() <= minutes * 60 for i in range(len(t) - n + 1))


def team_today(s) -> dict:
    """هدف الفريق المشترك اليوم: 60 × الموظفين النشطين الذين لديهم عملاء، مقابل كل محاولات التواصل اليوم."""
    from .contact import ATTEMPT_STATUSES
    from .models import Company, ContactLog, User
    from .timeutil import riyadh_day_bounds_utc, riyadh_today
    t0, t1 = riyadh_day_bounds_utc(riyadh_today())
    members = s.scalar(select(func.count(func.distinct(User.id))).join(Company, Company.owner_id == User.id).where(
        User.active.is_(True), User.role.in_(("rep", "manager")), Company.is_test.is_(False))) or 0
    done = s.scalar(select(func.count(ContactLog.id)).join(Company, Company.id == ContactLog.company_id).where(
        ContactLog.at >= t0, ContactLog.at < t1, ContactLog.status.in_(ATTEMPT_STATUSES), Company.is_test.is_(False))) or 0
    goal = members * DAILY_GOAL
    return {"done": done, "goal": goal, "pct": min(100, round(done / goal * 100)) if goal else 0}


def bravos(s, day: date) -> dict:
    from .models import Setting
    row = s.get(Setting, f"{BRAVO_KEY}:{day.isoformat()}")
    return dict(row.value or {}) if row else {}


def send_bravo(s, by, uid: int) -> bool:
    from .models import Notification, Setting, User
    from .timeutil import now_utc, riyadh_today
    u = s.get(User, uid)
    if u is None or not u.active:
        return False
    key = f"{BRAVO_KEY}:{riyadh_today().isoformat()}"
    row = s.get(Setting, key)
    v = dict(row.value or {}) if row else {}
    nm = (by.name or "الإدارة").strip()
    first = nm if nm.startswith("مدير") else nm.split()[0]
    v[str(uid)] = {"by": first, "at": now_utc().isoformat()}
    if row is None:
        s.add(Setting(key=key, value=v))
    else:
        row.value = v
    s.add(Notification(user_id=uid, title=f"👏 برافو من {first}!",
                       body="برافو على شغلك اليوم — كمّل كذا! 💪"))
    return True


NEXT = [(15, "🙂", "بداية جيدة"), (30, "😎", "أداء قوي"), (45, "🔥", "قريب من الهدف"), (60, "😍", "متصدر — حقق الهدف")]


def next_level(total: int):
    for n, face, label in NEXT:
        if total < n:
            return {"at": n, "face": face, "label": label, "left": n - total}
    return None


def milestone(total: int):
    for m in MILESTONES:
        if total >= m[0]:
            return m
    return None


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
    daily = _daily(s, {x["uid"] for x in rows})
    top_offers = max([(daily.get(x["uid"], {}).get(t) or {}).get("offer_sent", 0) for x in rows] + [0])
    bv = bravos(s, t)
    flags = []
    for x in rows:
        days = daily.get(x["uid"], {})
        today_row = days.get(t) or {}
        x.update(level(x["total"], goal))
        x["bonus"] = bonus(x)
        x["points"] = score(x["total"], goal, x["bonus"])
        x["pct"] = min(100, round(x["total"] / goal * 100)) if goal else 0
        x["streak"] = streak(days, t)
        x["badges"] = [lab for k, lab, _ in BADGES if k in badges(s, x["uid"], today_row, top_offers)]
        x["bravo"] = str(x["uid"]) in bv
        if suspicious(today_row.get("_times") or []):
            flags.append(x["name"])
    rows.sort(key=lambda x: (-x["points"], -x["total"], x["name"]))
    return {"rows": rows, "goal": goal, "key": key, "PERIODS": PERIODS, "phrase": today_phrase(t), "from": a, "to": b,
            "team": team_today(s), "flags": flags, "BONUS": BONUS}


def personal(s, user) -> dict:
    """تقدّم الموظف اليوم: محاولاته وتوزيعها، والباقي للهدف، والمستوى، والسلسلة والرقم القياسي والأوسمة، وعبارة اليوم."""
    from .models import User
    from .timeutil import riyadh_today
    t = riyadh_today()
    days = _daily(s, {user.id}, 120).get(user.id, {})
    daily = _daily(s, None, 1)
    today_row = days.get(t) or {}
    by = {k: v for k, v in today_row.items() if not k.startswith("_")}
    total = today_row.get("_n", 0)
    top_offers = max([(d.get(t) or {}).get("offer_sent", 0) for d in daily.values()] + [0])
    got = badges(s, user.id, today_row, top_offers)
    pb = best(days, t)
    first = (user.name or "").split()[0] if user.name else ""
    bv = bravos(s, t).get(str(user.id))
    ex = bonus(by)
    return {"total": total, "goal": DAILY_GOAL, "left": max(DAILY_GOAL - total, 0), "by": by,
            "pct": min(100, round(total / DAILY_GOAL * 100)), "phrase": today_phrase(t), "first": first,
            "streak": streak(days, t), "best": pb, "to_best": (pb + 1 - total) if pb and total <= pb else 0,
            "bonus": ex, "points": score(total, DAILY_GOAL, ex), "badges": [(lab, k in got, why) for k, lab, why in BADGES],
            "team": team_today(s), "bravo": bv, "milestone": milestone(total), "next": next_level(total),
            **level(total, DAILY_GOAL)}
