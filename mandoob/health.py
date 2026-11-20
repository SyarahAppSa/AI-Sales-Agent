"""«حالة البحث»: شريط واحد بالعربية يوضح السبب والإجراء بدل رسائل الأعطال التقنية الطويلة.

- يميّز بين **تعذّر مصدر واحد** (البحث مستمر بالمصادر الأخرى) و**توقف البحث كليًا**.
- التفاصيل التقنية (نص الخطأ الأصلي) تبقى في «سجل التشغيل» للإدارة فقط، ولا تظهر في الشريط.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from .config import settings
from .timeutil import now_utc, riyadh_day_bounds_utc, riyadh_today

# (كلمات في نص الخطأ، السبب، الإجراء، يوقف البحث كليًا؟)
_RULES = [
    (("credit balance", "billing", "insufficient", "quota", "payment", "رصيد"),
     "رصيد خدمة البحث غير كافٍ", "اشحن رصيد حساب Claude (Anthropic) ثم أعد تشغيل البحث", True),
    (("authentication", "invalid x-api-key", "invalid api key", "401", "permission_error", "unauthorized"),
     "مفتاح خدمة البحث غير صالح أو منتهي", "حدّث المفتاح في إعدادات الخادم (Render) ثم أعد التشغيل", True),
    (("not configured", "غير مضبوطة", "غير مُعد", "missing key", "no api key"),
     "مفتاح المصدر غير مضبوط", "أضف المفتاح من إعدادات الخادم، أو اترك المصدر — البحث يكمل بالمصادر الأخرى", False),
    (("ميزانيت", "budget", "سقف الإنفاق"),
     "بلغ البحث الميزانية المحددة", "ارفع الميزانية من «اكتشاف عملاء جدد» إن أردت المتابعة اليوم", False),
    (("أوقف", "stopped by", "إيقاف البحث"),
     "أُوقف البحث يدويًا", "استأنف البحث من «البحث عن عملاء» عند الحاجة", False),
    (("429", "rate limit", "rate_limit", "overloaded", "529", "too many requests"),
     "خدمة البحث مزدحمة مؤقتًا", "لا يلزم إجراء — تُعاد المحاولة تلقائيًا", False),
    (("timeout", "timed out", "deadline", "مهلة", "readtimeout"),
     "انتهت مهلة الاتصال بالمصدر", "لا يلزم إجراء — ينتقل البحث للمصدر التالي ويعيد المحاولة لاحقًا", False),
    (("403", "cloudflare", "forbidden", "blocked", "محجوب", "geo"),
     "الموقع يمنع الوصول الآلي", "لا يلزم إجراء — المصدر اختياري والبحث يكمل بالمصادر الأخرى", False),
    (("connection", "dns", "network", "unreachable", "502", "503", "500"),
     "تعذّر الوصول إلى المصدر", "لا يلزم إجراء — تُعاد المحاولة تلقائيًا", False),
]


def classify(text: str) -> dict:
    t = (text or "").lower()
    for keys, cause, action, fatal in _RULES:
        if any(k in t for k in keys):
            return {"cause": cause, "action": action, "fatal": fatal}
    return {"cause": "تعذّر تنفيذ البحث في المصدر", "action": "راجع التفاصيل في سجل التشغيل", "fatal": False}


def _src_label(key: str) -> str:
    try:
        from .directory import SOURCES
        if key in SOURCES:
            return SOURCES[key]["label"]
    except Exception:  # noqa: BLE001
        pass
    try:
        from .discovery.runner import SOURCE_LABELS
        return SOURCE_LABELS.get(key, key)
    except Exception:  # noqa: BLE001
        return key


def search_status(s) -> dict:
    """حالة البحث الحالية لشريط «حالة البحث».

    level: run (يعمل الآن) | ok (اكتمل) | warn (مصدر تعذّر والبحث مستمر/إجراء اختياري) | err (توقف كليًا) | idle"""
    from .models import DirectoryRun, DiscoveryRun
    out = {"level": "idle", "title": "", "cause": "", "action": "", "failed_sources": [], "details": "/runs"}
    if not settings.ai_enabled:
        return {**out, "level": "err", "title": "توقف البحث", "cause": "خدمة البحث غير مضبوطة على الخادم",
                "action": "أضف مفتاح Claude في إعدادات الخادم (Render)", "fatal": True}
    today = riyadh_today()
    d0, _ = riyadh_day_bounds_utc(today)
    dr = s.scalar(select(DirectoryRun).where(DirectoryRun.created_at >= d0 - timedelta(hours=12))
                  .order_by(DirectoryRun.id.desc()))
    pr = s.scalar(select(DiscoveryRun).where(DiscoveryRun.run_date == today).order_by(DiscoveryRun.id.desc()))
    failed: dict[str, dict] = {}
    if dr is not None:
        for src, e in ((dr.stats or {}).get("_errors") or {}).items():
            failed[src] = {**classify(e.get("last", "")), "n": e.get("n", 1)}
    if pr is not None:
        for src, e in (pr.source_errors or {}).items():
            failed.setdefault(src, {**classify(str(e)), "n": 1})
    out["failed_sources"] = [{"label": _src_label(k), **v} for k, v in failed.items()]

    def one_source_msg():
        f = out["failed_sources"]
        names = "، ".join(x["label"] for x in f[:2]) + (f" و{len(f) - 2} غيرها" if len(f) > 2 else "")
        return f, names

    fatal = next((x for x in out["failed_sources"] if x["fatal"]), None)
    running = dr is not None and dr.status == "running" and dr.heartbeat and \
        dr.heartbeat >= now_utc() - timedelta(minutes=12)
    if dr is not None and dr.status == "failed":
        last = next((ln for ln in reversed(dr.log or []) if "فشل" in ln), "")
        c = classify(last)
        return {**out, "level": "err", "title": "توقف البحث", "cause": c["cause"], "action": c["action"]}
    if fatal is not None:
        return {**out, "level": "err", "title": "توقف البحث", "cause": fatal["cause"], "action": fatal["action"]}
    if dr is not None and dr.status == "stopped":
        last = next((ln for ln in reversed(dr.log or []) if ln.split(" ", 1)[-1].startswith("توقف")), "")
        c = classify(last)
        return {**out, "level": "warn", "title": "توقف البحث مؤقتًا", "cause": c["cause"], "action": c["action"]}
    if running:
        if out["failed_sources"]:
            f, names = one_source_msg()
            return {**out, "level": "warn", "title": "البحث يعمل — تعذّر مصدر",
                    "cause": f"تعذّر {names}: {f[0]['cause']}",
                    "action": "البحث مستمر بالمصادر الأخرى. " + f[0]["action"].replace("لا يلزم إجراء — ", "")}
        return {**out, "level": "run", "title": "البحث يعمل الآن", "cause": "جمع العملاء من المصادر جارٍ",
                "action": "تظهر النتائج في البطاقات تلقائيًا"}
    if dr is not None and dr.status == "pending":
        return {**out, "level": "run", "title": "البحث في الانتظار", "cause": "طُلبت دفعة بحث وستبدأ خلال دقيقة",
                "action": ""}
    if dr is not None or pr is not None:
        if out["failed_sources"]:
            f, names = one_source_msg()
            return {**out, "level": "warn", "title": "اكتمل آخر بحث — تعذّر مصدر",
                    "cause": f"تعذّر {names}: {f[0]['cause']}", "action": f[0]["action"]}
        return {**out, "level": "ok", "title": "اكتمل آخر بحث بنجاح", "cause": "كل المصادر عملت دون أعطال",
                "action": ""}
    nxt = ""
    try:
        from .directory import get_auto
        a = get_auto(s)
        if a.get("enabled"):
            nxt = f"البحث الآلي اليومي يبدأ الساعة {a.get('time')} بتوقيت الرياض"
    except Exception:  # noqa: BLE001
        pass
    return {**out, "level": "idle", "title": "لم يبدأ بحث اليوم بعد", "cause": nxt or "البحث الآلي اليومي غير مفعّل",
            "action": "" if nxt else "اضغط «البحث عن عملاء» لبدء دفعة"}
