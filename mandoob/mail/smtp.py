"""إرسال عبر SMTP (مثل بريد Zoho على الدومين): يكفي لأكواد الدخول. لا مزامنة صندوق ولا تقويم.

الإعداد على الخادم (لا في الكود): SMTP_HOST (مثل smtppro.zoho.com أو smtp.zoho.com)، SMTP_PORT (465)،
SMTP_USER (عنوان البريد)، SMTP_PASSWORD («كلمة مرور التطبيق» من إعدادات Zoho — سرّ يُحفظ في Render فقط).
"""
from __future__ import annotations

import smtplib
import ssl

from ..config import settings
from .base import OutgoingEmail, SendResult, build_mime


ZOHO_HOSTS = ["smtp.zoho.sa", "smtppro.zoho.sa", "smtppro.zoho.com", "smtp.zoho.com", "smtppro.zoho.eu", "smtp.zoho.eu"]


def _connect(host: str, port: int):
    ctx = ssl.create_default_context()
    if port == 465:
        return smtplib.SMTP_SSL(host, 465, context=ctx, timeout=20)
    srv = smtplib.SMTP(host, port, timeout=20)
    srv.starttls(context=ctx)
    return srv


def check_login(user: str, password: str, host: str | None = None) -> tuple[str | None, str]:
    """يجرّب الدخول (دون إرسال) على الخادم المحدد أو خوادم Zoho المعروفة. يعيد (الخادم الناجح، سبب الفشل)."""
    hosts = [host] if host else ZOHO_HOSTS
    reason = "تعذّر الوصول إلى خادم البريد"
    for h in hosts:
        try:
            with _connect(h, 465) as srv:
                srv.login(user, password)
            return h, ""
        except smtplib.SMTPAuthenticationError:
            reason = "كلمة السر أو البريد غير صحيح (إن كان التحقق بخطوتين مفعّلًا فاستخدم «كلمة مرور التطبيق» من Zoho)"
        except (smtplib.SMTPException, OSError):
            continue
    return None, reason


class SMTPProvider:
    name = "smtp"

    def __init__(self, sender: str, account=None):
        self.sender = sender
        self.account = account

    def _creds(self):
        a = self.account
        if a is not None and a.smtp_password_enc:
            from ..secretbox import unseal
            return a.smtp_host, int(a.smtp_port or 465), a.smtp_user or self.sender, unseal(a.smtp_password_enc)
        return settings.smtp_host, int(settings.smtp_port), settings.smtp_user, settings.smtp_password

    def send(self, msg: OutgoingEmail) -> SendResult:
        mime = build_mime(msg)
        tries = [self._creds()]
        env = (settings.smtp_host, int(settings.smtp_port or 465), settings.smtp_user, settings.smtp_password)
        if settings.smtp_password and settings.smtp_user and env[3] != tries[0][3]:
            tries.append(env)  # احتياطي: كلمة السر في Render إن رُفضت المحفوظة (مثل تغيير كلمة السر في Zoho)
        detail = ""
        for host, port, user, password in tries:
            try:
                srv = _connect(host or "smtp.zoho.com", port)
                with srv:
                    srv.login(user, password)
                    srv.send_message(mime)
                mid = msg.ensure_message_id()
                return SendResult(accepted=True, provider_message_id=mid, internet_message_id=mid)
            except smtplib.SMTPAuthenticationError:
                detail = "auth"  # كلمة السر مرفوضة — يُجرّب الاحتياطي إن وُجد
            except (smtplib.SMTPException, OSError) as e:
                detail = type(e).__name__  # بلا تفاصيل قد تحوي بيانات الدخول
        return SendResult(accepted=False, detail=detail)

    def fetch_new(self, cursor: dict):
        return [], cursor

    def busy_slots(self, user_email, start, end):
        return []

    def create_event(self, organizer, attendee, start, end, subject, body):
        raise RuntimeError("التقويم غير مدعوم عبر SMTP")
