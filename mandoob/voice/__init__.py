"""قناة الاتصال الصوتي: مساعد مبيعات آلي بلهجة سعودية عبر ElevenLabs Conversational AI
(مربوط برقم الشركة عبر Twilio أو SIP trunk لمزود سعودي).

الضوابط قبل أي مكالمة:
- القناة مفعلة من الإعدادات.
- يوجد سجل موافقة صريحة (explicit) للاتصال لهذه الشركة.
- الرقم غير موجود في قائمة إيقاف التواصل، والشركة لم تطلب الإيقاف.
- ضمن أيام وساعات العمل بتوقيت الرياض.
المساعد يعرّف بنفسه أنه «المساعد الآلي لشركة سيارة آب» ويستأذن قبل المتابعة.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..companies import log_activity
from ..config import settings
from ..costs import record_cost
from ..models import Call, Company, Consent, Setting, Task
from ..outreach import is_suppressed
from ..timeutil import now_riyadh, now_utc, riyadh_to_utc

AGENT_PROMPT = """أنت «المساعد الآلي لشركة سيارة آب»، تتحدث بلهجة سعودية واضحة ومهذبة وطبيعية.
ابدأ دائمًا بـ: «السلام عليكم، معك المساعد الآلي من شركة سيارة آب. أتصل بخصوص {{service_name}} لشركة {{company_name}}. عندك دقيقتين؟»
- إذا لم يأذن العميل أو كان مشغولًا: اعتذر واسأل عن وقت مناسب، ثم أنهِ المكالمة.
- إذا أذن: اعرض الخدمة المعتمدة فقط بنقاطها التالية دون أي ادعاءات أو أسعار غير مذكورة: {{offer_points}}
- اقترح موعدًا تعريفيًا لمدة عشر دقائق مع {{rep_name}}، أو حوّل المكالمة إلى المندوب إذا طلب العميل التحدث مع شخص.
- إذا طلب العميل عدم الاتصال مجددًا: أكد له أنه لن يتم الاتصال به، وسجّل wants_no_contact = true.
- لا تجمع أي بيانات شخصية غير لازمة. لا تدّعِ أنك إنسان.
سجّل في نهاية المكالمة: outcome (interested / meeting_booked / transfer_to_rep / callback / not_interested / do_not_call / voicemail)، next_step، callback_time_riyadh (YYYY-MM-DDTHH:MM إن وُجد)، wants_no_contact."""


def voice_enabled(s: Session) -> bool:
    row = s.get(Setting, "voice")
    return bool(row and row.value.get("enabled"))


def check_call_eligibility(s: Session, company: Company) -> tuple[bool, str]:
    if not settings.voice_calls_enabled:
        return False, "الاتصال الآلي معطّل في هذه المرحلة (الفريق يتواصل بنفسه)"
    if company.is_test:
        return False, "بيانات تجريبية — لا يُتصل بها"
    if not voice_enabled(s):
        return False, "قناة الاتصال الصوتي غير مفعلة"
    if company.do_not_contact:
        return False, "طلبت الشركة إيقاف التواصل"
    phone = company.primary("phone")
    if not phone:
        return False, "لا يوجد رقم موثّق"
    consent = s.scalar(select(Consent).where(Consent.company_id == company.id, Consent.channel == "call")
                       .order_by(Consent.id.desc()))
    if not consent or not consent.granted or consent.basis != "explicit":
        return False, "لا توجد موافقة صريحة مسجلة على الاتصال الآلي"
    why = is_suppressed(s, company, None, "call")
    if why:
        return False, why
    from ..models import Suppression
    if s.scalar(select(Suppression.id).where(Suppression.kind == "phone", Suppression.value == phone.value)):
        return False, "الرقم ضمن قائمة إيقاف التواصل"
    now = now_riyadh()
    if now.weekday() in (4, 5) or not (9 <= now.hour < 17):
        return False, "خارج أوقات الاتصال المسموحة (الأحد–الخميس 9ص–5م)"
    return True, ""


def start_call(s: Session, company: Company, *, service_name: str, offer_points: str, rep_name: str,
               http: httpx.Client | None = None) -> Call:
    if not settings.voice_calls_enabled:
        raise PermissionError("الاتصال الآلي معطّل في هذه المرحلة")
    ok, why = check_call_eligibility(s, company)
    if not ok:
        raise PermissionError(why)
    if not (settings.elevenlabs_api_key and settings.elevenlabs_agent_id and settings.elevenlabs_phone_number_id):
        raise RuntimeError("إعدادات مزود المكالمات غير مكتملة")
    phone = company.primary("phone").value
    call = Call(company_id=company.id, to_number=phone)
    s.add(call)
    s.flush()
    http = http or httpx.Client(timeout=30)
    r = http.post("https://api.elevenlabs.io/v1/convai/twilio/outbound-call",
                  headers={"xi-api-key": settings.elevenlabs_api_key},
                  json={"agent_id": settings.elevenlabs_agent_id,
                        "agent_phone_number_id": settings.elevenlabs_phone_number_id,
                        "to_number": phone,
                        "conversation_initiation_client_data": {"dynamic_variables": {
                            "company_name": company.name, "service_name": service_name,
                            "offer_points": offer_points, "rep_name": rep_name, "call_ref": str(call.id)}}})
    if r.status_code >= 300:
        call.status = "failed"
        call.summary = f"تعذر بدء المكالمة: {r.status_code}"
    else:
        d = r.json()
        call.provider_call_id = d.get("conversation_id") or d.get("callSid")
        call.status = "ringing"
    log_activity(s, company.id, "call", "بدأت مكالمة المساعد الصوتي", f"الرقم: {phone}", ref_type="call",
                 ref_id=call.id)
    return call


def verify_webhook(body: bytes, signature_header: str | None, secret: str, tolerance: int = 1800) -> bool:
    """تحقق توقيع ElevenLabs: الترويسة بصيغة t=<timestamp>,v0=<hmac_sha256(t.body)>."""
    if not secret or not signature_header:
        return False
    parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
    t, sig = parts.get("t"), parts.get("v0")
    if not t or not sig or abs(time.time() - int(t)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def handle_post_call(s: Session, payload: dict) -> Call | None:
    from ..inbox import apply_opt_out, notify
    data = payload.get("data", payload)
    conv_id = data.get("conversation_id")
    call = s.scalar(select(Call).where(Call.provider_call_id == conv_id)) if conv_id else None
    if call is None:
        ref = (((data.get("conversation_initiation_client_data") or {}).get("dynamic_variables")) or {}).get("call_ref")
        call = s.get(Call, int(ref)) if ref and str(ref).isdigit() else None
    if call is None:
        return None
    analysis = data.get("analysis") or {}
    dc = {k: (v.get("value") if isinstance(v, dict) else v)
          for k, v in (analysis.get("data_collection_results") or {}).items()}
    meta = data.get("metadata") or {}
    call.duration_sec = int(meta.get("call_duration_secs") or 0)
    call.answered = call.duration_sec > 5
    call.status = "completed" if call.answered else "no_answer"
    call.summary = analysis.get("transcript_summary") or ""
    call.transcript = json.dumps(data.get("transcript") or [], ensure_ascii=False)[:50000]
    call.outcome = dc.get("outcome") or ("voicemail" if not call.answered else None)
    call.next_step = dc.get("next_step")
    call.ended_at = now_utc()
    record_cost("voice_minute", max(call.duration_sec, 1) / 60, session=s)
    company = call.company
    log_activity(s, company.id, "call_result", f"نتيجة المكالمة: {call.outcome or call.status}",
                 f"المدة: {call.duration_sec} ث\nالملخص: {call.summary}\nالخطوة التالية: {call.next_step or '—'}",
                 ref_type="call", ref_id=call.id)
    if dc.get("wants_no_contact") in (True, "true", "True") or call.outcome == "do_not_call":
        apply_opt_out(s, company, source="voice_call", detail=call.summary or "", channel="all")
        return call
    if call.outcome in ("interested", "meeting_booked", "transfer_to_rep", "callback"):
        due = now_utc() + timedelta(hours=2)
        if dc.get("callback_time_riyadh"):
            try:
                due = riyadh_to_utc(datetime.fromisoformat(str(dc["callback_time_riyadh"])[:16]))
            except ValueError:
                pass
        call.next_step_at = due
        t = Task(company_id=company.id, owner_id=company.owner_id, kind="call",
                 title=f"متابعة مكالمة المساعد — {company.name}", details=call.summary or "", due_at=due,
                 source_call_id=call.id)
        s.add(t)
        company.stage = "interested"
        company.next_action, company.next_action_at = call.next_step or "الاتصال بالعميل", due
        if company.owner:
            notify(s, company.owner, company, f"نتيجة مكالمة: {company.name}", call.summary or "")
    elif call.outcome == "not_interested":
        company.stage = "lost"
    return call
