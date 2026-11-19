"""فهم ردود العملاء: نوع الرد وموعد المتابعة المطلوب (بالعربية واللهجة السعودية)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .normalize import normalize_arabic
from .timeutil import AR_DAYS, riyadh_to_utc, to_riyadh

INTENT_LABELS = {
    "callback_request": "طلب اتصال",
    "meeting_request": "طلب اجتماع",
    "quote_request": "طلب عرض سعر",
    "details_request": "طلب تفاصيل",
    "interested": "مهتم",
    "not_interested": "غير مهتم",
    "unsubscribe": "طلب إيقاف التواصل",
    "out_of_office": "رد تلقائي",
    "other": "رد آخر",
}

_UNSUB = ["الغاء الاشتراك", "ايقاف", "اوقفوا", "لا ترسلوا", "لا تراسلونا", "لا تتواصلوا", "احذفوا", "شيلوا",
          "unsubscribe", "remove me", "stop emailing", "do not contact", "لا نرغب في استلام"]
_NOT_INTERESTED = ["غير مهتم", "مو مهتم", "ما نحتاج", "لا نحتاج", "لسنا بحاجه", "لسنا مهتمين", "غير مهتمين",
                   "not interested", "no thanks", "لا شكرا"]
_CALLBACK = ["اتصلوا", "اتصل ", "كلموني", "كلمني", "اتصلو", "كلمونا", "تواصلوا معي", "تتصلون", "call me", "give me a call",
             "اتصال", "رقمي"]
_MEETING = ["اجتماع", "زياره", "نلتقي", "meeting", "demo", "عرض توضيحي"]
_QUOTE = ["عرض سعر", "تسعيره", "الاسعار", "السعر", "كم التكلفه", "quotation", "quote", "pricing"]
_DETAILS = ["تفاصيل", "معلومات اكثر", "ارسلوا", "more details", "more information", "brochure", "ملف"]
_INTERESTED = ["مهتم", "مهتمين", "interested", "نرغب", "ابغى", "نبغى", "حياكم", "يناسبنا", "موافق"]
_OOO = ["خارج المكتب", "out of office", "رد تلقائي", "automatic reply", "اجازه"]

_DAYS = {"الاحد": 6, "الاثنين": 0, "الثلاثاء": 1, "الاربعاء": 2, "الخميس": 3, "الجمعه": 4, "السبت": 5,
         "sunday": 6, "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5}
# (بداية النافذة، نهايتها، الوصف)
_PARTS = [
    (("بعد الظهر", "بعد الظهر", "العصر", "afternoon"), (13, 16, "بعد الظهر (1:00–4:00 م)")),
    (("الظهر", "الظهر", "noon"), (12, 13, "الظهر (12:00–1:00 م)")),
    (("الصباح", "صباحا", "الصبح", "morning"), (9, 12, "الصباح (9:00–12:00 ص)")),
    (("المساء", "مساء", "الليل", "evening"), (17, 20, "المساء (5:00–8:00 م)")),
]


@dataclass
class ParsedReply:
    intent: str
    label: str
    requested_at: datetime | None  # UTC
    window_label: str | None
    summary: str


def _has(t: str, words) -> bool:
    return any(normalize_arabic(w).lower() in t for w in words)


def classify_intent(text: str) -> str:
    t = " " + normalize_arabic(text).lower() + " "
    if _has(t, _OOO) and len(t) < 600:
        return "out_of_office"
    if _has(t, _UNSUB):
        return "unsubscribe"
    if _has(t, _NOT_INTERESTED):
        return "not_interested"
    if _has(t, _MEETING):
        return "meeting_request"
    if _has(t, _CALLBACK):
        return "callback_request"
    if _has(t, _QUOTE):
        return "quote_request"
    if _has(t, _DETAILS):
        return "details_request"
    if _has(t, _INTERESTED):
        return "interested"
    return "other"


def parse_requested_time(text: str, received_at_utc: datetime) -> tuple[datetime | None, str | None]:
    """يستخرج موعد المتابعة المطلوب. يعيد (الوقت UTC، وصف النافذة بتوقيت الرياض)."""
    t = normalize_arabic(text).lower()
    t = t.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    base = to_riyadh(received_at_utc)
    day = None
    if re.search(r"بعد\s*بكر[هة]|بعد\s*غد", t):
        day = base.date() + timedelta(days=2)
    elif re.search(r"بكر[هة]|غدا|غدًا|tomorrow", t):
        day = base.date() + timedelta(days=1)
    elif re.search(r"\bاليوم\b|today", t):
        day = base.date()
    else:
        for name, wd in _DAYS.items():
            if re.search(rf"(^|\s|يوم\s*){name}", t):
                delta = (wd - base.weekday()) % 7
                if "القادم" in t or "الجاي" in t or "next" in t:
                    delta = delta or 7
                day = base.date() + timedelta(days=delta)
                break
    start_h, end_h, window = None, None, None
    for words, (sh, eh, label) in _PARTS:
        if any(normalize_arabic(w) in t for w in words):
            start_h, end_h, window = sh, eh, label
            break
    m = re.search(r"(?:الساعه|الساعة|at)\s*(\d{1,2})(?::(\d{2}))?\s*(ص|م|am|pm)?", t) or \
        re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(ص|م|am|pm)\b", t)
    minute = 0
    if m:
        h = int(m.group(1))
        minute = int(m.group(2) or 0)
        suffix = m.group(3)
        if suffix in ("م", "pm") and h < 12:
            h += 12
        elif suffix is None and (start_h or 0) >= 12 and h < 12:
            h += 12
        elif suffix is None and h < 8:
            h += 12  # "الساعة 3" في سياق العمل تعني 3 م
        start_h, window = h, f"الساعة {h % 12 or 12}:{minute:02d} {'م' if h >= 12 else 'ص'}"
    if day is None and start_h is None:
        return None, None
    if day is None:
        day = base.date() if start_h > base.hour else base.date() + timedelta(days=1)
    if day.weekday() in (4, 5):  # الجمعة والسبت: عطلة نهاية الأسبوع — ننقلها إلى الأحد
        day += timedelta(days=6 - day.weekday())
    if start_h is None:
        start_h, window = 10, "خلال اليوم"
    local = datetime(day.year, day.month, day.day, start_h, minute)
    label = f"{AR_DAYS[day.weekday()]} {day.day}/{day.month} — {window}"
    return riyadh_to_utc(local), label


def strip_quoted(text: str) -> str:
    """يحذف النص المقتبس من الرسالة الأصلية في الرد."""
    lines = []
    for line in (text or "").splitlines():
        l = line.strip()
        if l.startswith(">"):
            continue
        if re.match(r"^(On .* wrote:|في .* كتب|From:|من:|-----Original Message|________________)", l):
            break
        lines.append(line)
    return "\n".join(lines).strip()


REPLY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["intent", "summary", "requested_datetime_riyadh", "window_label"],
    "properties": {
        "intent": {"type": "string", "enum": list(INTENT_LABELS)},
        "summary": {"type": "string"},
        "requested_datetime_riyadh": {"type": "string", "description": "YYYY-MM-DDTHH:MM أو فارغ"},
        "window_label": {"type": "string"},
    },
}


def parse_reply(text: str, received_at_utc: datetime) -> ParsedReply:
    body = strip_quoted(text)
    from . import ai
    rule_intent = classify_intent(body)
    rule_at, rule_window = parse_requested_time(body, received_at_utc)
    if ai.settings.ai_enabled:
        try:
            base = to_riyadh(received_at_utc)
            out = ai.ai_json(
                "أنت تصنّف ردود العملاء على عروض مبيعات B2B باللغة العربية واللهجة السعودية.",
                f"وقت استلام الرد بتوقيت الرياض: {base:%Y-%m-%d %H:%M} ({AR_DAYS[base.weekday()]}).\n"
                f"نص الرد:\n{body}\n\nحدد نوع الرد، ولخصه في جملة، واستخرج موعد المتابعة الذي طلبه العميل "
                "بتوقيت الرياض (أقرب موعد قادم يطابق كلامه؛ «بعد الظهر» = 13:00). اترك الموعد فارغًا إن لم يحدده.",
                REPLY_SCHEMA, note="reply", max_tokens=1500)
            at = None
            if out.get("requested_datetime_riyadh"):
                try:
                    at = riyadh_to_utc(datetime.fromisoformat(out["requested_datetime_riyadh"][:16]))
                except ValueError:
                    at = None
            # الإيقاف يُحسم بالقواعد أيضًا احتياطًا (لا نخاطر بتجاهل طلب إيقاف)
            intent = "unsubscribe" if rule_intent == "unsubscribe" else out["intent"]
            return ParsedReply(intent, INTENT_LABELS[intent], at or rule_at,
                               out.get("window_label") or rule_window, out.get("summary", ""))
        except ai.AIUnavailable:
            pass
    return ParsedReply(rule_intent, INTENT_LABELS[rule_intent], rule_at, rule_window, body[:200])
