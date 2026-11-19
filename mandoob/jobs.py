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


def outreach_tick() -> dict:
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
