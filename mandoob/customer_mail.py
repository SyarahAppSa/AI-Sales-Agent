"""«بريد العملاء»: صندوق مستقل (مثل syarahapp@mandoob.live على Zoho) لمراسلة العملاء يدويًا من المنصة.

- الإرسال عبر SMTP بأي نص وأي عدد من المرفقات، مع نسخة في مجلد «المرسل» على الصندوق نفسه.
- الردود تُقرأ عبر IMAP كل دقيقتين وتُربط بالمحادثة (In-Reply-To/References) أو بالشركة من بريدها، وتنبّه المرسل.
- كلمة السر تُدخل من صفحة المنصة وتُحفظ مشفّرة ولا تُعرض أبدًا. منفصل تمامًا عن بريد أكواد الدخول.
"""
from __future__ import annotations

import email
import email.utils
import html as _html
import imaplib
import logging
import mimetypes
import re
import smtplib
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import default as _policy

from sqlalchemy import or_, select

from .db import session_scope
from .models import Company, Contact, CustomerMail, CustomerMailFile, Notification, Setting, Suppression, User
from .timeutil import now_utc

log = logging.getLogger(__name__)
KEY = "customer_mail"
MAX_TOTAL = 20 * 1024 * 1024  # حد Zoho لمرفقات الرسالة الواحدة
# (SMTP, IMAP) لكل منطقة Zoho — يُجرَّب بالترتيب حتى ينجح الدخول
ZOHO_PAIRS = [("smtppro.zoho.com", "imappro.zoho.com"), ("smtp.zoho.com", "imap.zoho.com"),
              ("smtppro.zoho.sa", "imappro.zoho.sa"), ("smtp.zoho.sa", "imap.zoho.sa"),
              ("smtppro.zoho.eu", "imappro.zoho.eu"), ("smtp.zoho.eu", "imap.zoho.eu")]
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


# ---------------------------------------------------------------- الإعداد

def get_cfg(s) -> dict:
    row = s.get(Setting, KEY)
    return dict(row.value or {}) if row else {}


def put_cfg(s, cfg: dict) -> None:
    row = s.get(Setting, KEY)
    if row is None:
        s.add(Setting(key=KEY, value=cfg))
    else:
        row.value = dict(cfg)


def configured(cfg: dict) -> bool:
    return bool(cfg.get("address") and cfg.get("password_enc") and cfg.get("smtp_host"))


def _smtp(host: str):
    return smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=30)


def _imap(host: str):
    return imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context(), timeout=30)


IMAP_HOSTS = ["imap.zoho.com", "imappro.zoho.com", "imap.zoho.sa", "imappro.zoho.sa", "imap.zoho.eu", "imappro.zoho.eu"]


def find_imap(address: str, password: str, imap_factory=None) -> tuple[str | None, str]:
    """يجرّب خوادم IMAP كلًا على حدة (الخادم الصحيح يختلف حسب نوع الحساب). يعيد (الخادم، سبب الفشل)."""
    why = "تعذّر الوصول إلى خادم IMAP"
    for ih in IMAP_HOSTS:
        try:
            m = (imap_factory or _imap)(ih)
            m.login(address, password)
            m.logout()
            return ih, ""
        except imaplib.IMAP4.error as e:
            msg = str(e)
            if "enable IMAP" in msg or "yet to enable" in msg:
                return None, ("قراءة الردود متوقفة لأن IMAP غير مفعّل لهذا البريد في Zoho — فعّله من Zoho Mail: "
                              "الإعدادات ← حسابات البريد ← IMAP، وستبدأ المنصة قراءة الردود تلقائيًا خلال دقائق")
            continue
        except OSError:
            continue
    return None, why


def connect(address: str, password: str) -> tuple[dict | None, str]:
    """يجرّب الدخول إلى SMTP ثم IMAP على خوادم Zoho دون إرسال شيء. يعيد (الخوادم، سبب الفشل)."""
    why = "تعذّر الوصول إلى خوادم Zoho"
    for sh in ("smtp.zoho.com", "smtppro.zoho.com", "smtp.zoho.sa", "smtppro.zoho.sa", "smtp.zoho.eu", "smtppro.zoho.eu"):
        try:
            with _smtp(sh) as srv:
                srv.login(address, password)
        except smtplib.SMTPAuthenticationError:
            why = ("البريد أو كلمة السر غير صحيحة — إن كان التحقق بخطوتين مفعّلًا في Zoho فاستخدم «كلمة مرور التطبيق»، "
                   "وتأكد أن الوصول عبر SMTP/IMAP مفعّل للحساب")
            continue
        except (smtplib.SMTPException, OSError):
            continue
        ih, iwhy = find_imap(address, password)
        return {"smtp_host": sh, "imap_host": ih}, iwhy
    return None, why


def save_connection(s, address: str, name: str, password: str, user: User) -> tuple[bool, str]:
    from .secretbox import seal
    hosts, why = connect(address, password)
    if not hosts:
        return False, why
    cfg = get_cfg(s)
    if cfg.get("address") and cfg.get("address") != address:
        cfg.pop("last_uid", None)
        cfg.pop("uidvalidity", None)
    cfg.update(address=address, name=name or cfg.get("name") or "سيارة آب", password_enc=seal(password),
               connected_at=now_utc().isoformat(), connected_by=user.id, last_error="", **hosts)
    cfg.setdefault("since", (now_utc() - timedelta(days=3)).date().isoformat())
    put_cfg(s, cfg)
    return True, why  # why غير فارغ = الإرسال يعمل والردود لا


# ---------------------------------------------------------------- الإرسال

def addresses(raw: str) -> list[str]:
    from .normalize import normalize_email
    out = []
    for a in _EMAIL.findall(raw or ""):
        e = normalize_email(a) or a.lower()
        if e not in out:
            out.append(e)
    return out


def suppressed(s, addrs: list[str]) -> list[str]:
    bad = []
    for a in addrs:
        dom = a.split("@", 1)[1]
        if s.scalar(select(Suppression.id).where(Suppression.channel.in_(("all", "email")), or_(
                (Suppression.kind == "email") & (Suppression.value == a),
                (Suppression.kind == "domain") & (Suppression.value == dom)))):
            bad.append(a)
    return bad


def text_to_html(body: str, signature_html: str = "") -> str:
    paras = _html.escape(body or "").replace("\r\n", "\n").replace("\n", "<br>")
    sig = f"<div style='margin-top:18px;color:#555'>{signature_html}</div>" if signature_html else ""
    return f"<div dir='auto' style='font-family:Tahoma,Arial,sans-serif;font-size:15px;line-height:1.7'>{paras}{sig}</div>"


def company_for(s, addrs: list[str]) -> int | None:
    for a in addrs:
        cid = s.scalar(select(Contact.company_id).where(Contact.kind == "email", Contact.value == a))
        if cid:
            return cid
    for a in addrs:  # نفس الدومين (غير المجاني)
        dom = a.split("@", 1)[1]
        if dom in ("gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com", "live.com"):
            continue
        cid = s.scalar(select(Contact.company_id).where(Contact.kind == "email", Contact.value.like(f"%@{dom}")))
        if cid:
            return cid
    return None


def send(s, user: User, to: str, cc: str, subject: str, body: str, files: list[tuple[str, bytes, str]],
         company_id: int | None = None, reply_to_id: int | None = None, smtp_factory=None) -> CustomerMail:
    from .secretbox import unseal
    cfg = get_cfg(s)
    if not configured(cfg):
        raise ValueError("بريد العملاء غير مربوط بعد")
    tos, ccs = addresses(to), addresses(cc)
    if not tos:
        raise ValueError("اكتب بريد المستلم بشكل صحيح")
    blocked = suppressed(s, tos + ccs)
    if blocked:
        raise ValueError("هذا البريد في قائمة إيقاف التواصل: " + "، ".join(blocked))
    total = sum(len(b) for _, b, _ in files)
    if total > MAX_TOTAL:
        raise ValueError(f"حجم المرفقات {total / 1048576:.1f} MB — الحد 20 MB للرسالة")
    subject = (subject or "").strip() or "(بلا عنوان)"
    parent = s.get(CustomerMail, reply_to_id) if reply_to_id else None
    domain = cfg["address"].split("@", 1)[1]
    mid = f"<{uuid.uuid4().hex}@{domain}>"
    m = EmailMessage()
    m["From"] = email.utils.formataddr((cfg.get("name") or "سيارة آب", cfg["address"]))
    m["To"] = ", ".join(tos)
    if ccs:
        m["Cc"] = ", ".join(ccs)
    m["Subject"] = subject
    m["Date"] = email.utils.formatdate(localtime=False)
    m["Message-ID"] = mid
    refs = ""
    if parent and parent.message_id:
        refs = " ".join(dict.fromkeys(((parent.references or parent.in_reply_to or "") + " " + parent.message_id).split()))
        m["In-Reply-To"] = parent.message_id
        m["References"] = refs
    sig_txt = re.sub(r"<[^>]+>", "", cfg.get("signature") or "").strip()
    m.set_content((body or "") + (f"\n\n{sig_txt}" if sig_txt else ""))
    m.add_alternative(text_to_html(body, cfg.get("signature") or ""), subtype="html")
    for name, data, mime in files:
        mt, st = (mime or mimetypes.guess_type(name)[0] or "application/octet-stream").split("/", 1)
        m.add_attachment(data, maintype=mt, subtype=st, filename=name)
    company_id = company_id or (parent.company_id if parent else None) or company_for(s, tos)
    row = CustomerMail(direction="out", thread_key=(parent.thread_key if parent else mid), company_id=company_id,
                       from_addr=cfg["address"], from_name=cfg.get("name") or "", to_addr=", ".join(tos), cc=", ".join(ccs),
                       subject=subject, body_text=body or "", message_id=mid, in_reply_to=parent.message_id if parent else None,
                       references=refs, sent_by=user.id, read=True, status="sent")
    password = unseal(cfg["password_enc"])
    try:
        with (smtp_factory or _smtp)(cfg["smtp_host"]) as srv:
            srv.login(cfg["address"], password)
            srv.send_message(m)
    except (smtplib.SMTPException, OSError) as e:
        row.status, row.detail = "failed", f"تعذّر الإرسال ({type(e).__name__})"
        from .emergency import note_incident
        note_incident("email", "customer_mail", f"فشل إرسال بريد عميل ({type(e).__name__})")
    s.add(row)
    s.flush()
    for name, data, mime in files:
        s.add(CustomerMailFile(mail_id=row.id, name=name[:255], mime=mime or "application/octet-stream", size=len(data), data=data))
    if row.status == "sent" and cfg.get("imap_host") and smtp_factory is None:
        _append_sent(cfg, password, m)
    if row.status == "sent" and company_id:
        from .models import Activity
        s.add(Activity(company_id=company_id, user_id=user.id, kind="email", title=f"أُرسل بريد: {subject}"[:300],
                       details=f"إلى {', '.join(tos)}", ref_type="customer_mail", ref_id=row.id))
    return row


def _append_sent(cfg: dict, password: str, m: EmailMessage) -> None:
    """نسخة في مجلد «المرسل» على Zoho حتى تظهر الرسالة في صندوق البريد نفسه (أفضل جهد)."""
    try:
        c = _imap(cfg["imap_host"])
        c.login(cfg["address"], password)
        for box in ("Sent", "INBOX.Sent", "Sent Items"):
            typ, _ = c.append(box, "\\Seen", imaplib.Time2Internaldate(time.time()), m.as_bytes())
            if typ == "OK":
                break
        c.logout()
    except Exception:  # noqa: BLE001
        log.info("append to Sent failed", exc_info=True)


# ---------------------------------------------------------------- قراءة الردود

def _text_of(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        txt = part.get_content()
    except Exception:  # noqa: BLE001
        txt = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        txt = re.sub(r"(?is)<(script|style).*?</\1>", "", txt)
        txt = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", txt)
        txt = _html.unescape(re.sub(r"<[^>]+>", "", txt))
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def ingest(s, raw: bytes, uid: str, own: str) -> CustomerMail | None:
    """يحفظ رسالة واردة ويربطها بالمحادثة والشركة. يعيد None إن كانت محفوظة مسبقًا أو من الصندوق نفسه."""
    from .mail.base import classify_report
    msg = email.message_from_bytes(raw, policy=_policy)
    mid = (msg.get("Message-ID") or "").strip() or None
    if mid and s.scalar(select(CustomerMail.id).where(CustomerMail.message_id == mid)):
        return None
    name, addr = email.utils.parseaddr(str(msg.get("From") or ""))
    addr = (addr or "").lower()
    if addr == own:
        return None
    irt = (msg.get("In-Reply-To") or "").strip() or None
    refs = " ".join(str(msg.get("References") or "").split())
    known = [x for x in ([irt] if irt else []) + refs.split() if x]
    parent = s.scalar(select(CustomerMail).where(CustomerMail.message_id.in_(known)).order_by(CustomerMail.id.desc())) if known else None
    subject = str(msg.get("Subject") or "")[:300]
    text = _text_of(msg)
    kind, reason = classify_report(addr, subject, text)
    try:
        at = email.utils.parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        at = now_utc()
    row = CustomerMail(direction="in", thread_key=parent.thread_key if parent else (mid or f"uid:{uid}"),
                       company_id=(parent.company_id if parent else None) or company_for(s, [addr]),
                       from_addr=addr, from_name=str(name or "")[:200], to_addr=str(msg.get("To") or "")[:500],
                       cc=str(msg.get("Cc") or "")[:500], subject=subject, body_text=text[:200000], message_id=mid,
                       in_reply_to=irt, references=refs[:5000], imap_uid=uid, at=at,
                       status="bounced" if kind == "bounced" else "received", detail=(reason or "")[:500])
    s.add(row)
    s.flush()
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True) or b""
        if not data or len(data) > 15 * 1024 * 1024:
            continue
        s.add(CustomerMailFile(mail_id=row.id, name=(part.get_filename() or "مرفق")[:255],
                               mime=part.get_content_type(), size=len(data), data=data))
    if row.company_id and kind != "bounced":
        from .models import Activity
        s.add(Activity(company_id=row.company_id, kind="email_reply", title=f"رد بريد: {subject}"[:300],
                       details=text[:800], ref_type="customer_mail", ref_id=row.id))
    if parent and kind == "bounced":
        parent.status, parent.detail = "bounced", (reason or "ارتد البريد")[:500]
    if kind != "bounced":
        who = {parent.sent_by} if parent and parent.sent_by else set()
        who |= set(s.scalars(select(User.id).where(User.role == "admin", User.active.is_(True))))
        co = s.get(Company, row.company_id) if row.company_id else None
        for uid_ in who:
            s.add(Notification(user_id=uid_, company_id=row.company_id,
                               title=f"رد بريد من {co.name if co else (name or addr)}",
                               body=f"{subject}\n\n{text[:600]}"))
    return row


def sync(imap_factory=None) -> dict:
    """يقرأ الجديد من صندوق الوارد عبر IMAP (UID أكبر من آخر مقروء)."""
    from .secretbox import unseal
    with session_scope() as s:
        cfg = get_cfg(s)
    if not configured(cfg):
        return {"skipped": True}
    if not cfg.get("imap_host"):  # لم يُفعَّل IMAP عند الربط: نعيد المحاولة كل 30 دقيقة فتبدأ القراءة تلقائيًا بعد تفعيله
        last = cfg.get("imap_probe_at")
        if last and now_utc() - datetime.fromisoformat(last) < timedelta(minutes=30) and imap_factory is None:
            return {"skipped": True}
        ih, why = find_imap(cfg["address"], unseal(cfg["password_enc"]), imap_factory)
        with session_scope() as s:
            cur = get_cfg(s)
            cur.update(imap_probe_at=now_utc().isoformat(), imap_host=ih, last_error="" if ih else why)
            put_cfg(s, cur)
        if not ih:
            return {"skipped": True, "why": why}
        cfg["imap_host"] = ih
    got = 0
    try:
        c = (imap_factory or _imap)(cfg["imap_host"])
        c.login(cfg["address"], unseal(cfg["password_enc"]))
        c.select("INBOX", readonly=True)
        uv = (c.response("UIDVALIDITY")[1] or [b""])[0]
        uv = uv.decode() if isinstance(uv, bytes) else str(uv or "")
        last = int(cfg.get("last_uid") or 0) if cfg.get("uidvalidity") == uv else 0
        if last:
            typ, data = c.uid("search", None, f"UID {last + 1}:*")
        else:
            since = datetime.fromisoformat(cfg.get("since") or (now_utc() - timedelta(days=3)).date().isoformat())
            typ, data = c.uid("search", None, f'SINCE {since.strftime("%d-%b-%Y")}')
        uids = [int(x) for x in (data[0] or b"").split() if int(x) > last] if typ == "OK" else []
        for u in sorted(uids)[:200]:
            typ, parts = c.uid("fetch", str(u), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if typ == "OK" and raw:
                with session_scope() as s:
                    if ingest(s, raw, str(u), cfg["address"]):
                        got += 1
            last = max(last, u)
        c.logout()
        with session_scope() as s:
            cur = get_cfg(s)
            cur.update(last_uid=last, uidvalidity=uv, last_sync=now_utc().isoformat(), last_error="")
            put_cfg(s, cur)
    except Exception as e:  # noqa: BLE001
        with session_scope() as s:
            cur = get_cfg(s)
            cur.update(last_error=f"تعذّرت قراءة الردود ({type(e).__name__})", last_sync=now_utc().isoformat())
            put_cfg(s, cur)
        from .emergency import note_incident
        note_incident("email", "customer_mail", f"تعذّرت قراءة ردود بريد العملاء ({type(e).__name__})")
        return {"error": type(e).__name__}
    return {"new": got}


def tick() -> dict:
    from .jobs import job_lock
    with job_lock("customer_mail", minutes=10) as ok:
        if not ok:
            return {}
        return sync()


# ---------------------------------------------------------------- العرض

def threads(s, q: str = "", limit: int = 100) -> list[dict]:
    rows = list(s.scalars(select(CustomerMail).order_by(CustomerMail.at.desc()).limit(2000)))
    out: dict[str, dict] = {}
    for r in rows:
        t = out.get(r.thread_key)
        if t is None:
            t = out[r.thread_key] = {"key": r.thread_key, "last": r, "subject": r.subject, "count": 0, "unread": 0,
                                     "company": None, "who": "", "replied": False, "first_id": r.id}
        t["count"] += 1
        t["first_id"] = r.id
        if r.direction == "in" and not r.read:
            t["unread"] += 1
        if r.direction == "in" and r.status == "received":
            t["replied"] = True
        if r.company and not t["company"]:
            t["company"] = r.company
        if not t["who"]:
            t["who"] = r.to_addr if r.direction == "out" else (r.from_name or r.from_addr)
        t["subject"] = r.subject or t["subject"]
    res = list(out.values())
    if q:
        ql = q.lower()
        res = [t for t in res if ql in (t["subject"] or "").lower() or ql in (t["who"] or "").lower()
               or (t["company"] and ql in t["company"].name.lower())]
    return res[:limit]


def unread_count(s) -> int:
    from sqlalchemy import func
    return s.scalar(select(func.count(CustomerMail.id)).where(CustomerMail.direction == "in", CustomerMail.read.is_(False),
                                                              CustomerMail.status == "received")) or 0
