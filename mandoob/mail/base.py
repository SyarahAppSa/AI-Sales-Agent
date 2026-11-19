from __future__ import annotations

import email.utils
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Optional, Protocol


@dataclass
class OutgoingEmail:
    from_addr: str
    from_name: str
    to: str
    subject: str
    text: str
    html: str
    reply_to: str | None = None
    attachment_name: str | None = None
    attachment_bytes: bytes | None = None
    attachment_mime: str = "application/pdf"
    in_reply_to: str | None = None
    references: str | None = None
    thread_id: str | None = None
    message_id: str = field(default_factory=lambda: "")
    headers: dict = field(default_factory=dict)

    def ensure_message_id(self) -> str:
        if not self.message_id:
            domain = self.from_addr.split("@", 1)[1] if "@" in self.from_addr else "mandoob.local"
            self.message_id = f"<{uuid.uuid4().hex}@{domain}>"
        return self.message_id


@dataclass
class SendResult:
    accepted: bool
    provider_message_id: str | None = None
    internet_message_id: str | None = None
    thread_id: str | None = None
    detail: str = ""


@dataclass
class InboundEmail:
    provider_id: str
    from_addr: str
    subject: str
    text: str
    received_at: datetime  # UTC بدون منطقة
    internet_message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    thread_id: str | None = None
    # تقارير التسليم/الارتداد
    report_type: Optional[str] = None  # delivered / bounced / None
    report_reason: str | None = None
    report_original_message_id: str | None = None
    report_recipient: str | None = None
    auto_reply: bool = False


class MailProvider(Protocol):
    name: str

    def send(self, msg: OutgoingEmail) -> SendResult: ...

    def fetch_new(self, cursor: dict) -> tuple[list[InboundEmail], dict]: ...

    def busy_slots(self, user_email: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]: ...

    def create_event(self, organizer: str, attendee: str | None, start: datetime, end: datetime,
                     subject: str, body: str) -> str: ...


def build_mime(msg: OutgoingEmail) -> EmailMessage:
    m = EmailMessage()
    m["From"] = email.utils.formataddr((msg.from_name, msg.from_addr))
    m["To"] = msg.to
    m["Subject"] = msg.subject
    m["Date"] = email.utils.formatdate(localtime=False)
    m["Message-ID"] = msg.ensure_message_id()
    if msg.reply_to:
        m["Reply-To"] = msg.reply_to
    if msg.in_reply_to:
        m["In-Reply-To"] = msg.in_reply_to
        m["References"] = msg.references or msg.in_reply_to
    for k, v in msg.headers.items():
        m[k] = v
    m.set_content(msg.text)
    m.add_alternative(msg.html, subtype="html")
    if msg.attachment_bytes is not None and msg.attachment_name:
        maintype, subtype = msg.attachment_mime.split("/", 1)
        m.add_attachment(msg.attachment_bytes, maintype=maintype, subtype=subtype, filename=msg.attachment_name)
    return m


AUTO_REPLY_MARKERS = ["auto-reply", "automatic reply", "out of office", "رد تلقائي", "خارج المكتب", "autoreply"]
BOUNCE_SENDERS = ["mailer-daemon", "postmaster", "microsoftexchange", "mail delivery subsystem"]
BOUNCE_SUBJECTS = ["undeliverable", "delivery status notification (failure)", "mail delivery failed",
                   "returned mail", "لم يتم التسليم", "تعذر التسليم", "delivery has failed"]
DELIVERED_SUBJECTS = ["delivered:", "تم التسليم:", "delivery receipt", "relayed:"]

_RCPT_RE = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"


def classify_report(from_addr: str, subject: str, text: str) -> tuple[str | None, str | None]:
    """يحدد هل الرسالة تقرير تسليم أو ارتداد، ويستخرج السبب إن وجد."""
    import re
    f, sub = (from_addr or "").lower(), (subject or "").lower()
    if any(b in f for b in BOUNCE_SENDERS) or any(sub.startswith(b) or b in sub for b in BOUNCE_SUBJECTS):
        if any(sub.startswith(d) for d in DELIVERED_SUBJECTS):
            return "delivered", None
        if any(b in sub for b in BOUNCE_SUBJECTS) or "failed" in text.lower()[:2000] or "550" in text[:4000]:
            m = re.search(r"((?:5\.\d\.\d+|55\d)[^\n]{0,200})", text)
            reason = m.group(1).strip() if m else text.strip().splitlines()[0][:200] if text.strip() else None
            return "bounced", reason
    if any(sub.startswith(d) for d in DELIVERED_SUBJECTS):
        return "delivered", None
    return None, None


def is_auto_reply(subject: str, headers: dict | None = None) -> bool:
    sub = (subject or "").lower()
    h = {k.lower(): str(v).lower() for k, v in (headers or {}).items()}
    if h.get("auto-submitted", "no") not in ("no", "") or "x-autoreply" in h or h.get("precedence") == "auto_reply":
        return True
    return any(m in sub for m in AUTO_REPLY_MARKERS)
