"""الاختبار الحي للبحث الآلي: مدينة وفئة فقط ← أول 20 عميلًا حقيقيًا بأرقامهم وإيميلاتهم ومصادرهم، ثم دفعة جديدة دون تكرار.

يتطلب: MANDOOB_ANTHROPIC_API_KEY (وكيل البحث وقراءة الصفحات يعمل على خوادم Anthropic).
اختياري: GOOGLE_PLACES_API_KEY و BRAVE_SEARCH_API_KEY لمصادر إضافية.

مثال:
  python3 scripts/live_auto_search.py --city الرياض --text "المنشآت وأصحاب الأنشطة الذين يستخدمون سيارات في عملهم" --batch 20
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="الرياض")
    ap.add_argument("--text", default="المنشآت وأصحاب الأنشطة الذين يستخدمون سيارات في عملهم")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--batches", type=int, default=2)
    args = ap.parse_args()

    from sqlalchemy import func, select

    from mandoob.config import settings
    from mandoob.db import init_db, session_scope
    from mandoob.discovery.agent import plan_queries
    from mandoob.discovery.runner import run_campaign_discovery
    from mandoob.models import Campaign, Category, Company, CostEntry, DiscoveryRun
    from mandoob.normalize import format_phone_display
    from mandoob.targeting import parse_request
    from mandoob.timeutil import riyadh_today

    if not settings.ai_enabled:
        sys.exit("اضبط MANDOOB_ANTHROPIC_API_KEY أولًا — البحث الآلي يعتمد على وكيل Claude.")
    init_db()
    text = f"ابحث لي يوميًا في {args.city} عن {args.text}"
    parsed = parse_request(text, 400)
    with session_scope() as s:
        camp = Campaign(name=f"البحث الآلي — {args.city}", request_text=text, status="active", daily_target=400,
                        exclusions=parsed["exclusions"], send_enabled=False)
        s.add(camp)
        s.flush()
        for c in parsed["categories"]:
            s.add(Category(campaign_id=camp.id, sector_key=c["sector_key"], label=c["label"], cities=[args.city],
                           keywords_ar=plan_queries(text, [args.city]), keywords_en=[], quota=c["quota"]))
        cid = camp.id
    print(f"الحملة #{cid}: {text}")
    seen: set[int] = set()
    for b in range(1, args.batches + 1):
        run_campaign_discovery(cid, max_new=args.batch)
        with session_scope() as s:
            rows = [c for c in s.scalars(select(Company).where(Company.campaign_id == cid).order_by(Company.id))
                    if c.id not in seen]
            print(f"\n================ الدفعة {b}: {sum(1 for c in rows if c.data_status == 'complete')} مكتمل، "
                  f"{sum(1 for c in rows if c.data_status == 'incomplete')} قيد الاستكمال ================")
            for c in rows:
                seen.add(c.id)
                ph, em = c.primary("phone"), c.primary("email")
                print(f"\n• {c.name} — {c.activity} — {c.city} [{'مكتمل' if c.data_status == 'complete' else 'قيد الاستكمال'}]")
                print(f"  الهاتف: {format_phone_display(ph.value) if ph else '—'}   ← {ph.source_url if ph else ''}")
                print(f"  البريد: {em.value if em else '—'}   ← {em.source_url if em else ''}")
                print(f"  السبب: {c.fit_reason}\n  الدليل: {c.fit_evidence_url or '—'}")
            run = s.scalar(select(DiscoveryRun).where(DiscoveryRun.campaign_id == cid))
            cost = s.scalar(select(func.sum(CostEntry.cost_usd)).where(CostEntry.day == riyadh_today())) or 0
            print(f"\nمكررات مستبعدة حتى الآن: {run.duplicate_count} — التكلفة التقديرية اليوم: ${cost:.2f}")


if __name__ == "__main__":
    main()
