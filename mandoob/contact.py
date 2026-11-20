"""حالة التواصل اليدوي ومنع تكرار التواصل بين الموظفين.

- «بدء التواصل» حجز ذري على سجل المنشأة: موظف واحد فقط ينجح إن ضغط اثنان في اللحظة نفسها.
- «لم يرد» محاولة تواصل؛ وآخر محاولة اليوم بواسطة موظف آخر تمنع إعادة الاتصال في اليوم نفسه بالخطأ.
- كل تحديث يُحفظ في السجل (ContactLog) باسم الموظف ووقته، ولا يُمسح ما قبله.
- «طلب عدم التواصل» يستبعد المنشأة من قوائم المتابعة ومن بدء التواصل.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .models import Company, ContactLog, Task, User
from .timeutil import now_utc, riyadh_day_bounds_utc, riyadh_today

STATUSES = {"new": "جديد", "in_progress": "جارٍ التواصل", "contacted": "تم التواصل", "no_answer": "لم يرد",
            "call_later": "طلب التواصل لاحقًا", "offer_sent": "أُرسل عرض", "not_interested": "غير مهتم",
            "do_not_contact": "طلب عدم التواصل"}
UPDATE_CHOICES = ["contacted", "no_answer", "call_later", "offer_sent", "not_interested", "do_not_contact", "new"]
ATTEMPT_STATUSES = {"contacted", "no_answer", "call_later", "offer_sent", "not_interested", "do_not_contact"}
LOCK_MINUTES = 120  # حجز «جارٍ التواصل» ينتهي تلقائيًا إن لم يُحدَّث خلال ساعتين
# مزامنة مرحلة المنشأة في التقارير الحالية
STAGE_OF = {"contacted": "contacted", "no_answer": "needs_followup", "call_later": "needs_followup",
            "offer_sent": "offer_sent", "not_interested": "lost", "do_not_contact": "do_not_contact"}


class ContactBlocked(Exception):
    pass


def _name(s: Session, uid) -> str:
    u = s.get(User, uid) if uid else None
    return u.name if u else ""


def lock_active(c: Company) -> bool:
    return bool(c.contact_lock_by_id and c.contact_lock_at and
                now_utc() - c.contact_lock_at < timedelta(minutes=LOCK_MINUTES))


def summary(s: Session, c: Company) -> dict:
    """ما يُعرض أمام اسم العميل في كل القوائم."""
    locked = lock_active(c)
    today0, _ = riyadh_day_bounds_utc(riyadh_today())
    return {"status": "in_progress" if locked else (c.contact_status or "new"),
            "label": (f"جارٍ التواصل بواسطة {_name(s, c.contact_lock_by_id)}" if locked
                      else STATUSES.get(c.contact_status or "new", c.contact_status)),
            "by": _name(s, c.contact_status_by_id), "at": c.contact_status_at,
            "note": c.last_note or "", "note_by": _name(s, c.last_note_by_id), "note_at": c.last_note_at,
            "attempt_at": c.last_attempt_at, "attempt_by": _name(s, c.last_attempt_by_id),
            "attempt_today": bool(c.last_attempt_at and c.last_attempt_at >= today0),
            "attempts": c.attempts_count or 0, "next_at": c.next_followup_at,
            "next_owner": _name(s, c.next_followup_owner_id), "locked": locked,
            "locked_by_id": c.contact_lock_by_id if locked else None, "reached_dm": c.reached_dm_at}


def start_contact(s: Session, c: Company, user: User) -> None:
    """حجز ذري: ينجح لموظف واحد فقط. يرفض إن كانت المنشأة طلبت عدم التواصل، أو حاول موظف آخر اليوم."""
    if c.contact_status == "do_not_contact" or c.do_not_contact:
        raise ContactBlocked("طلب العميل عدم التواصل — لا يُتصل به.")
    today0, _ = riyadh_day_bounds_utc(riyadh_today())
    if c.last_attempt_at and c.last_attempt_at >= today0 and c.last_attempt_by_id != user.id:
        raise ContactBlocked(f"حاول {_name(s, c.last_attempt_by_id)} التواصل اليوم الساعة "
                             f"{_hm(c.last_attempt_at)} ({STATUSES.get(c.contact_status, '')}) — لا تكرر الاتصال اليوم.")
    stale = now_utc() - timedelta(minutes=LOCK_MINUTES)
    res = s.execute(update(Company).where(
        Company.id == c.id,
        or_(Company.contact_lock_by_id.is_(None), Company.contact_lock_by_id == user.id,
            Company.contact_lock_at < stale)).values(contact_lock_by_id=user.id, contact_lock_at=now_utc())
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        s.refresh(c)
        raise ContactBlocked(f"جارٍ التواصل بواسطة {_name(s, c.contact_lock_by_id)} — انتظر تحديثه.")
    s.add(ContactLog(company_id=c.id, user_id=user.id, user_name=user.name, kind="start", status="in_progress",
                     note=None))
    s.flush()
    s.refresh(c)


def _hm(dt: datetime) -> str:
    from .timeutil import to_riyadh
    return to_riyadh(dt).strftime("%H:%M")


def update_followup(s: Session, c: Company, user: User, status: str, note: str = "",
                    next_at: datetime | None = None, next_owner_id: int | None = None,
                    reached_dm: bool = False) -> None:
    if status not in STATUSES or status == "in_progress":
        raise ValueError("حالة غير معروفة")
    if c.contact_lock_by_id and lock_active(c) and c.contact_lock_by_id != user.id and user.role == "rep":
        raise ContactBlocked(f"جارٍ التواصل بواسطة {_name(s, c.contact_lock_by_id)} — لا يمكن التحديث الآن.")
    now = now_utc()
    note = (note or "").strip()[:2000]
    c.contact_status, c.contact_status_at, c.contact_status_by_id = status, now, user.id
    if note:
        c.last_note, c.last_note_at, c.last_note_by_id = note, now, user.id
    if status in ATTEMPT_STATUSES:
        c.last_attempt_at, c.last_attempt_by_id = now, user.id
        c.attempts_count = (c.attempts_count or 0) + 1
        if not c.contacted_at:
            c.contacted_at, c.contacted_by_id = now, user.id
    if status == "offer_sent":
        c.offer_sent_at, c.offer_sent_by_id = c.offer_sent_at or now, c.offer_sent_by_id or user.id
    c.contact_lock_by_id = c.contact_lock_at = None  # التحديث يحرر الحجز
    if reached_dm and not c.reached_dm_at:  # وصل الفريق إلى المسؤول/صاحب القرار
        c.reached_dm_at, c.reached_dm_by_id = now, user.id
    if status in STAGE_OF:
        c.stage = STAGE_OF[status]
    owner_name = None
    if status == "do_not_contact":
        c.do_not_contact = True
        c.next_followup_at = c.next_followup_owner_id = None
        for t in s.scalars(select(Task).where(Task.company_id == c.id, Task.status == "open")):
            t.status, t.done_at = "cancelled", now
    elif next_at:
        owner = s.get(User, next_owner_id) if next_owner_id else user
        owner = owner or user
        owner_name = owner.name
        c.next_followup_at, c.next_followup_owner_id = next_at, owner.id
        c.next_action, c.next_action_at = (note[:200] or "متابعة"), next_at
        s.add(Task(company_id=c.id, owner_id=owner.id, kind="call", title=f"متابعة — {c.name}",
                   details=note, due_at=next_at))
    s.add(ContactLog(company_id=c.id, user_id=user.id, user_name=user.name, kind="update", status=status, note=note,
                     next_at=next_at if status != "do_not_contact" else None, next_owner_name=owner_name))
    from .companies import log_activity
    log_activity(s, c.id, "status", f"{STATUSES[status]} — {user.name}", note, user_id=user.id)
    s.flush()


def history(s: Session, company_id: int, limit: int = 50) -> list[ContactLog]:
    return list(s.scalars(select(ContactLog).where(ContactLog.company_id == company_id)
                          .order_by(ContactLog.id.desc()).limit(limit)))
