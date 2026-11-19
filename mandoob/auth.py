"""مصادقة بسيطة بكلمات مرور مشفرة (PBKDF2) وجلسات موقعة."""
from __future__ import annotations

import hashlib
import hmac
import os

from sqlalchemy import select

from .db import session_scope
from .models import User

ROLE_LABELS = {"admin": "مدير المنصة", "manager": "مدير المبيعات", "rep": "مندوب"}


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 310_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 310_000)
    return hmac.compare_digest(test.hex(), dk)


def authenticate(email: str, pw: str) -> User | None:
    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == email.strip().lower(), User.active.is_(True)))
        if u and verify_password(pw, u.password_hash):
            return u
    return None


def ensure_admin(email: str, password: str, name: str = "مدير المنصة") -> None:
    with session_scope() as s:
        if s.scalar(select(User.id).where(User.role == "admin")):
            return
        s.add(User(name=name, email=email.lower(), password_hash=hash_password(password), role="admin"))
