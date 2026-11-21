"""مراقبة الأخطاء عبر Sentry — تعمل فقط إذا ضُبط SENTRY_DSN في البيئة.

لا تُرسل بيانات العملاء: لا كوكيز ولا ترويسات تعريف ولا أجسام الطلبات، وتُحجب الأرقام والبريد من الرسائل.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?966|0)?5\d{8}(?!\d)|\+\d{10,15}")
_SENSITIVE_KEYS = re.compile(r"pass|secret|token|key|auth|cookie|session|otp|dsn|credential", re.I)
_state = {"on": False, "component": None}


def scrub_text(v):
    if not isinstance(v, str):
        return v
    return _PHONE.sub("[phone]", _EMAIL.sub("[email]", v))


def _scrub(obj, depth=0):
    if depth > 8:
        return obj
    if isinstance(obj, dict):
        return {k: ("[filtered]" if isinstance(k, str) and _SENSITIVE_KEYS.search(k) else _scrub(v, depth + 1))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, depth + 1) for v in obj]
    return scrub_text(obj)


def before_send(event, hint):
    req = event.get("request")
    if isinstance(req, dict):
        for k in ("cookies", "data", "query_string", "env"):
            req.pop(k, None)
        hdr = req.get("headers") or {}
        req["headers"] = {k: v for k, v in hdr.items() if k.lower() in ("user-agent", "host", "referer", "accept-language")}
        if "url" in req:
            req["url"] = scrub_text(req["url"])
    event.pop("user", None)
    for key in ("message", "logentry", "exception", "breadcrumbs", "extra", "contexts"):
        if key in event:
            event[key] = _scrub(event[key])
    return event


def init(component: str) -> bool:
    """يفعّل Sentry لخدمة (web أو worker). يرجع True إن فُعّل."""
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn or _state["on"]:
        return _state["on"]
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            release=os.environ.get("RENDER_GIT_COMMIT") or None,
            server_name=component,
            send_default_pii=False,
            include_local_variables=False,
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.05") or 0),
            before_send=before_send,
            before_send_transaction=lambda e, h: before_send(e, h),
        )
        sentry_sdk.set_tag("component", component)
        _state.update(on=True, component=component)
        log.info("Sentry enabled for %s", component)
    except Exception as e:  # المراقبة لا تُسقط المنصة أبدًا
        log.warning("Sentry init failed: %s", e)
    return _state["on"]


def enabled() -> bool:
    return _state["on"]


def send_test(who: str = "") -> str | None:
    """يرسل حدثًا تجريبيًا ويرجع معرّفه."""
    if not _state["on"]:
        return None
    import sentry_sdk
    eid = sentry_sdk.capture_message(f"اختبار ربط Sentry من المنصة ({_state['component']})", level="info")
    sentry_sdk.flush(timeout=5)
    return eid
