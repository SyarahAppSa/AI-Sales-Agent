"""دفعة تجريبية متنوعة القطاعات من أداة «البحث عن العملاء» عبر العامل، ثم تقرير بالسجلات ومصادرها.
  python scripts/e2e_sectors.py <حد الدفعة> <المدينة> <نشاط> [نشاط ...]
  python scripts/e2e_sectors.py --rows <من UTC ISO> <إلى UTC ISO>     ← سجلات فترة سابقة
"""
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import func, select  # noqa: E402

from mandoob import prospecting as P  # noqa: E402
from mandoob.db import init_db, session_scope  # noqa: E402
from mandoob.models import Call, Company, CostEntry, OutboundMessage  # noqa: E402
from mandoob.timeutil import to_riyadh  # noqa: E402

init_db()


def rows(cid, a, b=None):
    with session_scope() as s:
        q = select(Company).where(Company.campaign_id == cid, Company.first_found_at >= a)
        if b:
            q = q.where(Company.first_found_at < b)
        out = list(s.scalars(q.order_by(Company.category_id, Company.id)))
        for c in out:
            ph, em = c.primary("phone"), c.primary("email")
            print("ROW", c.id, "|", c.category.label if c.category else "-", "|", c.name, "|", (c.activity or "")[:70], "|",
                  c.city, "|", c.data_status, "|", c.vehicle_indicator or "-", "|", c.entity_type or "-", "|",
                  ph.value if ph else "-", "|", ph.source_url if ph else "-", "|", em.value if em else "-", "|",
                  em.source_url if em else "-", "|", (c.fit_evidence_url or "-"), "|",
                  f"{to_riyadh(c.first_found_at):%Y-%m-%d %H:%M}", flush=True)
        return out


if sys.argv[1] == "--rows":
    with session_scope() as s:
        cid = P.get_config(s)["campaign_id"]
    rows(cid, datetime.fromisoformat(sys.argv[2]), datetime.fromisoformat(sys.argv[3]))
    sys.exit(0)

batch, city, acts = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
with session_scope() as s:
    camp = P.save_config(s, cities=[city], regions=[], activities=acts, daily_target=400, scheduled=False, time="06:00")
    s.flush()
    cid = camp.id
    print("SEC categories", [(c.label, c.quota, c.keywords_ar) for c in camp.categories if c.active], flush=True)
    req = P.request_run(s, cid, batch, None)
t0 = datetime.fromisoformat(req["requested_at"])
print("SEC requested", req, flush=True)
w = time.time()
while time.time() - w < 3300:
    with session_scope() as s:
        req = P.pending_request(s)
    if req.get("state") in ("done", "failed"):
        break
    time.sleep(15)
began = datetime.fromisoformat(req.get("started_at") or req["requested_at"])
end = datetime.fromisoformat(req.get("finished_at") or datetime.utcnow().isoformat())
out = rows(cid, t0)
with session_scope() as s:
    cost = s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(CostEntry.at >= t0)) or 0
    comp = [c for c in out if c.data_status == "complete"]
    print("SEC state", req.get("state"), req.get("error", ""), "| picked_after_sec", int((began - t0).total_seconds()),
          "| minutes", round((end - began).total_seconds() / 60, 1), "| riyadh", f"{to_riyadh(began):%H:%M}",
          "→", f"{to_riyadh(end):%H:%M}", flush=True)
    print("SEC new", len(out), "complete", len(comp), "confirmed_vehicle", sum(c.vehicle_indicator == "confirmed" for c in comp),
          "| cost_usd", round(float(cost), 2), "| per_complete", round(float(cost) / max(len(comp), 1), 2), flush=True)
    by = {}
    for c in out:
        k = c.category.label if c.category else "-"
        by.setdefault(k, [0, 0])
        by[k][0 if c.data_status == "complete" else 1] += 1
    print("SEC by_sector complete/incomplete", by, flush=True)
    real = select(Company.id).where(Company.is_test.is_(False))
    print("SEC zero real_calls", s.scalar(select(func.count(Call.id)).where(Call.company_id.in_(real))),
          "real_emails_sent", s.scalar(select(func.count(OutboundMessage.id)).where(
              OutboundMessage.company_id.in_(real), OutboundMessage.sent_at.isnot(None))), flush=True)
