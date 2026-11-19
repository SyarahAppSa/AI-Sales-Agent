"""اختبار حقيقي على الخادم: «مدينة الرياض فقط» مقابل «منطقة الرياض كاملة» (بما فيها الخرج) بدفعة صغيرة عبر العامل.
  python scripts/e2e_geo.py <نشاط> <حد دفعة المدينة> <حد دفعة المنطقة>
"""
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import func, select  # noqa: E402

from mandoob import geo, prospecting as P  # noqa: E402
from mandoob.db import init_db, session_scope  # noqa: E402
from mandoob.models import Call, Campaign, Company, CostEntry, OutboundMessage  # noqa: E402
from mandoob.timeutil import to_riyadh  # noqa: E402

init_db()
act, n_city, n_region = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])


def batch(label, scope, n):
    with session_scope() as s:
        camp = P.save_config(s, scope=scope, activities=[act], daily_target=400, scheduled=False, time="06:00")
        s.flush()
        for c in camp.categories:
            c.cursor = 0  # يبدأ الاختبار من أول موقع في النطاق
        cid = camp.id
        places = geo.scope_places(scope)
        print(f"GEO [{label}] scope:", geo.scope_summary(scope), flush=True)
        print(f"GEO [{label}] locations in queries ({len(places)}):", "، ".join(geo.label(p) for p in places[:30]),
              flush=True)
        print(f"GEO [{label}] has_kharj:", "SA-01/al-kharj" in places, flush=True)
        req = P.request_run(s, cid, n, None)
    t0 = datetime.fromisoformat(req["requested_at"])
    w = time.time()
    while time.time() - w < 2400:
        with session_scope() as s:
            req = P.pending_request(s)
        if req.get("state") in ("done", "failed"):
            break
        time.sleep(15)
    began = datetime.fromisoformat(req.get("started_at") or req["requested_at"])
    end = datetime.fromisoformat(req.get("finished_at") or datetime.utcnow().isoformat())
    with session_scope() as s:
        rows = list(s.scalars(select(Company).where(Company.campaign_id == cid, Company.first_found_at >= t0)
                              .order_by(Company.id)))
        cost = s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(CostEntry.at >= t0)) or 0
        print(f"GEO [{label}] state", req.get("state"), req.get("error", ""), "| minutes",
              round((end - began).total_seconds() / 60, 1), "| new", len(rows), "complete",
              sum(c.data_status == "complete" for c in rows), "| cost_usd", round(float(cost), 2), flush=True)
        for c in rows:
            ph, em = c.primary("phone"), c.primary("email")
            print(f"GEO [{label}] ROW", c.id, "|", c.name, "|", (c.activity or "")[:50], "|",
                  geo.label(c.place_id, True) if c.place_id else c.city, "|", c.data_status, "|",
                  ph.value if ph else "-", "|", ph.source_url if ph else "-", "|", em.value if em else "-", "|",
                  em.source_url if em else "-", "|", "branches:" + ",".join(b["city"] for b in (c.branches or [])),
                  flush=True)
        st = P.status(s)
        for r in st["regions"]:
            print(f"GEO [{label}] REGION", r["label"], "|", r["status"], "| searched", r["searched"], "| complete",
                  r["complete"], "| incomplete", r["incomplete"], "| dup", r["dup"], "| out", r["out"], "| last",
                  r["last"], flush=True)
        for x in st["nexts"]:
            print(f"GEO [{label}] NEXT", x["label"], "→", x["place"], "«" + x["query"] + "»", x["pos"], "/", x["of"],
                  flush=True)
        run = st["run"]
        for line in (run.log or [])[-6:] if run else []:
            print(f"GEO [{label}] LOG", line, flush=True)


batch("مدينة الرياض فقط", {"places": ["SA-01/riyadh"]}, n_city)
batch("منطقة الرياض كاملة", {"regions": ["SA-01"]}, n_region)
with session_scope() as s:
    real = select(Company.id).where(Company.is_test.is_(False))
    print("GEO zero real_calls", s.scalar(select(func.count(Call.id)).where(Call.company_id.in_(real))),
          "real_emails_sent", s.scalar(select(func.count(OutboundMessage.id)).where(
              OutboundMessage.company_id.in_(real), OutboundMessage.sent_at.isnot(None))), flush=True)
