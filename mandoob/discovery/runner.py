"""التشغيل اليومي للاكتشاف: يحقق حصة كل فئة من عدة مصادر، ويحفظ نقطة التوقف بعد كل مرشح،
ويتجاوز المصادر المتعطلة، ويسجل العجز وسببه بصدق."""
from __future__ import annotations

import logging
from datetime import date
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..companies import (SOURCE_LABELS, assess_fit, find_existing, is_complete, log_activity,
                         upsert_candidate, _add_contact, _refresh_preferred)
from ..db import session_scope
from ..models import Campaign, Category, Company, DiscoveryRun
from ..normalize import domain_of
from ..timeutil import now_utc, riyadh_day_bounds_utc, riyadh_today
from .base import Candidate, SourceError, SourceUnavailable
from .crawler import Crawler, SiteResult
from .agent import AgentSource
from .sources import AISignalsSource, AIWebSearchSource, BraveSearchSource, GooglePlacesSource, find_official_website

log = logging.getLogger(__name__)

SOURCE_ORDER = ["ai_agent", "google_places", "web_search", "ai_search", "signals"]


def known_names() -> list[str]:
    """أسماء ونطاقات وجدناها مؤخرًا — تُعطى للوكيل ليتجنبها فيأتي بنتائج جديدة."""
    with session_scope() as s:
        rows = s.execute(select(Company.name, Company.domain).order_by(Company.id.desc()).limit(200))
        return [d or n for n, d in rows]


def default_sources() -> dict:
    def known_domains():
        with session_scope() as s:
            return [d for (d,) in s.execute(select(Company.domain).where(Company.domain.isnot(None))
                                            .order_by(Company.id.desc()).limit(300))]
    return {"google_places": GooglePlacesSource(), "web_search": BraveSearchSource(),
            "ai_search": AIWebSearchSource(known_domains), "signals": AISignalsSource(),
            "ai_agent": AgentSource(known_names)}


def _count_today(s: Session, category_id: int, day: date) -> int:
    return s.scalar(select(func.count(Company.id)).where(Company.category_id == category_id,
                                                         Company.counted_on == day,
                                                         Company.data_status == "complete")) or 0


def _log(run: DiscoveryRun, msg: str) -> None:
    run.log = (run.log or []) + [f"{now_utc():%H:%M:%S} {msg}"]
    if len(run.log) > 400:
        run.log = run.log[-400:]


def run_campaign_discovery(campaign_id: int, *, day: date | None = None, sources: dict | None = None,
                           crawler: Crawler | None = None, max_candidates_factor: int = 6,
                           on_progress: Callable[[str], None] | None = None, max_new: int | None = None,
                           force: bool = False) -> int:
    """max_new: حد الدفعة (مثل 20 عميلًا جديدًا مكتملًا) — للتجربة أو التشغيل على دفعات."""
    """يشغّل (أو يستأنف) اكتشاف اليوم لحملة واحدة ويعيد رقم التشغيل."""
    day = day or riyadh_today()
    sources = sources if sources is not None else default_sources()
    crawler = crawler or Crawler()

    with session_scope() as s:
        camp = s.get(Campaign, campaign_id)
        if camp is None or (camp.status != "active" and not force):
            raise ValueError("الحملة غير نشطة")
        run = s.scalar(select(DiscoveryRun).where(DiscoveryRun.campaign_id == campaign_id,
                                                  DiscoveryRun.run_date == day))
        if run is None:
            run = DiscoveryRun(run_date=day, campaign_id=campaign_id, target=camp.daily_target,
                               checkpoint={}, per_category={}, per_source={}, source_errors={}, log=[])
            s.add(run)
            s.flush()
            _log(run, "بدء التشغيل اليومي")
        else:
            run.status = "running"
            _log(run, "استئناف من نقطة التوقف")
        run_id = run.id
        # النطاق الجغرافي (أداة «البحث عن العملاء»): المواقع من دليل الأماكن بالتناوب بين المناطق
        from ..geo import PLACES, REGIONS, normalize_scope, scope_places
        scope = normalize_scope(camp.geo_scope) if camp.geo_scope else None
        scope_ids = scope_places(scope) if scope else []
        loc_label = {pid: f"{PLACES[pid]['ar']}، {REGIONS[PLACES[pid]['region']]['ar']}" for pid in scope_ids}
        label_pid = {v: k for k, v in loc_label.items()}
        cats = [(c.id, c.label, c.quota, [loc_label[p] for p in scope_ids] if scope else list(c.cities),
                 list(c.keywords_ar) + list(c.keywords_en))
                for c in camp.categories if c.active and c.quota > 0]
        allow_backfill = camp.allow_backfill
        total_target = sum(q for _, _, q, _, _ in cats)

    disabled: dict[str, str] = {}
    exhausted: dict[int, bool] = {}
    halted: dict[str, str] = {}
    if scope and max_new and hasattr(sources.get("ai_agent"), "per_call"):
        # دفعة صغيرة على نطاق جغرافي: نداءات أصغر تمتد على عدة مواقع بدل موقع واحد
        sources["ai_agent"].per_call = min(sources["ai_agent"].per_call, max(2, -(-max_new // 3)))

    def _region_stat(region: str, key: str, n: int = 1, last: str | None = None) -> None:
        with session_scope() as s:
            run = s.get(DiscoveryRun, run_id)
            pr = dict(run.per_region or {})
            e = dict(pr.get(region, {}))
            e[key] = e.get(key, 0) + n
            if last:
                e["last"] = last
            pr[region] = e
            run.per_region = pr
    with session_scope() as s:
        baseline = sum(_count_today(s, cid, day) for cid, *_ in cats)

    # دفعة محدودة على عدة قطاعات: لكل قطاع نصيبه، فلا يستهلكها القطاع الأول وحده
    per_cat_cap = -(-max_new // len(cats)) if max_new and len(cats) > 1 else None
    if per_cat_cap is not None and hasattr(sources.get("ai_agent"), "per_call"):
        # نداء الوكيل يُحفظ كاملًا؛ نصغّره على قدر نصيب القطاع حتى لا تتجاوز الدفعة حدها كثيرًا
        sources["ai_agent"].per_call = min(sources["ai_agent"].per_call, max(per_cat_cap + 1, 3))

    def batch_full(s) -> bool:
        if per_cat_cap is not None:
            return False  # نصيب كل قطاع هو الحد
        return max_new is not None and sum(_count_today(s, cid, day) for cid, *_ in cats) - baseline >= max_new

    with session_scope() as s:
        cat_base = {cid: _count_today(s, cid, day) for cid, *_ in cats}

    def cat_full(s, cat_id: int, quota: int) -> bool:
        have = _count_today(s, cat_id, day)
        return have >= quota or (per_cat_cap is not None and have - cat_base[cat_id] >= per_cat_cap)

    def process_category(cat_id: int, quota: int, cities: list[str], queries: list[str]) -> None:
        budget = quota * max_candidates_factor
        seen_candidates = 0
        if scope:
            # (موقع × عبارة): عبارة واحدة على كل مواقع النطاق ثم التالية، ويبدأ من حيث توقفت الدفعة السابقة
            pairs = [(city, qi, q) for qi, q in enumerate(queries) for city in cities]
            with session_scope() as s:
                start = (s.get(Category, cat_id).cursor or 0) % max(len(pairs), 1)
            pairs = pairs[start:] + pairs[:start]
        else:
            pairs = [(city, qi, q) for city in cities for qi, q in enumerate(queries)]
        for src_name in SOURCE_ORDER:
            for pi, (city, qi, query) in enumerate(pairs):
                if True:
                    from ..prospecting import halt_reason
                    why = halt_reason()
                    if why:
                        halted["why"] = why
                        return
                    with session_scope() as s:
                        if cat_full(s, cat_id, quota) or batch_full(s):
                            return
                    if src_name in disabled or src_name not in sources:
                        break
                    import hashlib
                    key = f"{cat_id}|{src_name}|{city}|{qi}|{hashlib.md5(query.encode()).hexdigest()[:6]}"
                    with session_scope() as s:
                        run = s.get(DiscoveryRun, run_id)
                        state = dict((run.checkpoint or {}).get(key, {}))
                    if state.get("done"):
                        continue
                    try:
                        # نتائج نداء الوكيل الواحد دُفع ثمنها مسبقًا: تُحفظ كلها حتى لو اكتملت الدفعة في منتصفها
                        whole_call = src_name == "ai_agent"
                        stop = False
                        searched_pid = label_pid.get(city)
                        for cand in sources[src_name].search(query, city, state):
                            seen_candidates += 1
                            _handle_candidate(run_id, cat_id, cand, crawler, day,
                                              scope_ids=set(scope_ids) if scope else None, searched_pid=searched_pid)
                            with session_scope() as s:
                                run = s.get(DiscoveryRun, run_id)
                                cp = dict(run.checkpoint or {})
                                cp[key] = dict(state)
                                run.checkpoint = cp
                                if cat_full(s, cat_id, quota) or batch_full(s):
                                    stop = True
                            if stop and not whole_call:
                                return
                            if seen_candidates >= budget and not whole_call:
                                return
                        state["done"] = True
                        if scope and searched_pid:
                            _region_stat(PLACES[searched_pid]["region"], "searched", 1, last=f"{city} — «{query}»")
                            with session_scope() as s:  # الموضع التالي يُحفظ للدفعة القادمة (ولو في يوم آخر)
                                cat_row = s.get(Category, cat_id)
                                cat_row.cursor = (start + pi + 1) % max(len(pairs), 1)
                        if stop or seen_candidates >= budget:
                            return
                    except SourceUnavailable as e:
                        disabled[src_name] = str(e)
                    except SourceError as e:
                        disabled[src_name] = str(e)
                        log.warning("source %s failed: %s", src_name, e)
                    finally:
                        with session_scope() as s:
                            run = s.get(DiscoveryRun, run_id)
                            cp = dict(run.checkpoint or {})
                            cp[key] = dict(state)
                            run.checkpoint = cp
                            if src_name in disabled:
                                errs = dict(run.source_errors or {})
                                if src_name not in errs:
                                    errs[src_name] = disabled[src_name]
                                    _log(run, f"تعطل المصدر {SOURCE_LABELS.get(src_name, src_name)}: "
                                              f"{disabled[src_name]} — نواصل من المصادر الأخرى")
                                run.source_errors = errs
        exhausted[cat_id] = True

    for cat_id, label, quota, cities, queries in cats:
        if halted:
            break
        with session_scope() as s:
            if batch_full(s):
                break
        if on_progress:
            on_progress(f"فئة {label}")
        process_category(cat_id, quota, cities, queries)

    # تعويض النقص من فئة أخرى (إن سُمح بذلك)
    if allow_backfill and max_new is None and not halted:
        with session_scope() as s:
            done_total = sum(_count_today(s, cid, day) for cid, *_ in cats)
        gap = total_target - done_total
        if gap > 0:
            for cat_id, label, quota, cities, queries in cats:
                if exhausted.get(cat_id):
                    continue
                with session_scope() as s:
                    have = _count_today(s, cat_id, day)
                process_category(cat_id, have + gap, cities, queries)
                with session_scope() as s:
                    gap = total_target - sum(_count_today(s, cid, day) for cid, *_ in cats)
                if gap <= 0:
                    break

    # إعادة محاولة استكمال السجلات الناقصة — في التشغيل اليومي فقط، لا في الدفعات اليدوية (أسرع وأوضح تكلفة)
    if max_new is None and not halted:
        enrich_incomplete(crawler=crawler, limit=int(__import__("os").environ.get("ENRICH_LIMIT", "15")))

    with session_scope() as s:
        run = s.get(DiscoveryRun, run_id)
        per_cat = {}
        reasons = []
        total = 0
        for cat_id, label, quota, *_ in cats:
            got = _count_today(s, cat_id, day)
            total += got
            per_cat[str(cat_id)] = {"label": label, "quota": quota, "complete": got}
            if got < quota:
                if halted:
                    why = halted["why"]
                elif max_new is not None and not exhausted.get(cat_id):
                    why = f"تشغيل على دفعات: توقف البحث بعد دفعة من {max_new} عملاء جدد كما طُلب"
                elif exhausted.get(cat_id):
                    why = "استُنفدت عبارات البحث والمصادر المتاحة"
                else:
                    why = "بلغ الحد الأقصى للمرشحين المسموح بفحصهم"
                reasons.append(f"{label}: {got} من {quota} ({why})")
        d0, d1 = riyadh_day_bounds_utc(day)
        incomplete_today = s.scalar(select(func.count(Company.id)).where(
            Company.data_status == "incomplete", Company.first_found_at >= d0, Company.first_found_at < d1)) or 0
        run.per_category = per_cat
        run.complete_count = total
        run.shortfall = max(total_target - total, 0)
        for name, err in disabled.items():
            reasons.append(f"المصدر «{SOURCE_LABELS.get(name, name)}» غير متاح: {err}")
        if incomplete_today:
            reasons.append(f"{incomplete_today} شركة مطابقة ينقصها هاتف أو بريد موثّق (قيد الاستكمال)")
        run.shortfall_reason = "\n".join(reasons) if run.shortfall else ""
        run.status = "done" if run.shortfall == 0 else ("stopped" if halted else "partial")
        if halted:
            _log(run, f"توقف البحث: {halted['why']} — لا نداءات مدفوعة جديدة؛ ما كان قيد التنفيذ اكتمل وحُفظ")
        run.finished_at = now_utc()
        _log(run, f"انتهى التشغيل: {total} مكتملة من {total_target}")
        return run_id


def _bump(run: DiscoveryRun, field: str, src: str) -> None:
    setattr(run, field, (getattr(run, field) or 0) + 1)
    ps = dict(run.per_source or {})
    entry = dict(ps.get(src, {}))
    entry[field] = entry.get(field, 0) + 1
    ps[src] = entry
    run.per_source = ps


def _handle_candidate(run_id: int, cat_id: int, cand: Candidate, crawler: Crawler, day: date | None = None,
                      scope_ids: set | None = None, searched_pid: str | None = None) -> str:
    from ..geo import PLACES, resolve_city
    if scope_ids is not None:
        # الموقع يجب أن يثبت بعنوان منشور داخل النطاق (لا عبارة «نوصّل لكل المناطق»)
        pid = resolve_city(cand.address or "") or resolve_city(cand.city or "")
        why = None
        if pid is None:
            why = "الموقع غير سعودي أو غير معروف في دليل الأماكن"
        elif not getattr(cand, "location_verified", True):
            why = "لم يثبت عنوان منشور للمنشأة في هذا الموقع"
        elif pid not in scope_ids:
            why = f"خارج النطاق المختار ({PLACES[pid]['ar']})"
        if why:
            with session_scope() as s:
                run = s.get(DiscoveryRun, run_id)
                _bump(run, "rejected_count", cand.source_type)
                pr = dict(run.per_region or {})
                reg = PLACES[searched_pid]["region"] if searched_pid else "-"
                e = dict(pr.get(reg, {}))
                e["out"] = e.get("out", 0) + 1
                pr[reg] = e
                run.per_region = pr
                _log(run, f"استُبعد «{cand.name}»: {why}")
            return "rejected"
        cand.place_id = pid
        cand.city = PLACES[pid]["ar"]
    with session_scope() as s:
        run = s.get(DiscoveryRun, run_id)
        cat = s.get(Category, cat_id)
        camp = cat.campaign
        # فحص سريع للتكرار قبل الزحف لتوفير الوقت والتكلفة
        pre = find_existing(s, cand, cand.contacts)
        site: SiteResult | None = None
        kinds = {fc.kind for fc in cand.contacts}
        if pre is None and cand.website and not (cand.prequalified and kinds >= {"phone", "email"}):
            try:
                site = crawler.crawl(cand.website)
            except Exception as e:  # الزاحف لا يوقف التشغيل
                site = SiteResult(website=cand.website, error=str(e)[:200])
        if pre is not None:
            fit = None
        elif cand.prequalified:
            fit = _prequalified_fit(cand, site, cat, camp.exclusions or [])
        else:
            fit = assess_fit(cand, site, cat, camp.exclusions or [], campaign_id=camp.id)
        if fit is None:
            from ..companies import FitResult
            fit = FitResult(True, pre.fit_reason, pre.fit_evidence_url, pre.fit_score, pre.activity)
        if cand.signals:
            fit.signals = list(fit.signals or []) + list(cand.signals)
        res = upsert_candidate(s, cand, site, cat, fit, run_id=run_id, day=day)
        pid = getattr(cand, "place_id", None)
        if res.company is not None and pid:
            if res.outcome in ("new_complete", "new_incomplete") and not res.company.place_id:
                res.company.place_id, res.company.region_id = pid, PLACES[pid]["region"]
            elif res.company.place_id and res.company.place_id != pid:
                # فرع موثّق في موقع آخر: يُحفظ للفريق ولا تُحتسب المنشأة مرة ثانية
                br = list(res.company.branches or [])
                if pid not in {b.get("place_id") for b in br}:
                    br.append({"place_id": pid, "city": PLACES[pid]["ar"], "address": cand.address or "",
                               "url": cand.source_url})
                    res.company.branches = br
        if res.outcome in ("duplicate", "branch") and pid:
            pr = dict(run.per_region or {})
            e = dict(pr.get(PLACES[pid]["region"], {}))
            e["dup"] = e.get("dup", 0) + 1
            pr[PLACES[pid]["region"]] = e
            run.per_region = pr
        src = cand.source_type
        ps = dict(run.per_source or {})
        e = dict(ps.get(src, {}))
        e["candidates"] = e.get("candidates", 0) + 1
        ps[src] = e
        run.per_source = ps
        if res.outcome in ("new_complete", "completed_now"):
            _bump(run, "complete_count", src)
        elif res.outcome == "new_incomplete":
            _bump(run, "incomplete_count", src)
        elif res.outcome in ("duplicate", "branch"):
            _bump(run, "duplicate_count", src)
        else:
            _bump(run, "rejected_count", src)
        if res.company is not None and res.outcome in ("new_complete", "completed_now"):
            from ..assignment import assign_owner
            assign_owner(s, res.company)
        return res.outcome


def _prequalified_fit(cand: Candidate, site, cat: Category, exclusions: list[str]):
    """مرشح من «البحث الآلي»: الوكيل قرأ الصفحة ووثّق دليل استخدام المركبات؛ لا حاجة لنداء تأهيل ثانٍ."""
    from ..companies import BROKER_WORDS, FitResult
    from ..normalize import normalize_arabic
    from ..segments import detect_operator, detect_signals
    from ..targeting import SECTORS
    text = normalize_arabic(f"{cand.name} {cand.activity} {cand.fit_hint}").lower()
    if exclusions and any(normalize_arabic(w) in text for w in BROKER_WORDS):
        return FitResult(False, "", None, 0, cand.activity, "نشاط وساطة (مستبعد)")
    pages = site.pages if site else []
    op = detect_operator(pages) if pages else {"status": cand.operator_status or "unknown", "carrier": None,
                                               "evidence": "", "url": None}
    if op["status"] == "unknown" and cand.operator_status in ("self", "contracted"):
        op = {"status": cand.operator_status, "carrier": None, "evidence": cand.phrase or "", "url": cand.evidence_url}
    score = 0.75 if cand.phrase else (0.4 if getattr(cand, "vehicle_indicator", None) == "unconfirmed" else 0.6)
    return FitResult(True, cand.fit_hint, cand.evidence_url or cand.source_url, score,
                     cand.activity or cat.label, phrase=cand.phrase, operator=op,
                     signals=detect_signals(pages) if pages else [],
                     decision_unit=SECTORS.get(cat.sector_key, {}).get("decision_unit"))


def enrich_incomplete(*, crawler: Crawler | None = None, limit: int = 100) -> int:
    """يعود للبحث عن البيان الناقص للشركات «قيد استكمال البيانات»."""
    crawler = crawler or Crawler(max_pages=10)
    completed = 0
    # شركات عُرف اسمها فقط (مثل الناقلين المذكورين في صفحات العملاء): نبحث أولًا عن موقعها الرسمي
    with session_scope() as s:
        nosite = [(c.id, c.name, c.city) for c in s.scalars(
            select(Company).where(Company.data_status == "incomplete", Company.website.is_(None),
                                  Company.is_test.is_(False), Company.enrich_attempts < 2).limit(min(limit, 10)))]
    for cid, name, city in nosite:
        site_url = find_official_website(name, city)
        with session_scope() as s:
            c = s.get(Company, cid)
            c.enrich_attempts += 1
            if site_url and not s.scalar(select(Company.id).where(Company.domain == domain_of(site_url))):
                c.website, c.domain = site_url, domain_of(site_url)
                c.retry_enrich_at = None
                log_activity(s, cid, "data", "عُثر على الموقع الرسمي", site_url)
    with session_scope() as s:
        ids = [cid for (cid,) in s.execute(
            select(Company.id).where(Company.data_status == "incomplete", Company.website.isnot(None),
                                     Company.is_test.is_(False),
                                     Company.enrich_attempts < 5,
                                     (Company.retry_enrich_at.is_(None)) | (Company.retry_enrich_at <= now_utc()))
            .limit(limit))]
    for cid in ids:
        with session_scope() as s:
            c = s.get(Company, cid)
            c.enrich_attempts += 1
            try:
                site = crawler.crawl(c.website)
            except Exception:
                site = SiteResult(website=c.website)
            added = sum(_add_contact(s, c, fc) for fc in site.contacts)
            _refresh_preferred(c)
            if not is_complete(c) and c.enrich_attempts <= 3:
                # البحث في مصادر أخرى عن البيان الناقص (يتحقق الوكيل من ظهوره نصًا في الصفحة)
                from ..companies import missing_fields
                from .agent import agent_complete
                for fc in agent_complete(c.name, c.city, c.website, missing_fields(c)):
                    added += _add_contact(s, c, fc)
                _refresh_preferred(c)
            if is_complete(c):
                c.data_status = "complete"
                c.completed_at = now_utc()
                if c.parent_id is None and c.lead_source in (None, "discovery") or (c.lead_source or "").startswith("carrier_of"):
                    c.counted_on = riyadh_today()
                log_activity(s, c.id, "data", "اكتملت بيانات الشركة", "عُثر على البيان الناقص في موقعها")
                from ..assignment import assign_owner
                assign_owner(s, c)
                completed += 1
            else:
                from datetime import timedelta
                c.retry_enrich_at = now_utc() + timedelta(days=2)
                if added:
                    log_activity(s, c.id, "data", "تحديث بيانات الشركة", f"أضيف {added} بيان")
    return completed
