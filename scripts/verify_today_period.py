"""تحقق على الخادم ببيانات المنصة الفعلية: «عملاء اليوم» ليوم ١ نوفمبر، والفترة ١–٢٠ نوفمبر، ومطابقة Excel للجدول."""
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from mandoob.config import settings  # noqa: E402
from mandoob.db import session_scope  # noqa: E402
from mandoob.models import CostEntry, DiscoveryRun  # noqa: E402
from mandoob.web.app import app  # noqa: E402

with session_scope() as s:
    before = (s.scalar(select(func.count(CostEntry.id))), s.scalar(select(func.count(DiscoveryRun.id))))
with TestClient(app, base_url="https://mandoob.live") as c:
    r = c.post("/admin/login/password", data={"email": settings.admin_email, "password": settings.admin_password},
               follow_redirects=False)
    print("PERIOD login", r.status_code, r.headers.get("location"))
    for label, qs in [("يوم ١ نوفمبر", "mode=day&day=2026-11-01"), ("١–٢٠ نوفمبر", "mode=range&from=2026-11-01&to=2026-11-20"),
                      ("يوم ٢٠ نوفمبر", "mode=day&day=2026-11-20"), ("نهاية قبل بداية", "mode=range&from=2026-11-20&to=2026-11-01")]:
        html = c.get("/today?" + qs).text
        m = re.search(r"الفترة المعروضة: (.*?) — (\d+) عميل", html)
        x = c.get("/today.xlsx?" + qs)
        xr = len([r for r in load_workbook(io.BytesIO(x.content)).active.iter_rows(min_row=4, values_only=True)
                  if r and r[0]]) if x.status_code == 200 else None
        print("PERIOD", label, "| table:", m.group(2) if m else "-", "| shown:", m.group(1) if m else "-",
              "| excel rows:", xr, "| excel http", x.status_code,
              "| empty msg:", "لا يوجد عملاء في الفترة المختارة" in html,
              "| warning:", "تاريخ النهاية قبل تاريخ البداية" in html, flush=True)
        dates = re.findall(r"(\d{1,2}) نوفمبر 2026", html)
with session_scope() as s:
    after = (s.scalar(select(func.count(CostEntry.id))), s.scalar(select(func.count(DiscoveryRun.id))))
print("PERIOD no_new_cost_or_runs", before == after, before, after)
