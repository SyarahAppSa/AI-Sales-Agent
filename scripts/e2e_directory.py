"""دفعة اختبار حقيقية لـ«اكتشاف عملاء جدد» بميزانية محددة عبر العامل، ثم الأرقام الفعلية لكل مصدر."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select  # noqa: E402

from mandoob import directory as D  # noqa: E402
from mandoob.db import init_db, session_scope  # noqa: E402
from mandoob.models import Company, DirectoryRun, LeadStub  # noqa: E402

init_db()
budget, limit = float(sys.argv[1]), int(sys.argv[2])
print("DIR probe", D.probe_sources(), flush=True)
with session_scope() as s:
    for x in D.source_status(s):
        print("DIR source", x["label"], "|", x["status"], "|", x.get("why", ""), flush=True)
    r = D.request(s, sources=["claude_directory"], params={
        "activities": ["transport.freight", "transport.cold_chain", "construction.contractors", "industry.food_factories",
                       "industry.water_factories", "industry.building_material_factories"],
        "scope": {"places": ["SA-01/riyadh", "SA-02/jeddah", "SA-04/dammam"]}, "stage1_limit": limit},
        budget=budget, user_id=None)
    rid = r.id
print("DIR run", rid, flush=True)
t0 = time.time()
while time.time() - t0 < 3300:
    with session_scope() as s:
        st = s.get(DirectoryRun, rid).status
    if st in ("done", "stopped", "failed"):
        break
    time.sleep(20)
with session_scope() as s:
    r = s.get(DirectoryRun, rid)
    print("DIR status", r.status, "| stats", r.stats, flush=True)
    for line in (r.log or [])[-12:]:
        print("DIR log", line, flush=True)
    for x in s.scalars(select(LeadStub).where(LeadStub.run_id == rid).order_by(LeadStub.id)):
        co = s.get(Company, x.company_id) if x.company_id else None
        ph, em = (co.primary("phone"), co.primary("email")) if co else (None, None)
        print("DIR row", x.status, "|", x.name, "|", x.activity[:40], "|", x.city, "|", x.source_url[:90], "|",
              ph.value if ph else "-", "|", (ph.source_url[:70] if ph else "-"), "|", em.value if em else "-", "|",
              (em.source_url[:70] if em else "-"), flush=True)
    for row in D.daily_by_source(s):
        print("DIR daily", row, flush=True)
