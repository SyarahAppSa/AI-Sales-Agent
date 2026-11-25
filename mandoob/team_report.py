"""إرسال «تقرير الفريق وجودة البيانات» يوميًا إلى بريد المدراء (المالك ومديري المبيعات النشطين).
يُرسل من بريد المنصة مرة واحدة يوميًا بعد الوقت المضبوط (افتراضيًا 6:00 مساءً بتوقيت الرياض)."""
from __future__ import annotations

import html as _h
from datetime import date

from sqlalchemy import select

from .db import session_scope
from .models import Setting, User
from .timeutil import now_riyadh, riyadh_today

KEY = "team_report_email"
DEFAULT = {"enabled": True, "time": "18:00"}


def get_cfg(s) -> dict:
    row = s.get(Setting, KEY)
    return {**DEFAULT, **(dict(row.value) if row and row.value else {})}


def put_cfg(s, **kw) -> dict:
    v = {**get_cfg(s), **kw}
    row = s.get(Setting, KEY)
    if row is None:
        s.add(Setting(key=KEY, value=v))
    else:
        row.value = v
    return v


def recipients(s) -> list[User]:
    return [u for u in s.scalars(select(User).where(User.active.is_(True), User.role.in_(("admin", "manager"))))
            if u.email and not u.email.endswith((".invalid", ".local", ".test"))]


def build_html(s, day: date, base_url: str = "") -> tuple[str, str, str]:
    from .reports import data_quality, team_activity
    owner = s.scalar(select(User).where(User.role == "admin", User.active.is_(True)).order_by(User.id)) \
        or User(name="المنصة", role="admin", scope="all")  # التقرير الكامل للمدراء
    r = team_activity(s, owner, day, day)
    q = data_quality(s, owner)
    esc = _h.escape
    th = "padding:6px 8px;border:1px solid #e5e0ee;background:#f4f1f9;font-size:12px;white-space:nowrap"
    td = "padding:6px 8px;border:1px solid #e5e0ee;font-size:13px;text-align:center"
    head = "".join(f"<th style='{th}'>{esc(lab)}</th>" for _, lab in r["COLS"])
    body = ""
    for x in r["reps"]:
        body += (f"<tr><td style='{td};text-align:right;font-weight:700'>{esc(x['name'])}</td>"
                 + "".join(f"<td style='{td}'>{x[k] or ''}</td>" for k, _ in r["COLS"]) + "</tr>")
    body += (f"<tr><td style='{td};text-align:right;font-weight:800'>الإجمالي</td>"
             + "".join(f"<td style='{td};font-weight:800'>{r['totals'][k]}</td>" for k, _ in r["COLS"]) + "</tr>")
    ql = [("عملاء مسندون", f"{q['total']:,}"), ("لديهم جوال سعودي", f"{q['phone_pct']}%"),
          ("لديهم بريد", f"{q['email_pct']}%"), ("بيانات مكتملة", f"{q['complete_pct']}%"),
          ("لم يُتواصل معهم بعد", f"{q['no_contact']:,} ({q['no_contact_pct']}%)"),
          ("«لم يرد» 3 مرات أو أكثر", f"{q['many_no_answer']:,}"), ("من عملاء CRM", f"{q['crm']:,}")]
    qhtml = "".join(f"<tr><td style='{td};text-align:right'>{esc(a)}</td><td style='{td};font-weight:700'>{b}</td></tr>" for a, b in ql)
    link = f"<p><a href='{base_url.rstrip('/')}/team-activity?d_from={day}&d_to={day}'>فتح التقرير في المنصة</a></p>" if base_url else ""
    subject = f"تقرير الفريق — {day.isoformat()} — {r['totals']['total']} تواصل، {r['totals']['assigned']} إسناد"
    html = (f"<div dir='rtl' style='font-family:Tahoma,Arial,sans-serif;color:#292431'>"
            f"<h2 style='margin:0 0 6px'>تقرير الفريق اليومي — {day.isoformat()}</h2>"
            f"<p style='color:#6F6879;margin:0 0 12px'>نتائج التواصل لكل موظف بتوقيت الرياض (بلا البيانات التجريبية).</p>"
            f"<table style='border-collapse:collapse'><tr><th style='{th}'>الموظف</th>{head}</tr>{body}</table>"
            f"<h3 style='margin:18px 0 6px'>جودة بيانات العملاء المسندين</h3>"
            f"<table style='border-collapse:collapse'>{qhtml}</table>{link}"
            f"<p style='color:#6F6879;font-size:12px'>المندوب الذكي لسيارة اب — يُرسل يوميًا لمدراء المنصة.</p></div>")
    text = subject + "\n\n" + "\n".join(
        f"{x['name']}: " + "، ".join(f"{lab} {x[k]}" for k, lab in r["COLS"] if x[k]) for x in r["reps"])
    return subject, text, html


def send(day: date | None = None, only: list[str] | None = None) -> dict:
    from .config import settings
    from .mail import get_provider
    from .mail.base import OutgoingEmail
    from .outreach import active_account
    day = day or riyadh_today()
    sent, failed = [], []
    with session_scope() as s:
        subject, text, html = build_html(s, day, settings.public_base_url)
        to = [u.email for u in recipients(s) if not only or u.email in only]
        acct = active_account(s)
        if acct is not None:
            s.expunge(acct)
    if acct is None:
        return {"sent": [], "failed": to, "why": "بريد المنصة غير مربوط"}
    prov = get_provider(acct)
    for addr in to:
        try:
            res = prov.send(OutgoingEmail(acct.sender_address, "المندوب الذكي — تقرير الفريق", addr, subject, text, html))
            (sent if res and res.accepted else failed).append(addr)
        except Exception:  # noqa: BLE001
            failed.append(addr)
    return {"sent": sent, "failed": failed}


def tick() -> bool:
    """العامل كل 5 دقائق: يرسل تقرير اليوم مرة واحدة بعد الوقت المضبوط."""
    with session_scope() as s:
        cfg = get_cfg(s)
        if not cfg.get("enabled"):
            return False
        hh, mm = (int(x) for x in str(cfg.get("time", "18:00")).split(":"))
        now = now_riyadh()
        today = riyadh_today().isoformat()
        if (now.hour, now.minute) < (hh, mm) or cfg.get("last_sent") == today:
            return False
        put_cfg(s, last_sent=today)  # قبل الإرسال: لا تكرار إن تعطل العامل في منتصفه
    out = send()
    with session_scope() as s:
        put_cfg(s, last_result={"day": today, "sent": len(out["sent"]), "failed": out["failed"]})
    return True
