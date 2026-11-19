"""إعادة تشغيل بحث حملة بنفس الإعدادات (اختبار منع التكرار/الاستئناف): يُظهر قبل وبعد والمكررات."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import func, select  # noqa: E402

from mandoob.db import session_scope  # noqa: E402
from mandoob.discovery.runner import run_campaign_discovery  # noqa: E402
from mandoob.models import Campaign, Company, CostEntry, DiscoveryRun  # noqa: E402
from mandoob.db import utcnow  # noqa: E402

cid, batch = int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 20


def snap():
    with session_scope() as s:
        run = s.scalar(select(DiscoveryRun).where(DiscoveryRun.campaign_id == cid).order_by(DiscoveryRun.id.desc()))
        return (s.scalar(select(func.count(Company.id)).where(Company.campaign_id == cid, Company.data_status == "complete")),
                s.scalar(select(func.count(Company.id)).where(Company.campaign_id == cid)),
                run.duplicate_count if run else 0)


with session_scope() as s:
    s.get(Campaign, cid).status = "active"
b = snap()
t0, w0 = utcnow(), time.time()
print("RERUN before complete/total/dups", b, flush=True)
try:
    run_campaign_discovery(cid, max_new=batch)
finally:
    with session_scope() as s:
        s.get(Campaign, cid).status = "paused"
a = snap()
with session_scope() as s:
    cost = s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(CostEntry.at >= t0)) or 0
    steps = dict(s.execute(select(CostEntry.note, func.sum(CostEntry.cost_usd)).where(CostEntry.at >= t0).group_by(CostEntry.note)).all())
print("RERUN after  complete/total/dups", a, flush=True)
print("RERUN new_complete", a[0] - b[0], "new_records", a[1] - b[1], "duplicates_found_this_run", a[2] - b[2])
print("RERUN minutes", round((time.time() - w0) / 60, 1), "cost_usd", round(float(cost), 3), "per_new_complete", round(float(cost) / max(a[0] - b[0], 1), 3))
print("RERUN cost_steps", {k: round(float(v), 3) for k, v in steps.items()})
