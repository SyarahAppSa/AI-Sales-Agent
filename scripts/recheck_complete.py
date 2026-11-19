"""إعادة فحص سجلات البحث الآلي بقاعدة «الوسيلة تخص المنشأة نفسها»: ما لا يطابق يُنقل إلى «قيد الاستكمال»."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select  # noqa: E402

from mandoob.companies import is_complete, log_activity, missing_fields  # noqa: E402
from mandoob.db import init_db, session_scope  # noqa: E402
from mandoob.models import Company  # noqa: E402

init_db()
with session_scope() as s:
    moved = []
    for c in s.scalars(select(Company).where(Company.is_test.is_(False), Company.lead_source == "discovery",
                                             Company.data_status == "complete")):
        if not is_complete(c):
            c.data_status, c.counted_on = "incomplete", None
            why = "، ".join(missing_fields(c))
            log_activity(s, c.id, "data", "نُقل إلى قيد الاستكمال بعد التدقيق", f"ينقص: {why}")
            moved.append((c.id, c.name, why))
    for m in moved:
        print("RECHECK moved", *m, flush=True)
    print("RECHECK total_moved", len(moved), flush=True)
