"""Google Workspace عبر Gmail API و Calendar API (OAuth برمز تحديث لحساب المرسل).

النطاقات المطلوبة: gmail.send, gmail.readonly (أو gmail.modify), calendar.
ملاحظة: Gmail لا يعيد تأكيد تسليم من خادم المستلم؛ لذلك تبقى الحالة «أُرسل» ما لم يصل ارتداد.
"""
from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timezone
from email import message_from_bytes, policy

import httpx

from ..config import settings
from .base import InboundEmail, OutgoingEmail, SendResult, build_mime, classify_report, is_auto_reply

API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailProvider:
    name = "google"

    def __init__(self, sender: str, http: httpx.Client | None = None):
        self.sender = sender
        self.http = http or httpx.Client(timeout=60)
        self._token: tuple[str, float] | None = None

    def _auth(self) -> dict:
        if not (settings.google_client_id and settings.google_client_secret and settings.google_refresh_token):
            raise RuntimeError("إعدادات Google Workspace غير مكتملة")
        if not self._token or self._token[1] < time.time() + 60:
            r = self.http.post("https://oauth2.googleapis.com/token", data={
                "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                "refresh_token": settings.google_refresh_token, "grant_type": "refresh_token"})
            r.raise_for_status()
            d = r.json()
            self._token = (d["access_token"], time.time() + int(d.get("expires_in", 3600)))
        return {"Authorization": f"Bearer {self._token[0]}"}

    def send(self, msg: OutgoingEmail) -> SendResult:
        mime = build_mime(msg)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        body = {"raw": raw}
        if msg.thread_id:
            body["threadId"] = msg.thread_id
        r = self.http.post(f"{API}/messages/send", json=body, headers=self._auth())
        if r.status_code >= 300:
            return SendResult(False, detail=f"Gmail {r.status_code}: {r.text[:300]}")
        d = r.json()
        # قراءة Message-ID الفعلي بعد الإرسال
        mid = msg.message_id
        try:
            r2 = self.http.get(f"{API}/messages/{d['id']}", params={"format": "metadata",
                                                                     "metadataHeaders": "Message-ID"},
                               headers=self._auth())
            for h in r2.json().get("payload", {}).get("headers", []):
                if h["name"].lower() == "message-id":
                    mid = h["value"]
        except httpx.HTTPError:
            pass
        return SendResult(True, provider_message_id=d["id"], internet_message_id=mid, thread_id=d.get("threadId"),
                          detail="قبِل Gmail طلب الإرسال")

    def fetch_new(self, cursor: dict) -> tuple[list[InboundEmail], dict]:
        after = int(cursor.get("after_epoch") or (time.time() - 2 * 86400))
        seen = set(cursor.get("seen_ids", []))
        r = self.http.get(f"{API}/messages", params={"q": f"in:inbox after:{after}", "maxResults": 100},
                          headers=self._auth())
        r.raise_for_status()
        out = []
        newest = after
        for ref in r.json().get("messages", []):
            if ref["id"] in seen:
                continue
            rr = self.http.get(f"{API}/messages/{ref['id']}", params={"format": "raw"}, headers=self._auth())
            rr.raise_for_status()
            d = rr.json()
            em = message_from_bytes(base64.urlsafe_b64decode(d["raw"]), policy=policy.default)
            text = _text_of(em)
            frm = str(em.get("From", ""))
            m = re.search(r"[\w.+\-]+@[\w.\-]+", frm)
            frm_addr = m.group(0) if m else frm
            rtype, reason = classify_report(frm_addr, str(em.get("Subject", "")), text)
            orig = None
            if rtype:
                mm = re.search(r"Message-ID:\s*(<[^>]+>)", text, re.I)
                orig = mm.group(1) if mm else None
            ts = int(d.get("internalDate", "0")) // 1000
            newest = max(newest, ts)
            headers = {k: v for k, v in em.items()}
            out.append(InboundEmail(
                provider_id=d["id"], from_addr=frm_addr, subject=str(em.get("Subject", "")), text=text,
                received_at=datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None),
                internet_message_id=em.get("Message-ID"), in_reply_to=em.get("In-Reply-To"),
                references=str(em.get("References", "")).split(), thread_id=d.get("threadId"),
                report_type=rtype, report_reason=reason or (em.get("X-Failed-Recipients") and "عنوان غير موجود"),
                report_original_message_id=orig, auto_reply=is_auto_reply(str(em.get("Subject", "")), headers)))
            seen.add(d["id"])
        return out, {"after_epoch": newest - 60, "seen_ids": list(seen)[-500:]}

    def busy_slots(self, user_email, start, end):
        r = self.http.post("https://www.googleapis.com/calendar/v3/freeBusy", headers=self._auth(), json={
            "timeMin": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeMax": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "items": [{"id": user_email}]})
        r.raise_for_status()
        busy = r.json().get("calendars", {}).get(user_email, {}).get("busy", [])
        return [(datetime.fromisoformat(b["start"][:19]), datetime.fromisoformat(b["end"][:19])) for b in busy]

    def create_event(self, organizer, attendee, start, end, subject, body):
        r = self.http.post(f"https://www.googleapis.com/calendar/v3/calendars/{organizer}/events",
                           params={"sendUpdates": "all"}, headers=self._auth(), json={
                               "summary": subject, "description": re.sub("<[^>]+>", "", body),
                               "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ")},
                               "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ")},
                               "attendees": [{"email": attendee}] if attendee else []})
        r.raise_for_status()
        return r.json()["id"]


def _text_of(em) -> str:
    if em.is_multipart():
        for part in em.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    return part.get_content()
                except Exception:
                    continue
        for part in em.walk():
            if part.get_content_type() == "text/html":
                return re.sub(r"<[^>]+>", " ", part.get_content())
        return ""
    try:
        return em.get_content()
    except Exception:
        return ""
