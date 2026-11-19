"""أداة «البحث عن العملاء» في لوحة الإدارة: المدن والمناطق والأنشطة والهدف اليومي، والتشغيل الفوري أو المجدول.

- الإعداد محفوظ في الخادم (Setting «prospecting») وتمثّله حملة بحث واحدة لا ترسل شيئًا (send_enabled=False).
- «ابحث الآن» لا يعمل داخل المتصفح: يُسجَّل طلب يلتقطه العامل على الخادم خلال دقيقة، فيكمل ولو أُغلقت اللوحة.
- «التشغيل اليومي» = الحملة نشطة، فيشغّلها العامل في الوقت المحدد بتوقيت الرياض.
- المنشأة تُحتسب مرة واحدة عبر كل الدفعات (منع التكرار بالاسم والنطاق والهاتف والبريد).
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from .models import Campaign, Category, Company, CostEntry, DiscoveryRun
from .segments import REGIONS
from .timeutil import now_utc, riyadh_today

SETTING = "prospecting"
REQUEST = "discovery_request"

MAIN_CITIES = ["الرياض", "جدة", "مكة المكرمة", "المدينة المنورة", "الدمام", "الخبر", "الجبيل", "الأحساء", "القطيف",
               "الطائف", "بريدة", "عنيزة", "تبوك", "أبها", "خميس مشيط", "حائل", "جازان", "نجران", "ينبع", "الخرج",
               "حفر الباطن", "الباحة", "سكاكا", "عرعر"]

# الأنشطة: من دليل الأنشطة (mandoob/activities.py) — قطاعات كاملة أو أنشطة محددة أو «جميع الأنشطة في السعودية»
from .activities import ACTIVITIES, DEFAULT_ACTIVITIES, SECTORS, activity_hints, by_sector, normalize_selection, \
    sector_queries  # noqa: E402

DEFAULT = {"cities": ["الرياض", "جدة", "الدمام"], "regions": [], "activities": DEFAULT_ACTIVITIES, "all": False,
           "daily_target": 400, "scheduled": False, "time": "06:00", "campaign_id": None}


def get_config(s) -> dict:
    from .models import Setting
    row = s.get(Setting, SETTING)
    cfg = {**DEFAULT, **(dict(row.value) if row else {})}
    cfg["activities"] = list(ACTIVITIES) if cfg.get("all") else (normalize_selection(cfg["activities"]) or DEFAULT_ACTIVITIES)
    from . import geo
    if not cfg.get("scope"):
        # إعداد سابق بأسماء مدن ومناطق: يُحوَّل بنفس معناه (مدينة وحدها تبقى وحدها)
        cfg["scope"] = geo.legacy_scope(cfg.get("cities") or [], cfg.get("regions") or [])
    cfg["scope"] = geo.normalize_scope(cfg["scope"])
    cfg["scope_summary"] = geo.scope_summary(cfg["scope"])
    return cfg


class ScopeError(ValueError):
    """نطاق فارغ أو اسم غامض يحتاج اختيارًا صريحًا."""


def _put(s, key: str, value: dict) -> None:
    from .models import Setting
    row = s.get(Setting, key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))
        s.flush()  # حتى يراه أي قراءة لاحقة في الجلسة نفسها


def resolve_cities(cities: list[str], regions: list[str]) -> list[str]:
    out = list(cities)
    for r in regions:
        out += REGIONS.get(r, [])
    return list(dict.fromkeys(c.strip() for c in out if c and c.strip()))


def split_quota(total: int, n: int) -> list[int]:
    base, extra = divmod(max(total, 0), max(n, 1))
    return [base + (1 if i < extra else 0) for i in range(n)]


def save_config(s, *, activities: list[str], daily_target: int, scheduled: bool, time: str,
                cities: list[str] | None = None, regions: list[str] | None = None, scope: dict | None = None,
                user_id: int | None = None, all_activities: bool = False) -> Campaign:
    """يحفظ الإعداد ويحدّث حملة البحث: فئة لكل قطاع (بعبارات أنشطته المختارة)، والهدف اليومي موزع بالتساوي على القطاعات."""
    cfg = get_config(s)
    all_activities = all_activities or "__all__" in (activities or [])
    activities = list(ACTIVITIES) if all_activities else (normalize_selection(activities) or DEFAULT_ACTIVITIES)
    groups = by_sector(activities)
    from . import geo
    scope = geo.normalize_scope(scope if scope is not None else geo.legacy_scope(cities or [], regions or []))
    places = geo.scope_places(scope)
    if not places:
        raise ScopeError("حدد نطاق البحث: «جميع مناطق المملكة» أو مناطق ومدن بعينها")
    all_cities = [geo.PLACES[p]["ar"] for p in places]
    daily_target = max(1, min(int(daily_target or 400), 2000))
    camp = s.get(Campaign, cfg["campaign_id"]) if cfg.get("campaign_id") else None
    if camp is None:
        camp = Campaign(name="البحث عن العملاء", request_text="", status="paused", send_enabled=False,
                        created_by=user_id, approved_at=now_utc())
        s.add(camp)
        s.flush()
    what = "جميع الأنشطة في السعودية" if all_activities else "، ".join(SECTORS[k]["label"] for k in groups)
    camp.request_text = f"منشآت داخل السعودية — {what} — النطاق: {geo.scope_summary(scope)}"
    camp.geo_scope = scope
    camp.daily_target, camp.send_enabled = daily_target, False
    camp.status = "active" if scheduled else "paused"
    existing = {c.sector_key: c for c in camp.categories}
    quotas = split_quota(daily_target, len(groups))
    keep = set()
    for (sector, keys), q in zip(groups.items(), quotas):
        cat = existing.get(sector)
        if cat is None:
            cat = Category(campaign_id=camp.id, sector_key=sector, label=SECTORS[sector]["label"], cities=[],
                           keywords_ar=[], keywords_en=[], quota=0)
            s.add(cat)
        cat.label = SECTORS[sector]["label"]
        cat.cities, cat.keywords_ar, cat.quota, cat.active = all_cities, sector_queries(keys), q, True
        cat.priority_signals = [activity_hints(keys)]
        keep.add(sector)
    for key, cat in existing.items():
        if key not in keep:
            cat.active, cat.quota = False, 0
    new_cfg = {"scope": scope, "activities": activities, "all": bool(all_activities),
               "daily_target": daily_target,
               "scheduled": bool(scheduled), "time": time or "06:00", "campaign_id": camp.id}
    _put(s, SETTING, new_cfg)
    # وقت التشغيل اليومي (بتوقيت الرياض) يقرؤه العامل على الخادم
    from .models import Setting
    row = s.get(Setting, "schedule")
    sched = dict(row.value) if row else {"discovery_time": "06:00", "daily_report_time": "18:00"}
    sched["discovery_time"] = new_cfg["time"]
    _put(s, "schedule", sched)
    return camp


def request_run(s, campaign_id: int, max_new: int | None, user_id: int | None) -> dict:
    """«ابحث الآن»: طلب يلتقطه العامل على الخادم (لا يعتمد على بقاء المتصفح مفتوحًا)."""
    from .models import Setting
    row = s.get(Setting, REQUEST)
    cur = dict(row.value) if row else {}
    if cur.get("state") in ("pending", "running"):
        return cur
    req = {"state": "pending", "campaign_id": campaign_id, "max_new": max_new, "by": user_id,
           "requested_at": now_utc().isoformat(timespec="seconds")}
    _put(s, REQUEST, req)
    return req


def pending_request(s) -> dict:
    from .models import Setting
    row = s.get(Setting, REQUEST)
    return dict(row.value) if row else {}


def history_cost_per_complete(s) -> float | None:
    """متوسط التكلفة الفعلية لكل منشأة مكتملة في آخر ١٤ يومًا (لتقدير تكلفة الهدف)."""
    since = riyadh_today() - timedelta(days=14)
    cost = s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(CostEntry.day >= since)) or 0
    done = s.scalar(select(func.count(Company.id)).where(Company.counted_on >= since, Company.is_test.is_(False),
                                                         Company.data_status == "complete")) or 0
    return round(float(cost) / done, 3) if done and cost else None


def status(s) -> dict:
    cfg = get_config(s)
    camp = s.get(Campaign, cfg["campaign_id"]) if cfg.get("campaign_id") else None
    today = riyadh_today()
    run = s.scalar(select(DiscoveryRun).where(DiscoveryRun.campaign_id == camp.id, DiscoveryRun.run_date == today)) \
        if camp else None
    runs = list(s.scalars(select(DiscoveryRun).where(DiscoveryRun.campaign_id == camp.id)
                          .order_by(DiscoveryRun.id.desc()).limit(10))) if camp else []
    done_today = s.scalar(select(func.count(Company.id)).where(
        Company.campaign_id == camp.id, Company.counted_on == today, Company.data_status == "complete")) if camp else 0
    incomplete_today = 0
    if camp:
        from .timeutil import riyadh_day_bounds_utc
        a, b = riyadh_day_bounds_utc(today)
        incomplete_today = s.scalar(select(func.count(Company.id)).where(
            Company.campaign_id == camp.id, Company.data_status == "incomplete",
            Company.first_found_at >= a, Company.first_found_at < b)) or 0
    sectors = []
    if camp:
        from .timeutil import riyadh_day_bounds_utc
        a, b = riyadh_day_bounds_utc(today)
        for cat in sorted([c for c in camp.categories if c.active], key=lambda c: c.id):
            base = select(func.count(Company.id)).where(Company.category_id == cat.id)
            sectors.append({"label": cat.label, "quota": cat.quota,
                            "complete": s.scalar(base.where(Company.counted_on == today,
                                                            Company.data_status == "complete")) or 0,
                            "incomplete": s.scalar(base.where(Company.data_status == "incomplete",
                                                              Company.first_found_at >= a,
                                                              Company.first_found_at < b)) or 0,
                            "confirmed": s.scalar(base.where(Company.counted_on == today,
                                                             Company.vehicle_indicator == "confirmed")) or 0,
                            "activities": len([k for k in cfg["activities"] if ACTIVITIES[k]["sector"] == cat.sector_key])})
    regions = region_table(s, camp, cfg, run) if camp else []
    nexts = []
    if camp and camp.geo_scope:
        from . import geo
        pl = geo.scope_places(camp.geo_scope)
        for cat in sorted([c for c in camp.categories if c.active], key=lambda c: c.id):
            qs = list(cat.keywords_ar) + list(cat.keywords_en)
            n = len(pl) * len(qs)
            if n:
                i = (cat.cursor or 0) % n
                pid, q = pl[i % len(pl)], qs[i // len(pl)]
                nexts.append({"label": cat.label, "place": geo.label(pid, True), "query": q, "pos": i + 1, "of": n})
    total_complete = s.scalar(select(func.count(Company.id)).where(
        Company.is_test.is_(False), Company.lead_source == "discovery", Company.data_status == "complete")) or 0
    cpc = history_cost_per_complete(s)
    return {"cfg": cfg, "camp": camp, "run": run, "runs": runs, "done_today": done_today or 0, "sectors": sectors,
            "regions": regions, "nexts": nexts,
            "incomplete_today": incomplete_today, "total_complete": total_complete,
            "request": pending_request(s), "cost_per_complete": cpc,
            "est_cost_target": round(cpc * cfg["daily_target"], 0) if cpc else None,
            "cost_today": round(float(s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0))
                                               .where(CostEntry.day == today)) or 0), 2)}


def region_table(s, camp, cfg, run) -> list[dict]:
    """«نتائج البحث حسب المنطقة» لليوم بتوقيت الرياض. المكتملة تُنسب لموقع أول احتساب، فمجموعها = إجمالي اليوم."""
    from . import geo
    from .timeutil import riyadh_day_bounds_utc
    today = riyadh_today()
    a, b = riyadh_day_bounds_utc(today)
    comp = dict(s.execute(select(Company.region_id, func.count(Company.id)).where(
        Company.campaign_id == camp.id, Company.counted_on == today, Company.data_status == "complete")
        .group_by(Company.region_id)).all())
    inc = dict(s.execute(select(Company.region_id, func.count(Company.id)).where(
        Company.campaign_id == camp.id, Company.data_status == "incomplete", Company.first_found_at >= a,
        Company.first_found_at < b).group_by(Company.region_id)).all())
    pr = (run.per_region or {}) if run else {}
    in_scope = set(geo.scope_regions(cfg["scope"]))
    rows = []
    for rid, r in geo.REGIONS.items():
        st = pr.get(rid, {})
        searched = st.get("searched", 0)
        c, i = comp.get(rid, 0), inc.get(rid, 0)
        if rid not in in_scope and not (searched or c or i):
            continue
        status = ("خارج النطاق" if rid not in in_scope and not searched else
                  "لم يبدأ البحث" if not searched else
                  "اكتمل البحث دون نتائج" if not (c or i) else "وُجدت نتائج")
        rows.append({"id": rid, "label": r["ar"], "status": status, "searched": searched, "complete": c,
                     "incomplete": i, "dup": st.get("dup", 0), "out": st.get("out", 0), "last": st.get("last", "")})
    other_c, other_i = comp.get(None, 0), inc.get(None, 0)
    if other_c or other_i:
        rows.append({"id": "-", "label": "غير محدد (سجلات قبل دليل المواقع)", "status": "—", "searched": 0,
                     "complete": other_c, "incomplete": other_i, "dup": 0, "out": 0, "last": ""})
    return rows


# ---------------------------------------------------------------- إيقاف البحث وسقف الإنفاق

CONTROL = "search_control"
DEFAULT_CAP_USD = 60.0


def get_control(s) -> dict:
    from .models import Setting
    row = s.get(Setting, CONTROL)
    return {"stopped": False, "daily_cap_usd": DEFAULT_CAP_USD, **(dict(row.value) if row else {})}


def set_control(s, **kw) -> dict:
    c = {**get_control(s), **kw}
    _put(s, CONTROL, c)
    return c


def halt_reason() -> str | None:
    """سبب منع النداءات المدفوعة الآن (يُقرأ من الخادم عند كل نداء)، أو None."""
    from .db import session_scope
    with session_scope() as s:
        c = get_control(s)
        if c.get("stopped"):
            return "أُوقف البحث من لوحة الإدارة"
        cap = float(c.get("daily_cap_usd") or 0)
        if cap > 0:
            spent = float(s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0))
                                   .where(CostEntry.day == riyadh_today())) or 0)
            if spent >= cap:
                return f"بلغ إنفاق اليوم سقفه ({spent:.2f} من {cap:.0f} دولارًا)"
    return None
