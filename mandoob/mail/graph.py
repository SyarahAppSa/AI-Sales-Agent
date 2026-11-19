"""Microsoft 365 عبر Microsoft Graph (تطبيق Entra ID بصلاحيات تطبيق).

الصلاحيات المطلوبة (Application): Mail.Send, Mail.ReadWrite, Calendars.ReadWrite.
يُنصح بتقييد التطبيق على صندوق المرسل فقط عبر Application Access Policy أو RBAC for Applications.
"""
from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timedelta

import httpx

from ..config import settings
from .base import InboundEmail, OutgoingEmail, SendResult, classify_report, is_auto_reply

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphProvider:
    name = "microsoft365"

    def __init__(self, sender: str, reply_mailbox: str | None = None, http: httpx.Client | None = None):
        self.sender = sender
        self.mailbox = reply_mailbox or sender
        self.http = http or httpx.Client(timeout=60)
        self._token: tuple[str, float] | None = None

    def _auth(self) -> dict:
        if not (settings.ms_tenant_id and settings.ms_client_id and settings.ms_client_secret):
            raise RuntimeError("إعدادات Microsoft 365 غير مكتملة (MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET)")
        if not self._token or self._token[1] < time.time() + 60:
            r = self.http.post(f"https://login.microsoftonline.com/{settings.ms_tenant_id}/oauth2/v2.0/token", data={
                "client_id": settings.ms_client_id, "client_secret": settings.ms_client_secret,
                "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"})
            r.raise_for_status()
            d = r.json()
            self._token = (d["access_token"], time.time() + int(d.get("expires_in", 3600)))
        return {"Authorization": f"Bearer {self._token[0]}"}

    def send(self, msg: OutgoingEmail) -> SendResult:
        payload = {
            "subject": msg.subject,
            "body": {"contentType": "HTML", "content": msg.html},
            "toRecipients": [{"emailAddress": {"address": msg.to}}],
            "from": {"emailAddress": {"address": msg.from_addr, "name": msg.from_name}},
            "isDeliveryReceiptRequested": True,
            "internetMessageHeaders": [{"name": k, "value": str(v)} for k, v in msg.headers.items()
                                       if k.lower().startswith("x-")],
        }
        if msg.reply_to:
            payload["replyTo"] = [{"emailAddress": {"address": msg.reply_to}}]
        if msg.attachment_bytes is not None:
            payload["attachments"] = [{
                "@odata.type": "#microsoft.graph.fileAttachment", "name": msg.attachment_name,
                "contentType": msg.attachment_mime,
                "contentBytes": base64.b64encode(msg.attachment_bytes).decode()}]
        # 1) إنشاء مسودة للحصول على المعرفات، 2) الإرسال
        r = self.http.post(f"{GRAPH}/users/{self.sender}/messages", json=payload, headers=self._auth())
        if r.status_code >= 300:
            return SendResult(False, detail=f"Graph create {r.status_code}: {r.text[:300]}")
        draft = r.json()
        r2 = self.http.post(f"{GRAPH}/users/{self.sender}/messages/{draft['id']}/send", headers=self._auth())
        if r2.status_code != 202:
            return SendResult(False, detail=f"Graph send {r2.status_code}: {r2.text[:300]}")
        return SendResult(True, provider_message_id=draft["id"], internet_message_id=draft.get("internetMessageId"),
                          thread_id=draft.get("conversationId"), detail="قبِل Microsoft 365 طلب الإرسال (202)")

    def fetch_new(self, cursor: dict) -> tuple[list[InboundEmail], dict]:
        since = cursor.get("since") or (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (f"{GRAPH}/users/{self.mailbox}/mailFolders/inbox/messages"
               f"?$filter=receivedDateTime ge {since}&$orderby=receivedDateTime asc&$top=50"
               "&$select=id,internetMessageId,conversationId,from,subject,body,receivedDateTime,internetMessageHeaders")
        out: list[InboundEmail] = []
        last = since
        while url:
            r = self.http.get(url, headers={**self._auth(), "Prefer": 'outlook.body-content-type="text"'})
            r.raise_for_status()
            d = r.json()
            for m in d.get("value", []):
                headers = {h["name"]: h["value"] for h in (m.get("internetMessageHeaders") or [])}
                frm = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
                text = (m.get("body") or {}).get("content", "")
                rtype, reason = classify_report(frm, m.get("subject", ""), text)
                orig = None
                if rtype:
                    mm = re.search(r"<[^<>@\s]+@[^<>\s]+>", text)
                    orig = mm.group(0) if mm else None
                received = datetime.strptime(m["receivedDateTime"][:19], "%Y-%m-%dT%H:%M:%S")
                out.append(InboundEmail(
                    provider_id=m["id"], from_addr=frm, subject=m.get("subject", ""), text=text,
                    received_at=received, internet_message_id=m.get("internetMessageId"),
                    in_reply_to=headers.get("In-Reply-To"),
                    references=(headers.get("References") or "").split(), thread_id=m.get("conversationId"),
                    report_type=rtype, report_reason=reason, report_original_message_id=orig,
                    auto_reply=is_auto_reply(m.get("subject", ""), headers)))
                last = m["receivedDateTime"]
            url = d.get("@odata.nextLink")
        return out, {"since": last}

    def busy_slots(self, user_email, start, end):
        body = {"schedules": [user_email], "availabilityViewInterval": 30,
                "startTime": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
                "endTime": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}}
        r = self.http.post(f"{GRAPH}/users/{user_email}/calendar/getSchedule", json=body,
                           headers={**self._auth(), "Prefer": 'outlook.timezone="UTC"'})
        r.raise_for_status()
        slots = []
        for sch in r.json().get("value", []):
            for it in sch.get("scheduleItems", []):
                if it.get("status") in ("busy", "oof", "tentative"):
                    slots.append((datetime.fromisoformat(it["start"]["dateTime"][:19]),
                                  datetime.fromisoformat(it["end"]["dateTime"][:19])))
        return slots

    def create_event(self, organizer, attendee, start, end, subject, body):
        ev = {"subject": subject, "body": {"contentType": "HTML", "content": body},
              "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
              "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
              "attendees": ([{"emailAddress": {"address": attendee}, "type": "required"}] if attendee else []),
              "isOnlineMeeting": True}
        r = self.http.post(f"{GRAPH}/users/{organizer}/events", json=ev, headers=self._auth())
        r.raise_for_status()
        return r.json()["id"]
