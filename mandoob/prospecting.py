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
    return cfg


def _put(s, key: str, value: dict) -> None:
    from .models import Setting
    row = s.get(Setting, key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))


def resolve_cities(cities: list[str], regions: list[str]) -> list[str]:
    out = list(cities)
    for r in regions:
        out += REGIONS.get(r, [])
    return list(dict.fromkeys(c.strip() for c in out if c and c.strip()))


def split_quota(total: int, n: int) -> list[int]:
    base, extra = divmod(max(total, 0), max(n, 1))
    return [base + (1 if i < extra else 0) for i in range(n)]


def save_config(s, *, cities: list[str], regions: list[str], activities: list[str], daily_target: int,
                scheduled: bool, time: str, user_id: int | None = None, all_activities: bool = False) -> Campaign:
    """يحفظ الإعداد ويحدّث حملة البحث: فئة لكل قطاع (بعبارات أنشطته المختارة)، والهدف اليومي موزع بالتساوي على القطاعات."""
    cfg = get_config(s)
    all_activities = all_activities or "__all__" in (activities or [])
    activities = list(ACTIVITIES) if all_activities else (normalize_selection(activities) or DEFAULT_ACTIVITIES)
    groups = by_sector(activities)
    all_cities = resolve_cities(cities, regions) or DEFAULT["cities"]
    daily_target = max(1, min(int(daily_target or 400), 2000))
    camp = s.get(Campaign, cfg["campaign_id"]) if cfg.get("campaign_id") else None
    if camp is None:
        camp = Campaign(name="البحث عن العملاء", request_text="", status="paused", send_enabled=False,
                        created_by=user_id, approved_at=now_utc())
        s.add(camp)
        s.flush()
    what = "جميع الأنشطة في السعودية" if all_activities else "، ".join(SECTORS[k]["label"] for k in groups)
    camp.request_text = f"منشآت داخل السعودية — {what} — في {'، '.join(all_cities[:12])}"
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
    new_cfg = {"cities": cities, "regions": regions, "activities": activities, "all": bool(all_activities),
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
    total_complete = s.scalar(select(func.count(Company.id)).where(
        Company.is_test.is_(False), Company.lead_source == "discovery", Company.data_status == "complete")) or 0
    cpc = history_cost_per_complete(s)
    return {"cfg": cfg, "camp": camp, "run": run, "runs": runs, "done_today": done_today or 0, "sectors": sectors,
            "incomplete_today": incomplete_today, "total_complete": total_complete,
            "request": pending_request(s), "cost_per_complete": cpc,
            "est_cost_target": round(cpc * cfg["daily_target"], 0) if cpc else None,
            "cost_today": round(float(s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0))
                                               .where(CostEntry.day == today)) or 0), 2)}
