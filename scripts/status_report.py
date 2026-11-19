"""ملخص النتائج لكل مصدر + التحقق من صفر تواصل (للتقرير)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import func, select  # noqa: E402

from mandoob.db import session_scope  # noqa: E402
from mandoob.models import Call, Campaign, Company, CostEntry, DiscoveryRun, OutboundMessage  # noqa: E402
from mandoob.timeutil import fmt  # noqa: E402

with session_scope() as s:
    camps = list(s.scalars(select(Campaign).where(Campaign.name.like("البحث الآلي%")).order_by(Campaign.id)))
    for camp in camps:
        print("STATUS campaign", camp.id, camp.name, camp.status)
        for r in s.scalars(select(DiscoveryRun).where(DiscoveryRun.campaign_id == camp.id).order_by(DiscoveryRun.id)):
            print("STATUS run", r.run_date, r.status, "start", fmt(r.started_at), "end", fmt(r.finished_at),
                  "complete", r.complete_count, "incomplete", r.incomplete_count, "dup", r.duplicate_count,
                  "rejected", r.rejected_count)
            print("STATUS per_source", json.dumps(r.per_source, ensure_ascii=False))
            print("STATUS errors", json.dumps(r.source_errors, ensure_ascii=False))
            print("STATUS shortfall", r.shortfall, (r.shortfall_reason or "").replace("\n", " | "))
        comps = list(s.scalars(select(Company).where(Company.campaign_id == camp.id).order_by(Company.id)))
        for c in comps:
            ph, em = c.primary("phone"), c.primary("email")
            print("ROW", c.id, "|", c.name, "|", c.activity, "|", c.city, "|", c.data_status, "|",
                  ph.value if ph else "-", "|", ph.source_url if ph else "-", "|", em.value if em else "-", "|",
                  em.source_url if em else "-", "|", (c.fit_reason or "")[:140], "|", c.fit_evidence_url or "-")
    real = select(Company.id).where(Company.is_test.is_(False))
    print("STATUS zero_contact", json.dumps({
        "messages_sent_real": s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.sent_at.isnot(None),
                                                                                   OutboundMessage.company_id.in_(real))),
        "messages_queued_any": s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.status.in_(["queued", "sending"]))),
        "calls_real": s.scalar(select(func.count(Call.id)).where(Call.company_id.in_(real))),
        "send_enabled_active_campaigns": s.scalar(select(func.count(Campaign.id)).where(Campaign.status == "active",
                                                                                        Campaign.send_enabled.is_(True))),
    }))
    costs = dict(s.execute(select(CostEntry.note, func.sum(CostEntry.cost_usd)).group_by(CostEntry.note)).all())
    print("STATUS cost_by_step", json.dumps({k: round(v, 3) for k, v in costs.items()}, ensure_ascii=False),
          "total", round(sum(costs.values()), 2))
