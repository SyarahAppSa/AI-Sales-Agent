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


# ---------------------------------------------------------------- الدخول برمز يصل إلى البريد

import secrets
from datetime import timedelta

from sqlalchemy import func

from .config import settings
from .models import AuditLog, LoginCode, MailAccount
from .timeutil import now_utc

CODE_TTL = timedelta(minutes=5)
MAX_ATTEMPTS_PER_CODE = 5
MAX_CODES_PER_15MIN = 3          # لكل بريد
MAX_CODES_PER_IP_15MIN = int(__import__('os').environ.get('OTP_IP_LIMIT', '10'))
MAX_FAILED_PER_HOUR = 10         # بعدها يُقفل الحساب ساعة


def _hash(code: str, user_id: int) -> str:
    return hashlib.sha256(f"{settings.secret_key}:{user_id}:{code}".encode()).hexdigest()


def portal_of(user: User) -> str:
    from .permissions import is_admin
    return "admin" if is_admin(user) else "staff"


def mail_ready(s) -> bool:
    return s.scalar(select(MailAccount.id).where(MailAccount.active.is_(True)).limit(1)) is not None


def audit(s, actor, action: str, target=None, **details) -> None:
    s.add(AuditLog(actor_id=getattr(actor, "id", None), actor_name=getattr(actor, "name", "") or "النظام",
                   action=action, target_user_id=getattr(target, "id", None),
                   target_name=getattr(target, "name", "") or "", details=details))


class OTPError(Exception):
    pass


def account_problem(s, email: str, portal: str) -> str | None:
    """يتحقق أن البريد (اسم المستخدم) مسجّل ومفعّل ولهذه البوابة. يعيد رسالة المشكلة أو None."""
    email = (email or "").strip().lower()
    user = s.scalar(select(User).where(User.email == email))
    if user is None:
        return "هذا البريد غير مسجّل في المنصة. تواصل مع الإدارة لإضافتك."
    if not user.active:
        return "هذا الحساب موقوف. تواصل مع الإدارة."
    if portal_of(user) != portal:
        return ("هذا البريد لحساب إدارة — ادخل من بوابة الإدارة." if portal == "staff"
                else "هذا البريد لحساب موظف — ادخل من بوابة الموظفين.")
    return None


def request_code(s, email: str, portal: str, ip_hash: str | None = None) -> tuple[User | None, str | None]:
    """يُنشئ رمزًا جديدًا (ويلغي السابق). يعيد (المستخدم، الرمز) أو (None، None) إن لم يكن الحساب مؤهلًا.
    الواجهة تعرض رسالة عامة واحدة في الحالتين حتى لا يُكشف وجود الحساب."""
    email = (email or "").strip().lower()
    since = now_utc() - timedelta(minutes=15)
    if ip_hash and (s.scalar(select(func.count(LoginCode.id)).where(LoginCode.ip_hash == ip_hash,
                                                                     LoginCode.created_at >= since)) or 0) >= MAX_CODES_PER_IP_15MIN:
        raise OTPError("طلبات كثيرة من هذا الجهاز. حاول بعد ١٥ دقيقة.")
    user = s.scalar(select(User).where(User.email == email))
    if user is None or not user.active or portal_of(user) != portal:
        return None, None
    # الرموز التي استُخدمت للدخول بنجاح لا تُحسب ضمن حد إعادة الإرسال
    recent = s.scalar(select(func.count(LoginCode.id)).where(LoginCode.user_id == user.id, LoginCode.used.is_(False),
                                                             LoginCode.created_at >= since)) or 0
    if recent >= MAX_CODES_PER_15MIN:
        raise OTPError("طلبت رموزًا كثيرة. انتظر ١٥ دقيقة ثم حاول مجددًا.")
    for old in s.scalars(select(LoginCode).where(LoginCode.user_id == user.id, LoginCode.used.is_(False),
                                                 LoginCode.revoked.is_(False))):
        old.revoked = True  # طلب رمز جديد يلغي السابق
    code = f"{secrets.randbelow(1_000_000):06d}"
    s.add(LoginCode(user_id=user.id, portal=portal, code_hash=_hash(code, user.id),
                    expires_at=now_utc() + CODE_TTL, ip_hash=ip_hash))
    s.flush()
    return user, code


def verify_code(s, email: str, code: str, portal: str) -> User:
    email = (email or "").strip().lower()
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    user = s.scalar(select(User).where(User.email == email))
    generic = OTPError("الرمز غير صحيح أو منتهي الصلاحية. اطلب رمزًا جديدًا.")
    if user is None or not user.active or portal_of(user) != portal:
        raise generic
    hour = now_utc() - timedelta(hours=1)
    failed = s.scalar(select(func.coalesce(func.sum(LoginCode.attempts), 0)).where(
        LoginCode.user_id == user.id, LoginCode.created_at >= hour, LoginCode.used.is_(False))) or 0
    if failed >= MAX_FAILED_PER_HOUR:
        raise OTPError("محاولات خاطئة كثيرة. أُقفل الدخول لهذا الحساب ساعة.")
    lc = s.scalar(select(LoginCode).where(LoginCode.user_id == user.id, LoginCode.used.is_(False),
                                          LoginCode.revoked.is_(False), LoginCode.portal == portal)
                  .order_by(LoginCode.id.desc()))
    if lc is None or lc.expires_at < now_utc() or lc.attempts >= MAX_ATTEMPTS_PER_CODE:
        raise generic
    if not hmac.compare_digest(lc.code_hash, _hash(code, user.id)):
        lc.attempts += 1
        if lc.attempts >= MAX_ATTEMPTS_PER_CODE:
            lc.revoked = True
        raise generic
    lc.used = True
    user.last_login_at = now_utc()
    audit(s, user, "login", user, portal=portal)
    return user


def send_code_email(s, user: User, code: str, portal: str) -> bool:
    from .mail import get_provider
    from .mail.base import OutgoingEmail
    from .outreach import active_account
    acct = active_account(s)
    if acct is None:
        return False
    link = settings.public_base_url.rstrip("/") + ("/admin/login" if portal == "admin" else "/portal/login")
    text = (f"مرحبًا {user.name}،\n\nكود الدخول إلى {'بوابة الإدارة' if portal == 'admin' else 'بوابة الموظفين'} "
            f"في مندوب سيارة آب الذكي:\n\n{code}\n\nصالح لمدة ٥ دقائق ولمرة واحدة. إن لم تطلبه فتجاهل هذه الرسالة.\n{link}")
    html = (f"<div dir='rtl' style='font-family:Tahoma'><p>مرحبًا {user.name}،</p><p>كود الدخول:</p>"
            f"<p style='font-size:28px;font-weight:bold;letter-spacing:6px'>{code}</p>"
            "<p>صالح لمدة ٥ دقائق ولمرة واحدة. إن لم تطلبه فتجاهل هذه الرسالة.</p></div>")
    res = get_provider(acct).send(OutgoingEmail(acct.sender_address, "مندوب سيارة آب الذكي", user.email,
                                                f"كود الدخول: {code}", text, html))
    return bool(res and res.accepted)


def send_invite_email(s, user: User) -> bool:
    from .mail import get_provider
    from .mail.base import OutgoingEmail
    from .outreach import active_account
    acct = active_account(s)
    if acct is None:
        return False
    link = settings.public_base_url.rstrip("/") + "/portal/login"
    text = (f"مرحبًا {user.name}،\n\nأُضيف حسابك إلى بوابة الموظفين في «مندوب سيارة آب الذكي».\n"
            f"ادخل من الرابط التالي ببريدك الوظيفي، وسيصلك كود دخول على هذا البريد — بلا كلمة مرور:\n{link}")
    html = (f"<div dir='rtl' style='font-family:Tahoma'><p>مرحبًا {user.name}،</p><p>أُضيف حسابك إلى بوابة الموظفين في "
            f"«مندوب سيارة آب الذكي».</p><p><a href='{link}'>الدخول إلى البوابة</a></p>"
            "<p>ادخل ببريدك الوظيفي، وسيصلك رمز دخول على هذا البريد.</p></div>")
    res = get_provider(acct).send(OutgoingEmail(acct.sender_address, "مندوب سيارة آب الذكي", user.email,
                                                "دعوة إلى بوابة الموظفين — مندوب سيارة آب", text, html))
    return bool(res and res.accepted)
