"""دفعة اختبار صغيرة من المصادر المتاحة الآن (بحث الويب، حسابات التواصل، أدلة الويب) بميزانية محددة، ثم عينة تتبع
كل عميل: عبارة البحث ← أول نتيجة ← مرجع الخرائط/الحساب ← الموقع ← صفحة الهاتف ← صفحة البريد."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select  # noqa: E402

from mandoob import directory as D  # noqa: E402
from mandoob.db import init_db, session_scope  # noqa: E402
from mandoob.models import Company, CompanySource, DirectoryRun, LeadStub  # noqa: E402
from mandoob.timeutil import now_utc, riyadh_today  # noqa: E402

init_db()
budget, limit = float(sys.argv[1]), int(sys.argv[2])
with session_scope() as s:
    for r in s.scalars(select(DirectoryRun).where(DirectoryRun.status.in_(["pending", "running"]))):
        r.status = "failed"
        r.log = (r.log or []) + [f"{now_utc():%H:%M:%S} أُلغيت: تعلّقت قبل إصلاح مهلة النداء"]
    s.flush()
    r = D.request(s, sources=["web_search", "social", "claude_directory"], params={
        "activities": ["industry.water_factories", "education.private_schools", "field_services.maintenance",
                       "transport.freight", "construction.contractors", "food.bakeries"],
        "scope": {"places": ["SA-05/buraydah", "SA-14/abha", "SA-04/dammam", "SA-01/riyadh", "SA-02/jeddah"]},
        "stage1_limit": limit, "day_index": riyadh_today().toordinal()}, budget=budget, user_id=None)
    rid = r.id
print("WEB run", rid, flush=True)
t0 = time.time()
while time.time() - t0 < 3300:
    with session_scope() as s:
        st = s.get(DirectoryRun, rid).status
    if st in ("done", "stopped", "failed"):
        break
    time.sleep(20)
with session_scope() as s:
    r = s.get(DirectoryRun, rid)
    print("WEB status", r.status, "| stats", r.stats, flush=True)
    for line in (r.log or [])[-25:]:
        print("WEB log", line, flush=True)
    for x in s.scalars(select(LeadStub).where(LeadStub.run_id == rid).order_by(LeadStub.id)):
        co = s.get(Company, x.company_id) if x.company_id else None
        ph, em = (co.primary("phone"), co.primary("email")) if co else (None, None)
        print("WEB row", x.status, "|", D.SOURCES.get(x.source, {}).get("label", x.source), "|", x.name, "|", x.city,
              "| Q:", x.query, "| first:", x.found_url or "-", "| maps:", x.maps_url or "-", "| social:", x.social_url or "-",
              "| site:", (co.website if co else x.website) or "-", "| phone:", ph.value if ph else "-", "←",
              ph.source_url if ph else "-", "| email:", em.value if em else "-", "←", em.source_url if em else "-",
              "| dup_of:", x.duplicate_of or "-", flush=True)
    for row in D.daily_by_source(s):
        print("WEB daily", row, flush=True)
