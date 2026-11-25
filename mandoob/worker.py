"""عامل الخلفية: يشغّل المهام المجدولة بتوقيت الرياض.

التشغيل: python -m mandoob.worker
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler

from . import jobs
from .config import RIYADH
from .db import init_db


def configure(scheduler) -> None:
    # الاكتشاف والتقرير اليومي: فحص كل دقيقتين لوقت التشغيل المحدد في الإعدادات (يمكن تغييره من الواجهة)
    scheduler.add_job(jobs.discovery_tick, "interval", minutes=2, id="discovery", max_instances=1, coalesce=True)
    # «ابحث الآن» من لوحة الإدارة: يلتقطه العامل خلال دقيقة
    scheduler.add_job(jobs.discovery_request_tick, "interval", minutes=1, id="discovery_request", max_instances=1,
                      coalesce=True)
    from . import directory
    scheduler.add_job(directory.tick, "interval", minutes=1, id="directory", max_instances=1, coalesce=True)
    scheduler.add_job(jobs.enrich_tick, "interval", minutes=10, id="enrich", max_instances=1, coalesce=True)
    from . import prospects
    scheduler.add_job(prospects.tick, "interval", minutes=1, id="prospects", max_instances=1, coalesce=True)
    from . import crm
    scheduler.add_job(crm.tick, "interval", minutes=1, id="crm_sync", max_instances=1, coalesce=True)
    from . import emergency
    scheduler.add_job(emergency.worker_heartbeat, "interval", minutes=1, id="heartbeat", max_instances=1, coalesce=True)
    from . import officials
    scheduler.add_job(officials.tick, "interval", minutes=5, id="officials", max_instances=1, coalesce=True)
    scheduler.add_job(jobs.daily_report_tick, "interval", minutes=5, id="daily_report", max_instances=1, coalesce=True)
    scheduler.add_job(jobs.weekly_report_tick, "interval", minutes=15, id="weekly_report", max_instances=1, coalesce=True)
    scheduler.add_job(jobs.outreach_tick, "interval", minutes=10, id="outreach", max_instances=1, coalesce=True)
    scheduler.add_job(jobs.inbox_tick, "interval", minutes=3, id="inbox", max_instances=1, coalesce=True)
    from . import team_report
    scheduler.add_job(team_report.tick, "interval", minutes=5, id="team_report", max_instances=1, coalesce=True)
    from . import customer_mail
    scheduler.add_job(customer_mail.tick, "interval", minutes=2, id="customer_mail", max_instances=1, coalesce=True)
    scheduler.add_job(jobs.reminders_tick, "interval", minutes=5, id="reminders", max_instances=1, coalesce=True)


def start_background() -> BackgroundScheduler:
    sch = BackgroundScheduler(timezone=RIYADH)
    configure(sch)
    sch.start()
    return sch


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    if jobs.recover_on_start():
        logging.getLogger(__name__).info("resumed an interrupted search request")
    sch = BlockingScheduler(timezone=RIYADH)
    configure(sch)
    logging.getLogger(__name__).info("worker started")
    sch.start()


if __name__ == "__main__":
    main()
