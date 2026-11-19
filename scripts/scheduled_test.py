"""اختبار التشغيل المجدول دون تدخل: حملة صغيرة مستقلة (الإرسال معطّل) يشغّلها العامل على الخادم وحده في موعد بتوقيت الرياض.

  setup <دقائق> <مدينة> <العدد>  ← ينشئ الحملة، ويضبط موعد البحث بعد N دقيقة، ويحفظ الموعد السابق
  report <رقم الحملة>            ← وقت البدء والانتهاء والنتيجة والتكلفة وصفر تواصل
  teardown <رقم الحملة>          ← يوقف الحملة ويعيد الموعد السابق
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import func, select  # noqa: E402

from mandoob.db import init_db, session_scope  # noqa: E402
from mandoob.jobs import DEFAULT_SCHEDULE, get_setting, put_setting  # noqa: E402
from mandoob.models import Call, Campaign, Category, Company, CostEntry, DiscoveryRun, OutboundMessage  # noqa: E402
from mandoob.timeutil import now_riyadh, to_riyadh  # noqa: E402

init_db()
cmd = sys.argv[1]
if cmd == "setup":
    mins, city, n = int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    from mandoob.discovery.agent import plan_queries
    text = f"ابحث لي يوميًا في {city} عن المنشآت وأصحاب الأنشطة الذين يستخدمون سيارات في عملهم"
    with session_scope() as s:
        active = [c.name for c in s.scalars(select(Campaign).where(Campaign.status == "active"))]
        if active:
            sys.exit(f"توجد حملات نشطة {active} — أوقفها أولًا حتى لا يشغّلها الاختبار")
        camp = Campaign(name=f"اختبار التشغيل المجدول — {city}", request_text=text, status="active", daily_target=n,
                        send_enabled=False)
        s.add(camp)
        s.flush()
        s.add(Category(campaign_id=camp.id, sector_key="vehicle_users", label="منشآت تستخدم مركبات", cities=[city],
                       keywords_ar=plan_queries(text, [city]), keywords_en=[], quota=n))
        cid = camp.id
    sched = get_setting("schedule", DEFAULT_SCHEDULE)
    if "backup" not in get_setting("schedule_backup", {}):
        put_setting("schedule_backup", {"backup": sched})
    at = now_riyadh() + timedelta(minutes=mins)
    put_setting("schedule", {**sched, "discovery_time": at.strftime("%H:%M")})
    state = get_setting("job_state")
    state.pop("discovery_done", None)
    put_setting("job_state", state)
    print(f"SCHED campaign {cid} scheduled at {at:%H:%M} Riyadh (now {now_riyadh():%H:%M})", flush=True)
elif cmd == "report":
    cid = int(sys.argv[2])
    with session_scope() as s:
        run = s.scalar(select(DiscoveryRun).where(DiscoveryRun.campaign_id == cid).order_by(DiscoveryRun.id.desc()))
        if not run:
            print("SCHED no run yet; now", f"{now_riyadh():%H:%M:%S}", "schedule", get_setting("schedule"))
            sys.exit(0)
        cost = s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(CostEntry.at >= run.started_at))
        ids = select(Company.id).where(Company.campaign_id == cid)
        print("SCHED status", run.status, "start", f"{to_riyadh(run.started_at):%Y-%m-%d %H:%M:%S}",
              "end", f"{to_riyadh(run.finished_at):%H:%M:%S}" if run.finished_at else "-",
              "complete", run.complete_count, "incomplete", run.incomplete_count, "dup", run.duplicate_count,
              "cost_usd", round(float(cost or 0), 3), flush=True)
        print("SCHED zero_contact messages", s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.company_id.in_(ids))),
              "calls", s.scalar(select(func.count(Call.id)).where(Call.company_id.in_(ids))))
        for c in s.scalars(select(Company).where(Company.campaign_id == cid).order_by(Company.id)):
            ph, em = c.primary("phone"), c.primary("email")
            print("SCHED row", c.id, c.name, "|", c.city, "|", c.data_status, "|", ph.value if ph else "-", "|", em.value if em else "-")
elif cmd == "teardown":
    cid = int(sys.argv[2])
    with session_scope() as s:
        s.get(Campaign, cid).status = "paused"
    b = get_setting("schedule_backup", {}).get("backup")
    if b:
        put_setting("schedule", b)
        put_setting("schedule_backup", {})
    print("SCHED teardown done; schedule", get_setting("schedule"))
