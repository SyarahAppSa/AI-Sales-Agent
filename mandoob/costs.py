"""دفتر التكاليف اليومي (تقديري) لكل خدمة."""
from __future__ import annotations

from .db import session_scope
from .models import CostEntry
from .timeutil import riyadh_today

# أسعار تقديرية بالدولار — عدّلها من متغيرات البيئة/الإعدادات حسب عقدك الفعلي
UNIT_PRICES = {
    "google_places": 0.035,   # Text Search (Enterprise fields) لكل طلب
    "brave": 0.005,           # لكل استعلام
    "voice_minute": 0.15,     # دقيقة مكالمة (اتصال + صوت ذكي)
}


def record_cost(service: str, units: float, cost_usd: float | None = None, *, campaign_id=None,
                note: str = "", session=None) -> None:
    if cost_usd is None:
        cost_usd = units * UNIT_PRICES.get(service, 0.0)
    entry = CostEntry(day=riyadh_today(), service=service, units=units, cost_usd=round(cost_usd, 5),
                      campaign_id=campaign_id, note=note[:200])
    if session is not None:
        session.add(entry)
        return
    with session_scope() as s:
        s.add(entry)
