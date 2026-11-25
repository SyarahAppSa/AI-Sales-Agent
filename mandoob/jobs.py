"""المهام المجدولة على الخادم (تعمل حتى لو كان المتصفح مغلقًا)."""
from __future__ import annotations

import logging
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import session_scope
from .models import Campaign, JobLock, Notification, Setting, Task, User
from .timeutil import now_riyadh, now_utc, riyadh_today

log = logging.getLogger(__name__)
HOLDER = f"{socket.gethostname()}:{os.getpid()}"

DEFAULT_SCHEDULE = {"discovery_time": "06:00", "daily_report_time": "18:00"}


def get_setting(key: str, default: dict | None = None) -> dict:
    with session_scope() as s:
        row = s.get(Setting, key)
        return dict(row.value) if row else dict(default or {})


def put_setting(key: str, value: dict) -> None:
    with session_scope() as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))


@contextmanager
def job_lock(name: str, minutes: int = 120):
    """قفل في قاعدة البيانات يمنع تشغيل المهمة نفسها مرتين (حتى مع أكثر من عامل)."""
    acquired = False
    with session_scope() as s:
        row = s.get(JobLock, name)
        if row is None:
            s.add(JobLock(name=name, locked_until=now_utc() + timedelta(minutes=minutes), holder=HOLDER))
            try:
                s.flush()
                acquired = True
            except IntegrityError:
                s.rollback()
        elif row.locked_until < now_utc():
            row.locked_until, row.holder = now_utc() + timedelta(minutes=minutes), HOLDER
            acquired = True
    try:
        yield acquired
    finally:
        if acquired:
            with session_scope() as s:
                row = s.get(JobLock, name)
                if row and row.holder == HOLDER:
                    row.locked_until = now_utc() - timedelta(seconds=1)


def _due(time_str: str, last_key: str) -> bool:
    hh, mm = (int(x) for x in time_str.split(":"))
    now = now_riyadh()
    if (now.hour, now.minute) < (hh, mm):
        return False
    state = get_setting("job_state")
    return state.get(last_key) != riyadh_today().isoformat()


def _mark(last_key: str) -> None:
    state = get_setting("job_state")
    state[last_key] = riyadh_today().isoformat()
    put_setting("job_state", state)


def discovery_tick(force: bool = False) -> list[int]:
    from .ai import paid_enabled
    if not paid_enabled():  # وكيل «البحث عن العملاء» مدفوع: لا يبدأ والبحث المدفوع موقوف
        return []
    sched = get_setting("schedule", DEFAULT_SCHEDULE)
    if not force and not _due(sched.get("discovery_time", "06:00"), "discovery_done"):
        return []
    run_ids = []
    with job_lock("discovery", minutes=600) as ok:
        if not ok:
            return []
        from .discovery.runner import run_campaign_discovery
        with session_scope() as s:
            ids = [c.id for c in s.scalars(select(Campaign).where(Campaign.status == "active"))]
        for cid in ids:
            try:
                run_ids.append(run_campaign_discovery(cid))
            except Exception:
                log.exception("discovery failed for campaign %s", cid)
        _mark("discovery_done")
        outreach_tick()
    return run_ids


STALL_MINUTES = 4


def _stalled(req: dict) -> bool:
    """طلب «يبحث الآن» توقف نبضه (العامل الذي كان يشغّله أُعيد تشغيله أو أُوقف أثناء التحديث)."""
    from datetime import datetime
    beat = req.get("heartbeat") or req.get("started_at")
    if req.get("state") != "running" or not beat:
        return False
    return now_utc() - datetime.fromisoformat(beat) > timedelta(minutes=STALL_MINUTES)


def recover_on_start() -> bool:
    """عند تشغيل العامل: لا يغيّر شيئًا بنفسه (قد يكون العامل السابق ما زال يعمل لحظة التبديل)،
    وإنما يعتمد على النبض: الطلب المتوقف أكثر من ٤ دقائق يُستأنف تلقائيًا في discovery_request_tick."""
    from .prospecting import REQUEST
    return _stalled(get_setting(REQUEST))


def discovery_request_tick() -> bool:
    """يلتقط طلب «ابحث الآن» من لوحة الإدارة ويشغّله على العامل (يكمل ولو أُغلقت اللوحة).
    أثناء التشغيل يُحدَّث «نبض» كل دقيقة؛ فإن انقطع (إعادة تشغيل العامل) يُستأنف الطلب من نقطة توقفه دون تكرار."""
    import threading
    from .prospecting import REQUEST
    req = get_setting(REQUEST)
    if _stalled(req):
        log.warning("search request stalled — resuming from its checkpoint")
        with session_scope() as s:
            lock = s.get(JobLock, "discovery")
            if lock is not None:
                lock.locked_until = now_utc() - timedelta(seconds=1)
        req.update(state="pending", resumed_after_stall=now_utc().isoformat(timespec="seconds"))
        put_setting(REQUEST, req)
    if req.get("state") != "pending":
        return False
    with job_lock("discovery", minutes=600) as ok:
        if not ok:
            return False  # تشغيل آخر قائم؛ يُلتقط الطلب بعد انتهائه
        req.update(state="running", started_at=now_utc().isoformat(timespec="seconds"),
                   heartbeat=now_utc().isoformat(timespec="seconds"))
        put_setting(REQUEST, req)
        stop = threading.Event()

        def beat():
            while not stop.wait(60):
                try:
                    cur = get_setting(REQUEST)
                    if cur.get("state") == "running":
                        cur["heartbeat"] = now_utc().isoformat(timespec="seconds")
                        put_setting(REQUEST, cur)
                except Exception:  # noqa: BLE001
                    log.exception("heartbeat failed")
        t = threading.Thread(target=beat, daemon=True)
        t.start()
        from .discovery.runner import run_campaign_discovery
        try:
            req["run_id"] = run_campaign_discovery(int(req["campaign_id"]), max_new=req.get("max_new"), force=True)
            req["state"] = "done"
        except Exception as e:  # noqa: BLE001
            log.exception("requested discovery failed")
            req.update(state="failed", error=str(e)[:300])
        finally:
            stop.set()
        req["finished_at"] = now_utc().isoformat(timespec="seconds")
        req.pop("heartbeat", None)
        put_setting(REQUEST, req)
    return True


def outreach_tick() -> dict:
    from .outreach import outreach_disabled
    if outreach_disabled():  # المرحلة الحالية: لا إرسال تلقائي ولا متابعات آلية
        return {}
    with job_lock("outreach", minutes=30) as ok:
        if not ok:
            return {}
        from .outreach import queue_followups, queue_offers, send_due
        with session_scope() as s:
            camps = [(c.id, c.daily_send_cap) for c in s.scalars(select(Campaign).where(
                Campaign.status == "active", Campaign.send_enabled.is_(True)))]
        for cid, cap in camps:
            queue_offers(cid, limit=cap)
        queue_followups()
        return send_due()


def inbox_tick() -> dict:
    with job_lock("inbox", minutes=15) as ok:
        if not ok:
            return {}
        from .inbox import sync_all
        return sync_all()


def reminders_tick() -> int:
    """تنبيه المندوب عند حلول موعد مهمة."""
    n = 0
    with session_scope() as s:
        state = get_setting("reminded_tasks", {"ids": []})
        done = set(state.get("ids", []))
        for t in s.scalars(select(Task).where(Task.status == "open", Task.due_at <= now_utc() + timedelta(minutes=15))):
            if t.id in done or not t.owner_id:
                continue
            s.add(Notification(user_id=t.owner_id, company_id=t.company_id, title=f"حان موعد: {t.title}",
                               body=t.details[:800]))
            done.add(t.id)
            n += 1
    put_setting("reminded_tasks", {"ids": sorted(done)[-5000:]})
    return n


def daily_report_tick(force: bool = False) -> bool:
    sched = get_setting("schedule", DEFAULT_SCHEDULE)
    if not force and not _due(sched.get("daily_report_time", "18:00"), "daily_report_done"):
        return False
    from .inbox import notify
    from .reports import daily_report
    with session_scope() as s:
        rep = daily_report(s)
        lines = [f"تقرير يوم {rep['day']}", f"شركات مكتملة جديدة: {rep['complete']}",
                 f"التكلفة التقديرية: ${rep['total_cost']}" +
                 (f" — تكلفة العميل المكتمل: ${rep['cost_per_lead']}" if rep["cost_per_lead"] else ""),
                 "", "القطاعات:"]
        for k, v in rep["by_sector"].items():
            lines.append(f"- {k}: تواصل {v['contacted']}، ردود {v['replied']}، اجتماعات {v['meetings']}، صفقات {v['won']}")
        lines.append(f"\nمهام الغد والمتأخرة: {len(rep['actions'])}")
        for u in s.scalars(select(User).where(User.role.in_(["admin", "manager"]), User.active.is_(True))):
            notify(s, u, None, f"التقرير اليومي — {rep['day']}", "\n".join(lines))
    _mark("daily_report_done")
    return True


def weekly_report_tick(force: bool = False) -> bool:
    """نهاية الأسبوع (الخميس بعد وقت التقرير اليومي): ملخص الشرائح لمدير المبيعات."""
    sched = get_setting("schedule", DEFAULT_SCHEDULE)
    if not force:
        if now_riyadh().weekday() != 3 or not _due(sched.get("daily_report_time", "18:00"), "weekly_report_done"):
            return False
    from .inbox import notify
    from .reports import weekly_segments
    with session_scope() as s:
        w = weekly_segments(s)
        lines = [f"أسبوع {w['start']} — {w['end']}",
                 f"تواصلنا مع {w['totals']['contacted']} شركة — {w['totals']['meetings_per_100']} اجتماع لكل 100 شركة", "",
                 "الشرائح (اجتماعات / 100 — مهتمة / 100 — صفقات):"]
        for _, r in w["rows"]:
            tag = " [اختبار]" if r["experiment"] else ""
            small = " (عينة صغيرة)" if r["small_sample"] else ""
            lines.append(f"- {r['label']}{tag}: {r['meetings_per_100']} — {r['interested_per_100']} — {r['won']} "
                         f"(من {r['contacted']} شركة){small}")
        if w["leads"]:
            lines.append("\nطلبات «اطلب عرضًا لأسطولك»: " + "، ".join(f"{k}: {v}" for k, v in w["leads"].items()))
        lines.append(f"\nالحصص الحالية: {w['split']['core']} للقطاعات الحالية و{w['split']['experiment']} للاختبار.")
        for u in s.scalars(select(User).where(User.role.in_(["admin", "manager"]), User.active.is_(True))):
            notify(s, u, None, f"التقرير الأسبوعي للشرائح — {w['start']}", "\n".join(lines))
    _mark("weekly_report_done")
    return True


ENRICH_KEY = "enrich_auto"  # {"budget_usd": 3}: سقف يومي لإعادة البحث عن الهاتف/البريد الناقص


def enrich_spent_today(s) -> float:
    from sqlalchemy import func, or_, select
    from .models import CostEntry
    from .timeutil import riyadh_today
    return float(s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(
        CostEntry.day == riyadh_today(), or_(CostEntry.note == "complete", CostEntry.note.like("%:retry")))) or 0)


def enrich_tick(limit: int = 5) -> int:
    """كل ١٠ دقائق: يعيد البحث عن الهاتف والبريد للعملاء «قيد الاستكمال» الذين حان موعدهم (أيًّا كان مصدرهم:
    دفعات «البحث عن عملاء» اليدوية أو الاكتشاف أو التشغيل اليومي) — الموقع أولًا ثم وكيل البحث، ضمن سقف يومي."""
    from .ai import paid_enabled
    from .config import settings
    if not settings.ai_enabled or not paid_enabled():  # لا تُستهلك محاولات الاستكمال والبحث المدفوع موقوف
        return 0
    budget = float((get_setting(ENRICH_KEY) or {}).get("budget_usd", 3))
    with session_scope() as s:
        if enrich_spent_today(s) >= budget:
            return 0
    with job_lock("enrich", minutes=30) as ok:
        if not ok:
            return 0
        from .discovery.runner import enrich_incomplete
        return enrich_incomplete(limit=limit)
