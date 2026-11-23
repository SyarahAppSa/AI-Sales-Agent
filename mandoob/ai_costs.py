"""صفحة «تكلفة الذكاء الاصطناعي»: صرف Claude الفعلي من دفتر التكاليف (CostEntry.service == "claude")
حسب الفترة، مقارنةً بالفترة السابقة المساوية، مع الصرف اليومي/بالساعة، والتوزيع حسب الغرض، وتكلفة المنشأة المكتملة."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from .models import Company, CostEntry
from .timeutil import riyadh_today

SAR = 3.75
PERIODS = [("today", "اليوم"), ("yesterday", "أمس"), ("7d", "آخر 7 أيام"), ("30d", "آخر 30 يوم"),
           ("month", "هذا الشهر"), ("last_month", "الشهر الماضي"), ("90d", "آخر 90 يوم"), ("all", "الكل")]
PURPOSE = {"harvest": "جمع المنشآت", "complete": "استكمال بيانات التواصل", "retry": "إعادة محاولة الجمع",
           "reasons": "كتابة سبب الترشيح", "signals": "إشارات الفرص", "officials": "البحث عن أصحاب القرار",
           "agent": "وكيل البحث", "ai_search": "البحث الذكي", "website": "قراءة المواقع", "targeting": "اقتراح الفئات",
           "fit": "تقييم الملاءمة", "plan": "خطة البحث", "compose": "صياغة الرسائل", "reply": "تحليل الردود",
           "google_live": "بحث خرائط Google"}


def period_range(key: str, d_from: date | None = None, d_to: date | None = None, first: date | None = None) -> tuple[date, date]:
    t = riyadh_today()
    if d_from or d_to:
        a, b = d_from or d_to, d_to or d_from
        return (min(a, b), max(a, b))
    if key == "yesterday":
        return t - timedelta(days=1), t - timedelta(days=1)
    if key in ("7d", "30d", "90d"):
        return t - timedelta(days=int(key[:-1]) - 1), t
    if key == "month":
        return t.replace(day=1), t
    if key == "last_month":
        end = t.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if key == "all":
        return (first or t), t
    return t, t


def purpose_label(note: str) -> str:
    from .directory import SOURCES
    parts = (note or "").split(":")
    if parts and parts[0] == "dir" and len(parts) >= 2:
        if len(parts) == 2:
            return PURPOSE.get(parts[1], parts[1])
        src = SOURCES.get(parts[1], {}).get("label", parts[1])
        return f"{PURPOSE.get(parts[2], parts[2])} — {src}"
    head = parts[0] if parts else ""
    return PURPOSE.get(head, head or "غير مصنّف")


def _sum(s, a: date, b: date, service: str | None = "claude") -> tuple[float, int]:
    q = select(func.coalesce(func.sum(CostEntry.cost_usd), 0), func.count(CostEntry.id)).where(CostEntry.day >= a, CostEntry.day <= b)
    if service:
        q = q.where(CostEntry.service == service)
    c, n = s.execute(q).one()
    return float(c or 0), int(n or 0)


def _complete(s, a: date, b: date) -> int:
    from .goal import _proof
    return s.scalar(select(func.count(Company.id)).where(
        Company.counted_on >= a, Company.counted_on <= b, Company.data_status == "complete", Company.is_test.is_(False),
        Company.is_existing_customer.is_(False), _proof("phone", True), _proof("email"))) or 0


def _pct(cur: float, prev: float) -> int | None:
    if not prev:
        return None
    return round((cur - prev) / prev * 100)


def report(s, key: str = "today", d_from: date | None = None, d_to: date | None = None) -> dict:
    from . import goal
    first = s.scalar(select(func.min(CostEntry.day)).where(CostEntry.service == "claude"))
    a, b = period_range(key, d_from, d_to, first)
    days = (b - a).days + 1
    cost, calls = _sum(s, a, b)
    pa, pb = a - timedelta(days=days), a - timedelta(days=1)
    pcost, pcalls = _sum(s, pa, pb)
    complete = _complete(s, a, b)
    all_cost, _ = _sum(s, a, b, None)

    t = riyadh_today()
    m0 = t.replace(day=1)
    month_cost, _ = _sum(s, m0, t)
    dim = calendar.monthrange(t.year, t.month)[1]
    forecast = month_cost / t.day * dim if t.day else 0.0
    today_cost, today_calls = _sum(s, t, t)
    cap = float(goal.get(s).get("cost_cap_usd") or 0)

    # الصرف: بالساعة ليوم واحد، وباليوم لما سواه
    bars: list[dict] = []
    if days == 1:
        hours = {h: 0.0 for h in range(24)}
        for at, c in s.execute(select(CostEntry.at, CostEntry.cost_usd).where(CostEntry.service == "claude", CostEntry.day == a)):
            if at:
                hours[(at + timedelta(hours=3)).hour] += float(c or 0)
        bars = [{"label": f"{h:02d}", "value": round(v, 4)} for h, v in hours.items()]
        unit = "بالساعة"
    else:
        per = {a + timedelta(days=i): 0.0 for i in range(days)}
        for d, c in s.execute(select(CostEntry.day, func.sum(CostEntry.cost_usd)).where(
                CostEntry.service == "claude", CostEntry.day >= a, CostEntry.day <= b).group_by(CostEntry.day)):
            per[d] = float(c or 0)
        bars = [{"label": f"{d.day}/{d.month}", "value": round(v, 4)} for d, v in per.items()]
        unit = "باليوم"
    peak = max([x["value"] for x in bars] + [0])

    purposes: dict[str, list] = {}
    for note, c, n in s.execute(select(CostEntry.note, func.sum(CostEntry.cost_usd), func.count(CostEntry.id)).where(
            CostEntry.service == "claude", CostEntry.day >= a, CostEntry.day <= b).group_by(CostEntry.note)):
        lab = purpose_label(note)
        row = purposes.setdefault(lab, [0.0, 0])
        row[0] += float(c or 0)
        row[1] += int(n or 0)
    by_purpose = sorted(({"label": k, "cost": v[0], "calls": v[1], "share": (v[0] / cost * 100) if cost else 0}
                         for k, v in purposes.items()), key=lambda x: -x["cost"])

    from .ai import CAP_LABELS, cap_category, get_caps
    caps = get_caps(s)
    used = {k: 0.0 for k in caps}
    for nt, c in s.execute(select(CostEntry.note, CostEntry.cost_usd).where(CostEntry.day == t, CostEntry.service == "claude")):
        k = cap_category(nt or "")
        if k in used:
            used[k] += float(c or 0)
    cap_rows = [{"key": k, "label": CAP_LABELS.get(k, k), "cap": float(v or 0), "used": round(used.get(k, 0.0), 2)}
                for k, v in caps.items()]
    return {"cap_rows": cap_rows, "key": key if not (d_from or d_to) else "custom", "from": a, "to": b, "days": days,
            "cost": cost, "calls": calls, "avg": cost / calls if calls else 0.0, "searches": None,
            "cost_pct": _pct(cost, pcost), "calls_pct": _pct(calls, pcalls), "prev_cost": pcost,
            "complete": complete, "per_complete": cost / complete if complete else None, "all_cost": all_cost,
            "month_cost": month_cost, "forecast": forecast, "today_cost": today_cost, "today_calls": today_calls,
            "cap": cap, "cap_pct": min(100.0, today_cost / cap * 100) if cap else 0.0,
            "bars": bars, "peak": peak, "unit": unit, "by_purpose": by_purpose, "SAR": SAR, "first": first}


def rows_for_export(r: dict) -> list[list]:
    out = [["الفترة", f"{r['from']} — {r['to']}"], ["تكلفة Claude ($)", round(r["cost"], 4)],
           ["تكلفة Claude (ر.س)", round(r["cost"] * SAR, 2)], ["الاستدعاءات", r["calls"]],
           ["متوسط الاستدعاء ($)", round(r["avg"], 5)], ["المنشآت المكتملة", r["complete"]],
           ["تكلفة المنشأة المكتملة ($)", round(r["per_complete"], 4) if r["per_complete"] is not None else ""], [],
           ["الغرض", "التكلفة ($)", "الاستدعاءات", "النسبة %"]]
    out += [[p["label"], round(p["cost"], 4), p["calls"], round(p["share"], 1)] for p in r["by_purpose"]]
    out += [[], [f"الصرف {r['unit']}", "التكلفة ($)"]] + [[x["label"], x["value"]] for x in r["bars"]]
    return out
