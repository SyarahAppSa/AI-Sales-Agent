"""إرسال عبر SMTP (مثل بريد Zoho على الدومين): يكفي لأكواد الدخول. لا مزامنة صندوق ولا تقويم.

الإعداد على الخادم (لا في الكود): SMTP_HOST (مثل smtppro.zoho.com أو smtp.zoho.com)، SMTP_PORT (465)،
SMTP_USER (عنوان البريد)، SMTP_PASSWORD («كلمة مرور التطبيق» من إعدادات Zoho — سرّ يُحفظ في Render فقط).
"""
from __future__ import annotations

import smtplib
import ssl

from ..config import settings
from .base import OutgoingEmail, SendResult, build_mime


class SMTPProvider:
    name = "smtp"

    def __init__(self, sender: str):
        self.sender = sender

    def send(self, msg: OutgoingEmail) -> SendResult:
        mime = build_mime(msg)
        ctx = ssl.create_default_context()
        try:
            if int(settings.smtp_port) == 465:
                srv = smtplib.SMTP_SSL(settings.smtp_host, 465, context=ctx, timeout=30)
            else:
                srv = smtplib.SMTP(settings.smtp_host, int(settings.smtp_port), timeout=30)
                srv.starttls(context=ctx)
            with srv:
                srv.login(settings.smtp_user, settings.smtp_password)
                srv.send_message(mime)
            mid = msg.ensure_message_id()
            return SendResult(accepted=True, provider_message_id=mid, internet_message_id=mid)
        except (smtplib.SMTPException, OSError) as e:
            return SendResult(accepted=False, detail=type(e).__name__)  # بلا تفاصيل قد تحوي بيانات الدخول

    def fetch_new(self, cursor: dict):
        return [], cursor

    def busy_slots(self, user_email, start, end):
        return []

    def create_event(self, organizer, attendee, start, end, subject, body):
        raise RuntimeError("التقويم غير مدعوم عبر SMTP")
