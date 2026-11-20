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


def provider_configured(acct) -> bool:
    if acct.provider == "microsoft365":
        return bool(settings.ms_tenant_id and settings.ms_client_id and settings.ms_client_secret)
    if acct.provider == "google":
        return bool(settings.google_client_id and settings.google_client_secret and settings.google_refresh_token)
    if acct.provider == "smtp":
        return bool(getattr(acct, "smtp_password_enc", None) and acct.smtp_host) or \
            bool(settings.smtp_host and settings.smtp_user and settings.smtp_password)
    return True


def mail_ready(s) -> bool:
    """البريد جاهز لإرسال أكواد الدخول فقط إذا كان مزوده مُعدًّا على الخادم ونجح إرسال فعلي منه.
    قبل ذلك يبقى الدخول التأسيسي بكلمة المرور متاحًا حتى لا تُغلق المنصة على الإدارة."""
    acct = s.scalar(select(MailAccount).where(MailAccount.active.is_(True)).order_by(MailAccount.id).limit(1))
    if acct is None or not provider_configured(acct):
        return False
    return acct.provider == "sandbox" or acct.verified_at is not None


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
    last = s.scalar(select(LoginCode.created_at).where(LoginCode.user_id == user.id, LoginCode.used.is_(False))
                    .order_by(LoginCode.id.desc()).limit(1))
    if last is not None and (now_utc() - last).total_seconds() < RESEND_AFTER_SECONDS:
        raise OTPError("أُرسل كود قبل أقل من دقيقة. انتظر قليلًا ثم اضغط «إعادة إرسال الكود».")
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


LOGIN_SUBJECT = "رمز الدخول إلى المندوب الذكي لسيارة اب"
RESEND_AFTER_SECONDS = 60


LOGIN_SUBJECT_EN = "Sign-in code for Syarah App Smart Sales Agent"


def login_email(user: User, code: str, portal: str, lang: str | None = None) -> tuple[str, str, str]:
    """(العنوان، النص، HTML) لرسالة كود الدخول: رسالة معاملات بسيطة — الرمز ومدة صلاحيته فقط،
    بلا روابط ولا صور ولا مرفقات (أقل احتمالًا للتصنيف كرسالة مزعجة)."""
    from html import escape
    from .i18n import current_lang
    if (lang or current_lang()) == "en":
        where = "the Admin portal" if portal == "admin" else "the Staff portal"
        text = (f"Hello {user.name},\n\nYour sign-in code for {where}:\n\n    {code}\n\n"
                f"Valid for 5 minutes, one use only. Any previous code no longer works.\n"
                f"If you did not request it, ignore this email and do not share it.\n\nSyarah App Smart Sales Agent")
        html = (f"<div dir='ltr' style='font-family:Arial,sans-serif;max-width:480px;margin:auto;color:#292431'>"
                f"<p>Hello {escape(user.name)},</p><p>Your sign-in code for {where}:</p>"
                f"<p style='font-size:40px;font-weight:bold;letter-spacing:10px;text-align:center;direction:ltr'>{code}</p>"
                f"<p><b>Valid for 5 minutes, one use only.</b> Any previous code no longer works.</p>"
                f"<p style='color:#6F6879;font-size:13px'>If you did not request it, ignore this email and do not share it.</p>"
                f"<p style='color:#6F6879;font-size:13px'>Syarah App Smart Sales Agent</p></div>")
        return LOGIN_SUBJECT_EN, text, html
    where = "بوابة الإدارة" if portal == "admin" else "بوابة الموظفين"
    text = (f"مرحبًا {user.name}،\n\nرمز الدخول إلى {where}:\n\n    {code}\n\n"
            f"صالح لمدة ٥ دقائق ولمرة واحدة فقط. أي رمز سابق لم يعد صالحًا.\n"
            f"إن لم تطلب هذا الرمز فتجاهل الرسالة ولا تشاركه مع أحد.\n\nالمندوب الذكي لسيارة اب")
    html = (f"<div dir='rtl' style='font-family:Tahoma,Arial,sans-serif;max-width:480px;margin:auto;color:#292431'>"
            f"<p>مرحبًا {escape(user.name)}،</p><p>رمز الدخول إلى {where}:</p>"
            f"<p style='font-size:40px;font-weight:bold;letter-spacing:10px;text-align:center;direction:ltr'>{code}</p>"
            f"<p><b>صالح لمدة ٥ دقائق ولمرة واحدة فقط.</b> أي رمز سابق لم يعد صالحًا.</p>"
            f"<p style='color:#6F6879;font-size:13px'>إن لم تطلب هذا الرمز فتجاهل الرسالة ولا تشاركه مع أحد.</p>"
            f"<p style='color:#6F6879;font-size:13px'>المندوب الذكي لسيارة اب</p></div>")
    return LOGIN_SUBJECT, text, html


def send_code_email(s, user: User, code: str, portal: str) -> bool:
    """يرسل الكود من بريد الشركة المربوط بالمنصة إلى بريد الموظف — مسار أكواد الدخول منفصل عن العروض التسويقية
    (المعطّلة) ولا يمر بطابور الإرسال."""
    from .mail import get_provider
    from .mail.base import OutgoingEmail
    from .outreach import active_account
    acct = active_account(s)
    if acct is None:
        return False
    subject, text, html = login_email(user, code, portal)
    res = get_provider(acct).send(OutgoingEmail(acct.sender_address, "المندوب الذكي لسيارة اب", user.email,
                                                subject, text, html,
                                                headers={"Auto-Submitted": "auto-generated", "X-Priority": "1",
                                                         "Importance": "high", "X-Auto-Response-Suppress": "All"}))
    return bool(res and res.accepted)


def send_invite_email(s, user: User) -> bool:
    from .mail import get_provider
    from .mail.base import OutgoingEmail
    from .outreach import active_account
    acct = active_account(s)
    if acct is None:
        return False
    link = settings.public_base_url.rstrip("/") + "/portal/login"
    text = (f"مرحبًا {user.name}،\n\nأُضيف حسابك إلى بوابة الموظفين في «المندوب الذكي لسيارة اب».\n"
            f"ادخل من الرابط التالي ببريدك الوظيفي، وسيصلك كود دخول على هذا البريد — بلا كلمة مرور:\n{link}")
    html = (f"<div dir='rtl' style='font-family:Tahoma'><p>مرحبًا {user.name}،</p><p>أُضيف حسابك إلى بوابة الموظفين في "
            f"«المندوب الذكي لسيارة اب».</p><p><a href='{link}'>الدخول إلى البوابة</a></p>"
            "<p>ادخل ببريدك الوظيفي، وسيصلك رمز دخول على هذا البريد.</p></div>")
    res = get_provider(acct).send(OutgoingEmail(acct.sender_address, "المندوب الذكي لسيارة اب", user.email,
                                                "دعوة إلى بوابة الموظفين — المندوب الذكي لسيارة اب", text, html))
    return bool(res and res.accepted)
