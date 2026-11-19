"""صندوق بريد تجريبي محلي (للاختبار والعرض قبل ربط البريد الحقيقي).

- كل رسالة صادرة تُحفظ كملف .eml كامل (بالمرفق) في data/sandbox/outbox.
- يولد تقرير تسليم فوريًا، أو ارتدادًا إذا كان نطاق المستلم ينتهي بـ bounce.test.
- يمكن حقن رد على أي رسالة عبر inject_reply (من الواجهة أو السكربت).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from pathlib import Path

from ..config import settings
from ..timeutil import now_utc
from .base import InboundEmail, OutgoingEmail, SendResult, build_mime

ROOT = settings.data_dir / "sandbox"


def _paths():
    (ROOT / "outbox").mkdir(parents=True, exist_ok=True)
    return ROOT / "outbox", ROOT / "inbox.json", ROOT / "calendar.json"


def _load(p: Path, default):
    return json.loads(p.read_text("utf-8")) if p.exists() else default


def _save(p: Path, data) -> None:
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), "utf-8")


class SandboxProvider:
    name = "sandbox"

    def __init__(self, sender: str):
        self.sender = sender

    def send(self, msg: OutgoingEmail) -> SendResult:
        outbox, inbox_p, _ = _paths()
        mime = build_mime(msg)
        pid = uuid.uuid4().hex
        (outbox / f"{pid}.eml").write_bytes(mime.as_bytes())
        thread = msg.thread_id or f"thr-{pid[:10]}"
        inbox = _load(inbox_p, [])
        rcpt_domain = msg.to.split("@")[-1].lower()
        report = {
            "provider_id": f"dsn-{pid}", "internet_message_id": f"<dsn-{pid}@sandbox>",
            "from_addr": "postmaster@sandbox.local", "thread_id": thread,
            "received_at": (now_utc() + timedelta(seconds=5)).isoformat(),
            "report_original_message_id": msg.message_id, "report_recipient": msg.to,
        }
        if rcpt_domain.endswith("bounce.test"):
            report.update(subject=f"Undeliverable: {msg.subject}", report_type="bounced",
                          report_reason="550 5.1.1 RESOLVER.ADR.RecipNotFound; عنوان المستلم غير موجود",
                          text="Delivery has failed to these recipients")
        else:
            report.update(subject=f"Delivered: {msg.subject}", report_type="delivered", text="تم التسليم")
        inbox.append(report)
        _save(inbox_p, inbox)
        return SendResult(True, provider_message_id=pid, internet_message_id=msg.message_id, thread_id=thread,
                          detail="قبِل صندوق التجربة طلب الإرسال")

    def fetch_new(self, cursor: dict) -> tuple[list[InboundEmail], dict]:
        _, inbox_p, _ = _paths()
        seen = set(cursor.get("seen", []))
        out = []
        for m in _load(inbox_p, []):
            if m["provider_id"] in seen:
                continue
            received = datetime.fromisoformat(m["received_at"])
            if received > now_utc() + timedelta(seconds=10):
                continue
            out.append(InboundEmail(
                provider_id=m["provider_id"], from_addr=m["from_addr"], subject=m.get("subject", ""),
                text=m.get("text", ""), received_at=received, internet_message_id=m.get("internet_message_id"),
                in_reply_to=m.get("in_reply_to"), references=m.get("references", []), thread_id=m.get("thread_id"),
                report_type=m.get("report_type"), report_reason=m.get("report_reason"),
                report_original_message_id=m.get("report_original_message_id"),
                report_recipient=m.get("report_recipient"), auto_reply=m.get("auto_reply", False)))
            seen.add(m["provider_id"])
        return out, {"seen": sorted(seen)}

    # ---- أدوات التجربة
    @staticmethod
    def inject_reply(*, from_addr: str, original_message_id: str, thread_id: str | None, subject: str,
                     text: str, received_at: datetime | None = None) -> str:
        _, inbox_p, _ = _paths()
        inbox = _load(inbox_p, [])
        pid = f"rep-{uuid.uuid4().hex}"
        inbox.append({"provider_id": pid, "internet_message_id": f"<{pid}@customer.example>",
                      "from_addr": from_addr, "subject": subject if subject.lower().startswith("re")
                      else f"RE: {subject}", "text": text, "in_reply_to": original_message_id,
                      "references": [original_message_id], "thread_id": thread_id,
                      "received_at": (received_at or now_utc()).isoformat()})
        _save(inbox_p, inbox)
        return pid

    @staticmethod
    def read_sent(provider_message_id: str):
        outbox, _, _ = _paths()
        return message_from_bytes((outbox / f"{provider_message_id}.eml").read_bytes(), policy=policy.default)

    def busy_slots(self, user_email, start, end):
        _, _, cal_p = _paths()
        busy = []
        for ev in _load(cal_p, []):
            if ev["owner"] != user_email:
                continue
            s, e = datetime.fromisoformat(ev["start"]), datetime.fromisoformat(ev["end"])
            if s < end and e > start:
                busy.append((s, e))
        return busy

    def create_event(self, organizer, attendee, start, end, subject, body):
        _, _, cal_p = _paths()
        cal = _load(cal_p, [])
        eid = f"evt-{uuid.uuid4().hex[:12]}"
        cal.append({"id": eid, "owner": organizer, "attendee": attendee, "start": start.isoformat(),
                    "end": end.isoformat(), "subject": subject})
        _save(cal_p, cal)
        return eid
