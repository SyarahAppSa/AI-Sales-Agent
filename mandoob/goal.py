"""الهدف اليومي الموحّد: إعداد واحد (افتراضيًا ٢٠٠ منشأة جديدة مكتملة) تقرؤه الجدولة والحملة اليومية ولوحة التحكم
والتقرير اليومي و«اكتشاف عملاء جدد» و«العملاء المحتملون».

تعريف المنشأة المحتسبة (مرة واحدة يوم اكتمالها): جوال سعودي (+966) وبريد منشوران، لكل منهما رابط مصدر يثبته؛
ويُستبعد العملاء الحاليون والبيانات التجريبية والمكررات (المكرر لا يُنشأ سجلًا جديدًا أصلًا).
يتوقف جمع العملاء الجدد لذلك اليوم عند بلوغ الهدف أو حد التكلفة اليومي.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import exists, func, select

from .db import session_scope
from .models import Company, Contact, CostEntry, Setting
from .timeutil import now_riyadh, riyadh_today

KEY = "daily_goal"
DEFAULT = {"target": 200, "cost_cap_usd": 50.0}


def get(s) -> dict:
    row = s.get(Setting, KEY)
    return {**DEFAULT, **((row.value if row else None) or {})}


def target(s) -> int:
    return int(get(s)["target"])


def _proof(kind: str, phone: bool = False):
    q = select(Contact.id).where(Contact.company_id == Company.id, Contact.kind == kind,
                                 Contact.source_url.like("http%"))
    if phone:
        q = q.where(Contact.value.like("+966%"))
    return exists(q)


def counted_filter(day: date):
    return (Company.counted_on == day, Company.data_status == "complete", Company.is_test.is_(False),
            Company.is_existing_customer.is_(False), _proof("phone", True), _proof("email"))


def achieved(s, day: date | None = None) -> int:
    return s.scalar(select(func.count(Company.id)).where(*counted_filter(day or riyadh_today()))) or 0


def cost_today(s, day: date | None = None) -> float:
    return round(float(s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0))
                                .where(CostEntry.day == (day or riyadh_today()))) or 0), 2)


def halt_reason() -> str | None:
    """يُسأل قبل كل خطوة جمع: هل نتوقف اليوم؟"""
    with session_scope() as s:
        g = get(s)
        have = achieved(s)
        if have >= int(g["target"]):
            return f"بلغ هدف اليوم: {have} من {g['target']}"
        spent = cost_today(s)
        if g.get("cost_cap_usd") and spent >= float(g["cost_cap_usd"]):
            return f"بلغ حد التكلفة اليومي (${spent:.2f} من ${float(g['cost_cap_usd']):.2f})"
    return None


def shortfall_reason(s, day: date | None = None) -> str:
    """سبب النقص بالعربية (يُعرض بجانب «١٦٠ من ٢٠٠»)."""
    from .models import DirectoryRun, DiscoveryRun
    from .timeutil import riyadh_day_bounds_utc
    day = day or riyadh_today()
    g = get(s)
    have = achieved(s, day)
    if have >= int(g["target"]):
        return ""
    reasons = []
    spent = cost_today(s, day)
    if g.get("cost_cap_usd") and spent >= float(g["cost_cap_usd"]):
        reasons.append(f"بلغ حد التكلفة اليومي (${spent:.2f})")
    try:
        from .directory import get_auto
        auto = get_auto(s)
        a, b = riyadh_day_bounds_utc(day)
        runs = list(s.scalars(select(DirectoryRun).where(DirectoryRun.created_at >= a, DirectoryRun.created_at < b)))
        if not auto.get("enabled") and not runs:
            reasons.append("الجمع اليومي التلقائي متوقف («اكتشاف عملاء جدد»)")
        elif not runs and day == riyadh_today():
            hh, mm = (int(x) for x in str(auto.get("time", "05:30")).split(":"))
            now = now_riyadh()
            if (now.hour, now.minute) < (hh, mm):
                reasons.append(f"لم يبدأ الجمع اليومي بعد (يبدأ {auto.get('time')})")
        for r in runs:
            if r.status in ("pending", "running"):
                reasons.append("الجمع ما زال جاريًا")
            last = (r.log or [""])[-1]
            if "توقف" in last:
                reasons.append(last.split(" ", 1)[-1][:160])
            st = (r.stats or {}).get("overture") or {}
            if st.get("incomplete"):
                reasons.append(f"{st['incomplete']} منشأة جديدة ينقصها هاتف أو بريد منشور في موقعها (قيد الاستكمال)")
    except Exception:  # noqa: BLE001
        pass
    for r in s.scalars(select(DiscoveryRun).where(DiscoveryRun.run_date == day)):
        if r.shortfall_reason:
            reasons.append(r.shortfall_reason.split("\n")[0][:160])
    out = list(dict.fromkeys(x for x in reasons if x))
    return "؛ ".join(out[:4]) or "المصادر المتاحة اليوم لم تُخرج منشآت جديدة مكتملة بعدد كافٍ"


def set_goal(s, n: int, cost_cap_usd: float | None = None) -> dict:
    """يحفظ الهدف ويطبّقه على كل ما يقرؤه: الجدولة والحملة اليومية و«اكتشاف عملاء جدد» و«العملاء المحتملون»."""
    from . import prospecting
    from .models import Campaign
    n = max(1, min(int(n), 5000))
    v = get(s)
    v["target"] = n
    if cost_cap_usd is not None:
        v["cost_cap_usd"] = max(0.0, float(cost_cap_usd))
    row = s.get(Setting, KEY)
    if row is None:
        s.add(Setting(key=KEY, value=v))
    else:
        row.value = v
    cfg = prospecting.get_config(s)  # الحملة اليومية «البحث عن العملاء» وحصص قطاعاتها
    raw = s.get(Setting, prospecting.SETTING)
    if raw is not None:
        raw.value = {**dict(raw.value or {}), "daily_target": n}
    if cfg.get("campaign_id"):
        camp = s.get(Campaign, cfg["campaign_id"])
        if camp is not None:
            camp.daily_target = n
            from .models import Category
            s.flush()
            act = list(s.scalars(select(Category).where(Category.campaign_id == camp.id, Category.active.is_(True))
                                 .order_by(Category.id)))
            for c, q in zip(act, prospecting.split_quota(n, len(act))):
                c.quota = q
    from . import directory, prospects
    directory.set_auto(s, target_complete=n)
    prospects.set_auto(s, target=n)
    return v
