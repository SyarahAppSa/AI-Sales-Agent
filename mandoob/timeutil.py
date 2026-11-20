"""أدوات الوقت: التخزين UTC والعرض بتوقيت الرياض."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .config import RIYADH

AR_DAYS = {0: "الإثنين", 1: "الثلاثاء", 2: "الأربعاء", 3: "الخميس", 4: "الجمعة", 5: "السبت", 6: "الأحد"}
AR_MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر",
             "أكتوبر", "نوفمبر", "ديسمبر"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_riyadh() -> datetime:
    return datetime.now(RIYADH)


def riyadh_today() -> date:
    return now_riyadh().date()


def to_riyadh(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(RIYADH)


def riyadh_to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=RIYADH)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def riyadh_day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=RIYADH)
    return riyadh_to_utc(start), riyadh_to_utc(start + timedelta(days=1))


EN_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _en() -> bool:
    try:
        from .i18n import current_lang
        return current_lang() == "en"
    except Exception:  # noqa: BLE001
        return False


def fmt(dt: datetime | None, with_day: bool = True) -> str:
    r = to_riyadh(dt)
    if r is None:
        return "—"
    hour = r.hour % 12 or 12
    if _en():
        s = f"{r.day} {EN_MONTHS[r.month - 1]} {r.year}, {hour}:{r.minute:02d} {'AM' if r.hour < 12 else 'PM'}"
        return f"{EN_DAYS[r.weekday()]} {s}" if with_day else s
    ampm = "ص" if r.hour < 12 else "م"
    s = f"{r.day} {AR_MONTHS[r.month - 1]} {r.year}، {hour}:{r.minute:02d} {ampm}"
    return f"{AR_DAYS[r.weekday()]} {s}" if with_day else s


def fmt_date(d: date | None) -> str:
    if d is None:
        return "—"
    if _en():
        return f"{EN_DAYS[d.weekday()]} {d.day} {EN_MONTHS[d.month - 1]} {d.year}"
    return f"{AR_DAYS[d.weekday()]} {d.day} {AR_MONTHS[d.month - 1]} {d.year}"
