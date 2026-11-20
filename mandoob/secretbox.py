"""تشفير الأسرار المحفوظة في قاعدة البيانات (مثل كلمة سر SMTP) بمفتاح مشتق من SECRET_KEY على الخادم.
لا تُعاد القيمة للواجهة أبدًا؛ تُفك فقط لحظة الاتصال بخادم البريد."""
from __future__ import annotations

import base64
import hashlib


def _f():
    from cryptography.fernet import Fernet
    from .config import settings
    key = base64.urlsafe_b64encode(hashlib.sha256(("mandoob-secretbox:" + settings.secret_key).encode()).digest())
    return Fernet(key)


def seal(value: str) -> str:
    return _f().encrypt(value.encode()).decode()


def unseal(token: str | None) -> str:
    if not token:
        return ""
    try:
        return _f().decrypt(token.encode()).decode()
    except Exception:  # noqa: BLE001 — مفتاح الخادم تغيّر: يُطلب إدخالها من جديد
        return ""
