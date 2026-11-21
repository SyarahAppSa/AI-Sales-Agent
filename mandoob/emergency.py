"""«لوحة طوارئ»: منع التنزيل وتسجيل كل محاولة، وحالة الخوادم والقياسات، والسجل الزمني للنشاط.

- المنع على الخادم قبل تكوين الملف (المسارات المعروفة) + شبكة أمان بعد التكوين لأي استجابة ملف لم تُعرف مسبقًا.
- القياسات من داخل الحاوية نفسها (cgroup و/proc) لكل من الموقع والعامل، ومن Render API إن ضُبط RENDER_API_KEY.
- لا يُسجّل أي رمز دخول أو مفتاح خدمة أو محتوى ملف عميل.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import timedelta
from urllib.parse import unquote

from sqlalchemy import func, select, text

from .db import engine, session_scope
from .models import DownloadAttempt, Incident, Notification, Setting, User
from .timeutil import fmt, now_utc

BLOCK_MSG = "تنزيل الملفات محظور وفق سياسة حماية بيانات المنصة"
FILE_EXT = re.compile(r"\.(xlsx|xlsm|xls|csv|pdf|eml|zip|docx)$", re.I)
FILE_PATH = re.compile(r"/(attachment|download|export)(/|$)", re.I)
FILE_TYPES = ("spreadsheetml", "ms-excel", "text/csv", "application/pdf", "application/zip", "octet-stream",
              "message/rfc822", "wordprocessingml")

# اسم كل ملف/تقرير ومعرّفه الثابت (للتقارير التي تُنشأ عند الطلب)
REPORTS = [
    (r"^/officials\.xlsx$", "تقرير موظفي الشركات", "127"),
    (r"^/portal/officials\.xlsx$", "تقرير مسؤولي الشركات (بوابة الموظفين)", "128"),
    (r"^/companies\.xlsx$", "تقرير الشركات وإسنادها", "101"),
    (r"^/today\.xlsx$", "تقرير عملاء اليوم", "102"),
    (r"^/followups\.xlsx$", "تقرير متابعات اليوم", "103"),
    (r"^/daily-report\.xlsx$", "التقرير اليومي", "104"),
    (r"^/ai-costs\.xlsx$", "تقرير تكلفة الذكاء الاصطناعي", "129"),
    (r"^/weekly-report\.xlsx$", "التقرير الأسبوعي للشرائح", "105"),
    (r"^/leads\.xlsx$", "طلبات العروض الواردة", "106"),
    (r"^/offers-report\.xlsx$", "تقرير الشركات التي أُرسلت إليها عروض", "107"),
    (r"^/prospects\.xlsx$", "تقرير العملاء المحتملين", "108"),
    (r"^/random-leads\.xlsx$", "تقرير عملاء عشوائيين", "109"),
    (r"^/portal/export\.xlsx$", "تقرير «عملائي» (بوابة الموظفين)", "110"),
    (r"^/portal/reports\.xlsx$", "تقرير مبيعاتي (بوابة الموظفين)", "111"),
    (r"^/(?:admin-lists|portal/lists)/(\d+)\.xlsx$", "قائمة عملاء إدارية", None),
    (r"^/portal/export/phones\.csv$", "نسخ كل أرقام العملاء (بوابة الموظفين)", "112"),
    (r"^/portal/export/emails\.csv$", "نسخ كل بريد العملاء (بوابة الموظفين)", "113"),
    (r"^/messages/(\d+)/attachment$", "مرفق رسالة عرض", None),
    (r"^/customer-mail/attachment/(\d+)$", "مرفق في «بريد العملاء»", None),
]


def describe(path: str) -> tuple[str, str | None]:
    for rx, label, rid in REPORTS:
        m = re.match(rx, path)
        if m:
            return label, rid or (m.group(1) if m.groups() else None)
    name = path.rstrip("/").rsplit("/", 1)[-1] or path
    return f"ملف «{name}»", (re.search(r"/(\d+)", path).group(1) if re.search(r"/(\d+)", path) else None)


def is_file_path(path: str) -> bool:
    return bool(FILE_EXT.search(path) or FILE_PATH.search(path))


def is_file_response(headers: list[tuple[bytes, bytes]]) -> bool:
    h = {k.decode().lower(): v.decode("latin-1").lower() for k, v in headers}
    if "attachment" in h.get("content-disposition", ""):
        return True
    ct = h.get("content-type", "")
    return any(t in ct for t in FILE_TYPES)


def can_access(user: User | None) -> bool:
    return bool(user and user.active and (user.role == "admin" or (user.perms or {}).get("emergency")))


ALLOWED = "سُمح — مدير المنصة"


def is_owner(session) -> bool:
    """مدير المنصة (المالك) فقط يُسمح له بالتنزيل — من بوابة الإدارة وبحساب نشط."""
    uid = (session or {}).get("uid")
    if not uid or (session or {}).get("portal") != "admin":
        return False
    with session_scope() as s:
        u = s.get(User, uid)
        return bool(u and u.active and u.role == "admin" and u.session_version == (session or {}).get("sv"))


def record_attempt(session: dict, path: str, referer: str, result: str = "مُنع") -> int:
    """يسجّل المحاولة وينبّه أصحاب صلاحية الطوارئ. يعيد رقم التتبّع."""
    label, rid = describe(path)
    uid = (session or {}).get("uid")
    page = ""
    if referer:
        m = re.match(r"https?://[^/]+(/[^?#]*)", referer)
        page = unquote(m.group(1)) if m else ""
    with session_scope() as s:
        u = s.get(User, uid) if uid else None
        a = DownloadAttempt(user_id=u.id if u else None, user_name=u.name if u else "غير مسجّل الدخول",
                            user_email=u.email if u else "", portal=(session or {}).get("portal", ""),
                            page=page[:300], target=path[:300], label=label[:200], report_id=rid, result=result,
                            read=result == ALLOWED)
        s.add(a)
        s.flush()
        if result == ALLOWED:  # تنزيل مدير المنصة: يُسجَّل في السجل دون تنبيه
            return a.id
        title = f"مُنع تنزيل {label}. المستخدم: {a.user_name}." + (f" رقم التقرير: {rid}." if rid else "") + \
            f" الوقت: {fmt(a.at, False).split('، ')[-1] if fmt(a.at, False) else ''}. محاولة رقم: {a.id}"
        for v in s.scalars(select(User).where(User.active.is_(True))):
            if can_access(v):
                s.add(Notification(user_id=v.id, title=title[:300], body="لوحة طوارئ ← محاولات التنزيل"))
        return a.id


class DownloadGuard:
    """يرفض كل تنزيل من الخادم ويُرجع المستخدم إلى صفحته برسالة المنع. داخل SessionMiddleware."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from .config import settings
        path = scope.get("path", "")
        # «ملفات طلبات العملاء»: المواد التي اعتمدتها الإدارة فقط — المسارات تتحقق بنفسها من النشر والصلاحية
        if not settings.downloads_blocked or path.startswith(("/static/", "/kit/")):
            return await self._timed(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        referer = headers.get(b"referer", b"").decode("latin-1")
        session = scope.get("session")
        if is_owner(session):
            if is_file_path(path):
                try:
                    record_attempt(session, path, referer, ALLOWED)
                except Exception:  # noqa: BLE001
                    pass
            return await self._timed(scope, receive, send)
        if is_file_path(path):
            return await self._block(scope, send, path, referer)
        start = {}

        async def _send(msg):
            if msg["type"] == "http.response.start":
                if is_file_response(msg.get("headers") or []):
                    start["blocked"] = True
                    return
                return await send(msg)
            if start.get("blocked"):
                if not msg.get("more_body"):
                    await self._block(scope, send, path, referer, after=True)
                return
            await send(msg)
        await self._timed(scope, receive, _send)

    async def _timed(self, scope, receive, send):
        t0 = time.perf_counter()
        try:
            await self.app(scope, receive, send)
        except Exception as e:
            note_incident("web", "error", f"{scope.get('path', '')[:120]}: {type(e).__name__}")
            raise
        finally:
            LATENCY.append(time.perf_counter() - t0)
            del LATENCY[:-500]

    async def _block(self, scope, send, path, referer, after=False):
        session = scope.get("session")
        try:
            tid = record_attempt(session, path, referer)
        except Exception:  # noqa: BLE001 — المنع يبقى حتى لو تعذّر التسجيل
            tid = None
        back = "/"
        m = re.match(r"https?://[^/]+(/[^#]*)", referer or "")
        if m and not m.group(1).startswith("//") and not is_file_path(m.group(1).split("?")[0]):
            back = m.group(1)
        elif (session or {}).get("portal") == "staff":
            back = "/portal"
        if isinstance(session, dict):
            session["flash"] = {"msg": BLOCK_MSG + (f" (رقم التتبّع: {tid})" if tid else ""), "kind": "err"}
        await send({"type": "http.response.start", "status": 303,
                    "headers": [(b"location", back.encode("utf-8")), (b"cache-control", b"no-store"),
                                (b"x-download-blocked", b"1")]})
        await send({"type": "http.response.body", "body": b""})


LATENCY: list[float] = []


def note_incident(service: str, kind: str, message: str) -> None:
    try:
        with session_scope() as s:
            s.add(Incident(service=service, kind=kind, message=_clean(message)[:500]))
    except Exception:  # noqa: BLE001
        pass


SECRET_RX = re.compile(r"(?i)(code|password|passwd|token|secret|api[_-]?key|authorization)[\"'=:\s]+[^\s,\"'}]+")


def _clean(t: str) -> str:
    return SECRET_RX.sub(lambda m: m.group(1) + "=•••", str(t or ""))


# ---------------------------------------------------------------- القياسات

def _read(path: str) -> str:
    with open(path) as f:
        return f.read().strip()


def container_metrics(prev: dict | None = None) -> dict:
    """ذاكرة ومعالج الحاوية (cgroup v2 ثم v1) وتخزين المجلد المحلي. يرفع استثناء إن تعذّرت كل القراءات."""
    out: dict = {}
    try:
        cur = int(_read("/sys/fs/cgroup/memory.current"))
        mx = _read("/sys/fs/cgroup/memory.max")
        out["mem_used_mb"] = round(cur / 2**20, 1)
        out["mem_limit_mb"] = None if mx == "max" else round(int(mx) / 2**20, 1)
    except OSError:
        try:
            out["mem_used_mb"] = round(int(_read("/sys/fs/cgroup/memory/memory.usage_in_bytes")) / 2**20, 1)
            lim = int(_read("/sys/fs/cgroup/memory/memory.limit_in_bytes"))
            out["mem_limit_mb"] = None if lim > 2**50 else round(lim / 2**20, 1)
        except OSError:
            pass
    try:
        usage = int(re.search(r"usage_usec (\d+)", _read("/sys/fs/cgroup/cpu.stat")).group(1))
        quota = _read("/sys/fs/cgroup/cpu.max").split()
        cpus = (int(quota[0]) / int(quota[1])) if quota[0] != "max" else (os.cpu_count() or 1)
        now = time.time()
        out["cpu_usage_usec"], out["cpu_t"], out["cpu_limit"] = usage, now, round(cpus, 2)
        if prev and prev.get("cpu_usage_usec") and prev.get("cpu_t") and now > prev["cpu_t"]:
            out["cpu_pct"] = round(100 * (usage - prev["cpu_usage_usec"]) / 1e6 / (now - prev["cpu_t"]) / cpus, 1)
    except (OSError, AttributeError, ValueError, IndexError):
        pass
    try:
        du = shutil.disk_usage(os.environ.get("DATA_DIR") or "/tmp")
        out["disk_total_gb"], out["disk_used_gb"] = round(du.total / 2**30, 2), round(du.used / 2**30, 2)
        out["disk_free_gb"] = round(du.free / 2**30, 2)
    except OSError:
        pass
    if not out:
        raise OSError("لا توجد قراءات متاحة")
    return out


def _metric_key(service: str) -> str:
    return f"metrics_{service}"


def refresh_metrics(service: str, reader=container_metrics) -> dict:
    """يحدّث قراءة الخدمة؛ عند الفشل يحتفظ بآخر قراءة ناجحة ويسجّل وقت التعذّر."""
    with session_scope() as s:
        row = s.get(Setting, _metric_key(service))
        old = dict((row.value if row else {}) or {})
        try:
            vals = reader(old.get("values"))
            if service == "web" and LATENCY:
                lat = sorted(LATENCY)
                vals["latency_ms_p50"] = round(lat[len(lat) // 2] * 1000)
                vals["latency_ms_p95"] = round(lat[int(len(lat) * .95) - 1] * 1000)
            new = {"values": vals, "ok_at": now_utc().isoformat(), "error": None, "error_at": old.get("error_at")}
        except Exception as e:  # noqa: BLE001
            new = {**old, "error": f"{type(e).__name__}: {str(e)[:120]}", "error_at": now_utc().isoformat()}
        if row is None:
            s.add(Setting(key=_metric_key(service), value=new))
        else:
            row.value = new
        return new


def worker_heartbeat() -> None:
    """العامل كل دقيقة: نبض + قياساته (لأن قياسات الموقع لا ترى حاوية العامل)."""
    with session_scope() as s:
        row = s.get(Setting, "worker_heartbeat")
        if row is None:
            s.add(Setting(key="worker_heartbeat", value={"at": now_utc().isoformat()}))
        else:
            row.value = {"at": now_utc().isoformat()}
    refresh_metrics("worker")


def db_check() -> tuple[bool, int | None, str]:
    t0 = time.perf_counter()
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True, round((time.perf_counter() - t0) * 1000), ""
    except Exception as e:  # noqa: BLE001
        note_incident("db", "error", f"{type(e).__name__}")
        return False, None, type(e).__name__


def _dt(v):
    from datetime import datetime
    return datetime.fromisoformat(v) if v else None


def services(s) -> list[dict]:
    """حالة الويب وقاعدة البيانات والبحث المجدول — مع آخر فحص وآخر عطل."""
    from .models import DirectoryRun, DiscoveryRun, ProspectRun
    now = now_utc()
    last_inc = lambda svc: s.scalar(select(Incident).where(Incident.service == svc).order_by(Incident.id.desc()))  # noqa: E731
    out = []
    w = refresh_metrics("web")
    inc = last_inc("web")
    out.append({"key": "web", "name": "خدمة الويب (الموقع)", "ok": True, "state": "تعمل", "checked": now,
                "last_error": inc, "metrics": w})
    ok, ms, err = db_check()
    inc = last_inc("db")
    out.append({"key": "db", "name": "قاعدة البيانات", "ok": ok, "state": f"تعمل — زمن الاستجابة {ms} ms" if ok else f"لا تستجيب ({err})",
                "checked": now, "last_error": inc, "metrics": None})
    hb = s.get(Setting, "worker_heartbeat")
    hb_at = _dt((hb.value or {}).get("at")) if hb else None
    alive = bool(hb_at and now - hb_at < timedelta(minutes=3))
    runs = []
    for model, label in ((ProspectRun, "العملاء المحتملون"), (DirectoryRun, "اكتشاف عملاء جدد"), (DiscoveryRun, "البحث عن العملاء")):
        for r in s.scalars(select(model).order_by(model.id.desc()).limit(30)):
            fin = getattr(r, "finished_at", None) or getattr(r, "started_at", None) or getattr(r, "created_at", None)
            runs.append((label, r, fin))
    good = next((x for x in sorted(runs, key=lambda x: x[2] or now, reverse=True)
                 if x[1].status in ("done",)), None)
    bad = next((x for x in sorted(runs, key=lambda x: x[2] or now, reverse=True)
                if x[1].status in ("failed", "stopped", "partial")), None)
    why = ""
    if bad:
        lg = [ln for ln in (getattr(bad[1], "log", None) or []) if any(w in ln for w in ("فشل", "توقف", "تعذ", "تعطل"))]
        from .health import classify
        why = classify(lg[-1] if lg else (getattr(bad[1], "shortfall_reason", "") or "")).get("cause") if (lg or getattr(bad[1], "shortfall_reason", "")) \
            else ("أُوقفت يدويًا" if bad[1].status == "stopped" else "")
    inc = last_inc("scheduler")
    out.append({"key": "scheduler", "name": "خدمة البحث المجدول (العامل)", "ok": alive,
                "state": "تعمل" if alive else ("متوقفة — لا نبض منذ " + (fmt(hb_at) if hb_at else "البداية")),
                "checked": hb_at, "last_error": inc, "metrics": s.get(Setting, "metrics_worker").value if s.get(Setting, "metrics_worker") else None,
                "last_ok_run": good, "last_bad_run": bad, "bad_reason": why})
    return out


# ---------------------------------------------------------------- السجل الزمني

EVENT_TYPES = {"login": "تسجيل الدخول", "perms": "تغيير الصلاحيات والموظفين", "assign": "إسناد العملاء",
               "followup": "تحديث المتابعة", "search": "تشغيل البحث وإيقافه", "download": "محاولات التنزيل",
               "incident": "الأعطال", "other": "إجراءات إدارية أخرى"}
AUDIT_TYPE = {"login": "login", "update_permissions": "perms", "add_user": "perms", "activate": "perms", "deactivate": "perms",
              "invite": "perms", "assign_customers": "assign", "bulk_assign": "assign", "directory_run": "search",
              "directory_auto": "search", "prospecting_config": "search", "prospects_run": "search", "prospects_stop": "search",
              "officials_control": "search"}
ACTION_AR = {"login": "دخل إلى المنصة", "update_permissions": "غيّر صلاحيات", "add_user": "أضاف موظفًا", "activate": "فعّل حساب",
             "deactivate": "أوقف حساب", "invite": "أرسل دعوة إلى", "assign_customers": "أسند عملاء", "bulk_assign": "أسند عملاء",
             "directory_run": "شغّل دفعة اكتشاف", "directory_auto": "غيّر البحث الآلي", "prospecting_config": "غيّر إعدادات البحث",
             "prospects_run": "شغّل دفعة العملاء المحتملين", "prospects_stop": "أوقف دفعة العملاء المحتملين",
             "officials_control": "تحكم في البحث عن أصحاب القرار", "import": "رفع ملف", "export": "صدّر تقريرًا",
             "export_download": "نزّل ملفًا", "mail_secret": "حدّث إعدادات البريد"}


def timeline(s, user_q: str = "", d_from=None, d_to=None, etype: str = "", limit: int = 300) -> list[dict]:
    from .models import AuditLog, ContactLog
    from .timeutil import riyadh_day_bounds_utc
    a = riyadh_day_bounds_utc(d_from)[0] if d_from else None
    b = riyadh_day_bounds_utc(d_to)[1] if d_to else None
    ev = []

    def rng(col, q):
        if a is not None:
            q = q.where(col >= a)
        if b is not None:
            q = q.where(col < b)
        return q
    if etype in ("", "download"):
        for x in s.scalars(rng(DownloadAttempt.at, select(DownloadAttempt)).order_by(DownloadAttempt.id.desc()).limit(limit)):
            ev.append({"at": x.at, "type": "download", "who": x.user_name, "email": x.user_email,
                       "what": f"{'نزّل' if x.result == ALLOWED else 'مُنع تنزيل'} {x.label}" + (f" (رقم التقرير {x.report_id})" if x.report_id else "")
                               + f" — محاولة رقم {x.id}", "where": x.page})
    if etype in ("", "incident"):
        for x in s.scalars(rng(Incident.at, select(Incident)).order_by(Incident.id.desc()).limit(limit)):
            ev.append({"at": x.at, "type": "incident", "who": "النظام", "email": "", "what": f"عطل في {x.service}: {x.message}",
                       "where": ""})
    if etype in ("", "followup"):
        from .models import Company
        for x in s.scalars(rng(ContactLog.at, select(ContactLog)).order_by(ContactLog.id.desc()).limit(limit)):
            c = s.get(Company, x.company_id)
            ev.append({"at": x.at, "type": "followup", "who": x.user_name, "email": "",
                       "what": f"حدّث متابعة «{c.name if c else x.company_id}»: {x.status}", "where": f"/companies/{x.company_id}"})
    if etype != "download" and etype != "incident" and etype != "followup":
        for x in s.scalars(rng(AuditLog.at, select(AuditLog)).order_by(AuditLog.id.desc()).limit(limit)):
            t = AUDIT_TYPE.get(x.action, "other")
            if etype and t != etype:
                continue
            det = {k: ("•••" if re.search(r"(?i)code|pass|token|secret|key", k) else v) for k, v in (x.details or {}).items()}
            det_s = "، ".join(f"{k}: {v}" for k, v in det.items() if v not in (None, "", [], {}))[:160]
            ev.append({"at": x.at, "type": t, "who": x.actor_name, "email": "",
                       "what": f"{ACTION_AR.get(x.action, x.action)}" + (f" «{x.target_name}»" if x.target_name else "")
                               + (f" — {det_s}" if det_s else ""), "where": ""})
    if user_q:
        uq = user_q.strip().lower()
        ev = [e for e in ev if uq in (e["who"] or "").lower() or uq in (e["email"] or "").lower()]
    ev.sort(key=lambda e: e["at"], reverse=True)
    return ev[:limit]


def summary_attempts(s) -> dict:
    unread = s.scalar(select(func.count(DownloadAttempt.id)).where(DownloadAttempt.read.is_(False))) or 0
    return {"unread": unread}


# ---------------------------------------------------------------- المراقبة والمتابعة

def _mem(title: str, v: dict) -> dict:
    used, lim, cpu = v.get("mem_used_mb"), v.get("mem_limit_mb"), v.get("cpu_pct")
    hint = [f"من {lim:g} MB" if lim else "بلا حد معروف", f"المعالج {cpu}%" if cpu is not None else ""]
    return {"title": title, "value": f"{used:g} MB" if used is not None else "لا قراءة بعد",
            "bad": bool(lim and used and used > 0.9 * lim), "long": used is None, "hint": " — ".join(x for x in hint if x)}


def monitor(s, svcs: list[dict] | None = None) -> dict:
    """أرقام «يحتاج تدخلك» و«الحالة الحين» وClaude والبحث والإيميلات — قراءات فقط، بلا بيانات عملاء."""
    from . import goal
    from .models import Company, CostEntry, DirectoryRun, DiscoveryRun, LeadStub, LoginCode, ProspectRun, Task
    from .outreach import active_account
    from .timeutil import now_riyadh, riyadh_today
    now = now_utc()
    today = riyadh_today()
    svcs = svcs if svcs is not None else services(s)
    by = {v["key"]: v for v in svcs}
    cnt = lambda q: s.scalar(q) or 0  # noqa: E731
    inc = lambda svc, since: cnt(select(func.count(Incident.id)).where(Incident.service == svc, Incident.at >= since))  # noqa: E731

    # دفعات البحث: الفاشلة/الموقوفة خلال 7 أيام، والعالقة الآن، والتي تعمل الآن
    week = now - timedelta(days=7)
    failed = running = stuck = 0
    for model in (DirectoryRun, ProspectRun):
        failed += cnt(select(func.count(model.id)).where(model.status.in_(("failed", "stopped")), model.created_at >= week))
        for r in s.scalars(select(model).where(model.status == "running")):
            running += 1
            beat = r.heartbeat or r.started_at or r.created_at
            if beat and now - beat > timedelta(minutes=15):
                stuck += 1
    failed += cnt(select(func.count(DiscoveryRun.id)).where(DiscoveryRun.status.in_(("failed", "partial")),
                                                             DiscoveryRun.started_at >= week))
    for r in s.scalars(select(DiscoveryRun).where(DiscoveryRun.status == "running")):
        running += 1
        if r.started_at and now - r.started_at > timedelta(hours=3):
            stuck += 1

    tgt, got = goal.target(s), goal.achieved(s)
    late_goal = got < tgt and now_riyadh().hour >= 18
    unassigned = cnt(select(func.count(Company.id)).where(Company.data_status == "complete", Company.owner_id.is_(None),
                                                          Company.is_test.is_(False), Company.is_existing_customer.is_(False)))
    overdue = cnt(select(func.count(Task.id)).where(Task.status == "open", Task.due_at < now))
    day_ago = now - timedelta(hours=24)
    incidents = cnt(select(func.count(Incident.id)).where(Incident.at >= day_ago))
    mail_fail = inc("email", day_ago)
    worker = by.get("scheduler") or {}

    attention = [
        {"title": "العامل (البحث المجدول)", "value": "متوقف" if not worker.get("ok") else "يعمل", "bad": not worker.get("ok"),
         "hint": worker.get("state", "")},
        {"title": "دفعات فشلت أو توقفت (7 أيام)", "value": failed, "bad": failed > 0, "hint": "السبب في «حالة الخوادم» أدناه"},
        {"title": "دفعات عالقة الآن", "value": stuck, "bad": stuck > 0, "hint": "تعمل بلا تقدّم منذ أكثر من 15 دقيقة"},
        {"title": "هدف اليوم", "value": f"{got} من {tgt}", "bad": late_goal,
         "hint": (goal.shortfall_reason(s) or "متأخر عن الهدف") if late_goal else ("بلغ الهدف" if got >= tgt else "الجمع مستمر")},
        {"title": "شركات مكتملة بلا مسؤول", "value": unassigned, "bad": unassigned > 0, "hint": "تحتاج إسنادًا لموظف"},
        {"title": "مهام متابعة متأخرة", "value": overdue, "bad": overdue > 0, "hint": "تجاوزت موعدها ولم تُغلق"},
        {"title": "أعطال (24 ساعة)", "value": incidents, "bad": incidents > 0, "hint": "التفاصيل في «السجل الزمني» ← الأعطال"},
        {"title": "فشل إرسال أكواد الدخول (24 ساعة)", "value": mail_fail, "bad": mail_fail > 0, "hint": "الموظف لن يستطيع الدخول"},
    ]

    web = (by.get("web") or {}).get("metrics") or {}
    wv = web.get("values") or {}
    wk = (worker.get("metrics") or {}).get("values") or {}
    db = by.get("db") or {}
    hb = worker.get("checked")
    live = [
        {"title": "قاعدة البيانات", "value": db.get("state", "—").replace("تعمل — زمن الاستجابة ", "") if db.get("ok") else "لا تستجيب",
         "bad": not db.get("ok"), "hint": "زمن استعلام بسيط"},
        {"title": "زمن استجابة الموقع", "value": f"{wv['latency_ms_p50']} ms" if wv.get("latency_ms_p50") is not None else "—", "bad": (wv.get("latency_ms_p95") or 0) > 3000,
         "hint": f"95% من الطلبات أسرع من {wv['latency_ms_p95']} ms" if wv.get("latency_ms_p95") is not None else "لا قراءة بعد"},
        _mem("ذاكرة الموقع", wv),
        _mem("ذاكرة العامل", wk),
        {"title": "آخر نبضة من العامل", "value": (f"{int((now - hb).total_seconds())} ث") if hb else "—",
         "bad": not worker.get("ok"), "hint": "ينبض كل دقيقة"},
        {"title": "دفعات بحث تعمل الآن", "value": running, "bad": False, "hint": f"منها عالقة: {stuck}"},
    ]

    ten = now - timedelta(minutes=10)
    month0 = today.replace(day=1)
    claude_q = lambda *w: select(func.count(CostEntry.id)).where(CostEntry.service == "claude", *w)  # noqa: E731
    spend = lambda *w: round(float(s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(*w)) or 0), 2)  # noqa: E731
    cap = float(goal.get(s).get("cost_cap_usd") or 0)
    today_all = spend(CostEntry.day == today)
    claude = [
        {"title": "طلبات آخر 10 د", "value": cnt(claude_q(CostEntry.at >= ten)), "bad": False, "hint": f"اليوم: {cnt(claude_q(CostEntry.day == today))}"},
        {"title": "أخطاء آخر 10 د", "value": inc("claude", ten), "bad": inc("claude", ten) > 0, "hint": f"آخر 24 ساعة: {inc('claude', day_ago)}"},
        {"title": "صرف Claude اليوم", "value": f"${spend(CostEntry.day == today, CostEntry.service == 'claude')}", "bad": False,
         "hint": f"الشهر: ${spend(CostEntry.day >= month0, CostEntry.service == 'claude')}"},
        {"title": "كل التكاليف اليوم", "value": f"${today_all}", "bad": bool(cap and today_all >= cap),
         "hint": f"الحد اليومي ${cap:g}" if cap else "بلا حد يومي"},
    ]

    stubs_today = cnt(select(func.count(LeadStub.id)).where(LeadStub.created_at >= now - timedelta(hours=24)))
    good = worker.get("last_ok_run")
    search = [
        {"title": "مكتملة اليوم", "value": f"{got} من {tgt}", "bad": late_goal, "hint": "جوال سعودي وبريد بإثبات"},
        {"title": "منشآت مكتشفة (24 ساعة)", "value": stubs_today, "bad": False, "hint": "قبل التحقق والتصفية"},
        {"title": "آخر دفعة نجحت", "value": fmt(good[2]) if good and good[2] else "—", "bad": False, "long": True,
         "hint": f"{good[0]} #{good[1].id}" if good else ""},
    ]

    acct = active_account(s)
    codes = cnt(select(func.count(LoginCode.id)).where(LoginCode.created_at >= day_ago))
    emails = [
        {"title": "بريد المنصة", "value": "مربوط" if acct else "غير مربوط", "bad": acct is None,
         "hint": "منه تُرسل أكواد الدخول"},
        {"title": "أكواد دخول طُلبت (24 ساعة)", "value": codes, "bad": False, "hint": f"آخر ساعة: {cnt(select(func.count(LoginCode.id)).where(LoginCode.created_at >= now - timedelta(hours=1)))}"},
        {"title": "فشل الإرسال (24 ساعة)", "value": mail_fail, "bad": mail_fail > 0, "hint": "أكواد دخول لم تُرسل"},
    ]
    return {"attention": attention, "needs": sum(1 for a in attention if a["bad"]), "live": live, "claude": claude,
            "search": search, "emails": emails}
