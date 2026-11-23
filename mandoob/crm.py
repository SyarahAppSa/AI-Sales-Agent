"""«عملاء CRM»: نسخة للقراءة فقط من عملاء الشركة في crm.syarahapp.sa.

- الربط الرسمي: واجهة ABP على crmapi.syarahapp.sa — رمز OAuth (العميل CRM_App) ثم قائمة العملاء على صفحات.
  لا تُرسل المنصة أي تعديل إلى الـCRM (قراءة فقط).
- بيانات الدخول تُدخلها الإدارة في «إعدادات الربط» وتُحفظ مشفّرة (secretbox)؛ لا تُعاد للواجهة ولا تُكتب في السجلات.
- بديل الربط: استيراد ملف Excel/CSV مصدَّر من الـCRM بالقواعد نفسها.
- رقم العميل في الـCRM هو مفتاح منع التكرار. العميل الموجود في المنصة بمطابقة مؤكدة (الجوال أو البريد) يُربط بسجله
  نفسه: يبقى مندوبه وملاحظاته وسجل اتصالاته، وتُضاف إليه بيانات الـCRM (الحالة، الموظف، الملاحظات).
- كل عميل CRM يُضاف إلى «عملاء سيارة آب الحاليين» فيُستبعد من عدّاد الألف عميل محتمل الجديد يوميًا.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

import httpx
from sqlalchemy import or_, select

from .db import session_scope
from .models import Company, Contact, ExistingCustomer, Setting, User
from .normalize import normalize_email, normalize_name, normalize_phone
from .timeutil import now_utc

log = logging.getLogger(__name__)

CONN_KEY, STATE_KEY = "crm_conn", "crm_state"
DEFAULT_CONN = {"base_url": "https://crmapi.syarahapp.sa", "client_id": "CRM_App", "scope": "offline_access CRM",
                "entity": "contact", "username": "", "password_enc": "", "daily": True}
PAGE = 100
UA = "MandoobSyarahApp/1.0 (read-only CRM sync)"

# أسماء الحقول المحتملة (واجهة ABP بأسماء camelCase، وملفات التصدير بعناوين عربية أو إنجليزية)
FIELDS = {
    "crm_id": ["customer id", "customerid", "id", "contactid", "customerid", "رقم العميل", "رقم العميل في الcrm", "معرف العميل", "customer id", "crm id"],
    "number": ["code", "number", "contactnumber", "customernumber", "serial", "serialnumber", "رقم", "الرقم", "كود العميل"],
    "company": ["customer name", "customername", "companyname", "company", "organization", "organizationname", "accountname", "businessname", "اسم الشركة",
                "الشركة", "المنشأة", "اسم المنشأة", "company name"],
    "contact": ["contact person name", "contactpersonname", "contactname", "contactperson", "fullname", "name", "personname", "اسم مسؤول التواصل", "مسؤول التواصل",
                "اسم العميل", "الاسم", "contact name"],
    "first": ["firstname", "الاسم الأول"], "last": ["lastname", "surname", "اسم العائلة"],
    "phone": ["customer phone", "contact person phone", "customerphone", "phonenumber", "mobile", "mobilenumber", "phone", "whatsapp", "whatsappnumber", "رقم الجوال", "الجوال",
              "الهاتف", "رقم الهاتف", "mobile number", "phone number"],
    "email": ["customer email", "contact person email", "customeremail", "email", "emailaddress", "البريد", "البريد الإلكتروني", "البريد الالكتروني", "e-mail"],
    "city": ["city (arabic)", "city", "cityname", "address.city", "المدينة", "city name"],
    "status": ["status", "statusname", "contactstatus", "leadstatus", "stage", "stagename", "حالة العميل", "الحالة"],
    "notes": ["customer description", "notes", "note", "comment", "comments", "description", "remarks", "الملاحظات", "ملاحظات", "ملاحظة"],
    "owner": ["assigned to", "assigned to username", "assignedto.name", "assignedto.fullname", "assignedtoname", "assignedtousername", "assignedto.username",
              "assigneduser.name", "assigneduser.username", "assignedusername", "ownername", "owner.name", "salesrep",
              "salesrepname", "employeename", "username", "الموظف", "اسم الموظف", "الموظف المسؤول", "المسؤول",
              "assigned to", "owner"],
    "cars": ["number of cars", "numberofcars", "عدد السيارات", "عدد المركبات"],
    "owner_email": ["assigned to email", "assignedto.email", "assigneduser.email", "owner.email", "ownermail"],
}


# ---------------------------------------------------------------- الإعدادات والحالة

def get_conn(s) -> dict:
    row = s.get(Setting, CONN_KEY)
    return {**DEFAULT_CONN, **((row.value if row else None) or {})}


def set_conn(s, **kw) -> dict:
    from .secretbox import seal
    cur = get_conn(s)
    pw = kw.pop("password", None)
    if pw:
        cur["password_enc"] = seal(pw)
    for k, v in kw.items():
        if k in DEFAULT_CONN and v is not None:
            cur[k] = v
    row = s.get(Setting, CONN_KEY)
    if row is None:
        s.add(Setting(key=CONN_KEY, value=cur))
    else:
        row.value = cur
    return cur


def has_credentials(conn: dict) -> bool:
    return bool(conn.get("username") and conn.get("password_enc"))


def get_state(s) -> dict:
    row = s.get(Setting, STATE_KEY)
    return dict((row.value if row else None) or {})


def set_state(s, **kw) -> dict:
    row = s.get(Setting, STATE_KEY)
    cur = {**((row.value if row else None) or {}), **kw}
    if row is None:
        s.add(Setting(key=STATE_KEY, value=cur))
    else:
        row.value = cur
    return cur


# ---------------------------------------------------------------- الواجهة الرسمية (قراءة فقط)

class CrmError(Exception):
    """سبب واضح بالعربية — بلا أي سر."""


def _client() -> httpx.Client:
    return httpx.Client(timeout=40, headers={"User-Agent": UA, "Accept": "application/json"})


def token(conn: dict, client: httpx.Client) -> str:
    from .secretbox import unseal
    pw = unseal(conn.get("password_enc"))
    if not conn.get("username") or not pw:
        raise CrmError("بيانات الدخول إلى الـCRM غير مُدخلة — أدخلها في «إعدادات الربط»")
    try:
        r = client.post(conn["base_url"].rstrip("/") + "/connect/token",
                        data={"grant_type": "password", "client_id": conn["client_id"], "username": conn["username"],
                              "password": pw, "scope": conn["scope"]})
    except httpx.HTTPError as e:
        raise CrmError(f"تعذّر الوصول إلى خادم الـCRM ({type(e).__name__})") from None
    if r.status_code != 200:
        try:
            err = r.json().get("error_description") or r.json().get("error") or ""
        except Exception:  # noqa: BLE001
            err = ""
        if "grant" in err.lower() or "unsupported" in err.lower() or "client" in err.lower():
            raise CrmError("الـCRM لا يسمح بالدخول الآلي بكلمة المرور لهذا التطبيق — اطلب من مزوّد الـCRM تفعيل "
                           "«password grant» لعميل CRM_App أو مفتاح ربط، أو استخدم استيراد ملف Excel")
        raise CrmError(f"رفض الـCRM الدخول (HTTP {r.status_code}) — تحقق من اسم المستخدم وكلمة المرور")
    tok = (r.json() or {}).get("access_token")
    if not tok:
        raise CrmError("لم يُرجع الـCRM رمز دخول")
    return tok


def fetch_page(conn: dict, client: httpx.Client, tok: str, skip: int, n: int = PAGE) -> tuple[list[dict], int]:
    base, ent = conn["base_url"].rstrip("/"), conn.get("entity") or "contact"
    h = {"Authorization": f"Bearer {tok}"}
    try:
        r = client.get(f"{base}/api/app/{ent}", params={"SkipCount": skip, "MaxResultCount": n, "Sorting": "creationTime"},
                       headers=h)
        if r.status_code in (404, 405):
            r = client.post(f"{base}/api/app/{ent}/get-paged", json={"skipCount": skip, "maxResultCount": n}, headers=h)
    except httpx.HTTPError as e:
        raise CrmError(f"انقطع الاتصال بالـCRM أثناء السحب ({type(e).__name__})") from None
    if r.status_code == 401:
        raise CrmError("انتهت صلاحية الدخول إلى الـCRM أثناء السحب")
    if r.status_code == 403:
        raise CrmError("حساب الربط لا يملك صلاحية قراءة العملاء في الـCRM")
    if r.status_code != 200:
        raise CrmError(f"ردّ الـCRM بخطأ HTTP {r.status_code}")
    data = r.json()
    if isinstance(data, list):
        return data, len(data)
    return list(data.get("items") or []), int(data.get("totalCount") or 0)


# ---------------------------------------------------------------- توحيد السجل

def _flat(d, prefix="") -> dict:
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{str(k).strip().lower()}"
        if isinstance(v, dict):
            out.update(_flat(v, key + "."))
            for kk in ("name", "displayname", "text", "value"):  # الحالة كقائمة: {"id":2,"name":"عميل"}
                if kk in {str(x).lower() for x in v}:
                    out.setdefault(key, next(vv for k2, vv in v.items() if str(k2).lower() == kk))
        elif isinstance(v, list):
            if v and all(isinstance(x, (str, int, float)) for x in v):
                out[key] = "، ".join(str(x) for x in v)
            elif v and all(isinstance(x, dict) for x in v):  # ملاحظات كقائمة كائنات
                texts = [str(x.get("text") or x.get("note") or x.get("content") or x.get("body") or "").strip() for x in v]
                if any(texts):
                    out[key] = "\n".join(t for t in texts if t)
        elif v is not None:
            out[key] = v
    return out


_EMPTY = {"", "null", "none", "nan", "n/a", "-", "—"}


def _pick(f: dict, names: list[str]) -> str:
    for n in names:
        v = f.get(n.lower())
        if v is None or isinstance(v, (dict, list)):
            continue
        t = re.sub(r"<[^>]+>", " ", str(v)).strip()
        if t.lower() not in _EMPTY:  # «NULL» في ملفات التصدير = خانة فارغة
            return t
    return ""


def _all_phones(f: dict) -> list[str]:
    out = []
    for n in FIELDS["phone"]:
        v = _pick(f, [n])
        out += [p for p in re.split(r"[;,/|\n،]+", v) if p.strip()]
    return list(dict.fromkeys(out))


def to_record(item: dict) -> dict:
    f = _flat(item)
    contact = _pick(f, FIELDS["contact"]) or " ".join(x for x in (_pick(f, FIELDS["first"]), _pick(f, FIELDS["last"])) if x)
    company = _pick(f, FIELDS["company"])
    phones = _all_phones(f)
    cars = _pick(f, FIELDS["cars"])
    notes = _pick(f, FIELDS["notes"])
    if cars and cars not in ("0", "0.0"):
        notes = (f"عدد السيارات: {cars}" + (f"\n{notes}" if notes else ""))
    return {"crm_id": _pick(f, FIELDS["crm_id"]), "number": _pick(f, FIELDS["number"]),
            "company": company or contact, "contact": contact if contact != company else "",
            "phones": phones, "email": _pick(f, FIELDS["email"]), "city": _pick(f, FIELDS["city"]),
            "status": _pick(f, FIELDS["status"]), "notes": notes, "cars": cars,
            "owner": _pick(f, FIELDS["owner"]), "owner_email": _pick(f, FIELDS["owner_email"])}


def display_number(c: Company) -> str:
    return c.crm_number or c.crm_id or ""


# ---------------------------------------------------------------- الإدراج والتحديث

def _phone(raw: str) -> str | None:
    d = re.sub(r"[\s\-()]", "", str(raw or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
    if re.fullmatch(r"\d+\.0", d):
        d = d[:-2]
    if re.fullmatch(r"[15]\d{8}", d):
        d = "0" + d
    return normalize_phone(d)


def _users(s) -> dict:
    out = {}
    for u in s.scalars(select(User).where(User.active.is_(True))):
        out[normalize_name(u.name)] = u.id
        out[(u.email or "").lower()] = u.id
    return out


def upsert(s, rec: dict, source: str, users: dict | None = None) -> str:
    """يعيد added / updated / linked، أو يرفع CrmError بسبب الفشل."""
    from .companies import log_activity
    cid = str(rec.get("crm_id") or "").strip()
    if not cid:
        raise CrmError("بلا رقم عميل في الـCRM")
    name = (rec.get("company") or rec.get("contact") or "").strip()
    phones = list(dict.fromkeys(p for p in (_phone(x) for x in rec.get("phones") or []) if p))
    email = normalize_email(rec.get("email")) if "@" in (rec.get("email") or "") else None
    if not name and not phones and not email:
        raise CrmError("بلا اسم ولا جوال ولا بريد")
    comp = s.scalar(select(Company).where(Company.crm_id == cid).limit(1))
    result = "updated"
    if comp is None:  # مطابقة مؤكدة مع عميل موجود في المنصة: الجوال أو البريد فقط (لا الاسم وحده)
        for kind, vals in (("phone", phones), ("email", [email] if email else [])):
            for v in vals:
                ct = s.scalar(select(Contact).where(Contact.kind == kind, Contact.value == v))
                if ct is not None and ct.company.crm_id in (None, cid):
                    comp, result = ct.company, "linked"
                    break
            if comp is not None:
                break
    now = now_utc()
    if comp is None:
        comp = Company(name=(name or phones[0] if phones else name or email or cid)[:300],
                       name_norm=normalize_name(name or cid)[:300], city=(rec.get("city") or "")[:80],
                       data_status="complete" if (phones or email) else "incomplete", stage="new",
                       lead_source="crm", is_existing_customer=True, first_found_at=now, last_seen_at=now)
        s.add(comp)
        s.flush()
        result = "added"
        log_activity(s, comp.id, "import", "نُسخ من الـCRM", f"رقم العميل في الـCRM: {rec.get('number') or cid}")
    elif result == "linked":
        log_activity(s, comp.id, "import", "رُبط بعميل الـCRM",
                     f"رقم العميل في الـCRM: {rec.get('number') or cid} — بقي المندوب والملاحظات وسجل الاتصال كما هي")
    comp.crm_id, comp.crm_source, comp.crm_synced_at = cid, source, now
    comp.crm_number = (rec.get("number") or cid)[:80]
    comp.crm_status = rec.get("status") or None
    comp.crm_contact_name = rec.get("contact") or None
    comp.crm_owner_name = rec.get("owner") or None
    comp.crm_notes = rec.get("notes") or None
    comp.crm_raw = {k: v for k, v in rec.items() if k != "phones"} | {"phones": rec.get("phones") or []}
    comp.is_existing_customer = True
    if not comp.city and rec.get("city"):
        comp.city = rec["city"][:80]
    if comp.owner_id is None and rec.get("owner"):  # لا يُغيَّر مندوب المنصة أبدًا؛ يُعيَّن فقط لعميل بلا مندوب
        users = users if users is not None else _users(s)
        uid = users.get((rec.get("owner_email") or "").lower()) or users.get(normalize_name(rec["owner"]))
        if uid:
            comp.owner_id, comp.assigned_at = uid, now
    url = f"https://crm.syarahapp.sa/contact/{cid}"
    for kind, vals in (("phone", phones), ("email", [email] if email else [])):
        for v in vals:
            if s.scalar(select(Contact.id).where(Contact.kind == kind, Contact.value == v)) is None:
                s.add(Contact(company_id=comp.id, kind=kind, value=v, raw_value=v, source_url=url, source_type="crm",
                              verified=True, label=(rec.get("contact") or None) and rec["contact"][:80]))
                s.flush()
    # الاستبعاد من عدّاد العملاء المحتملين الجدد
    for old in s.scalars(select(ExistingCustomer).where(ExistingCustomer.crm_id == cid)):
        s.delete(old)
    s.flush()
    keys = [(None, None)] + [(p, None) for p in phones] + ([(None, email)] if email else [])
    for p, e in keys:
        s.add(ExistingCustomer(name=(name or cid)[:300], name_norm=normalize_name(name or cid)[:300], phone=p, email=e,
                               crm_id=cid))
    s.flush()
    return result


def _empty_stats() -> dict:
    return {"added": 0, "updated": 0, "linked": 0, "failed": 0, "failures": [], "seen": 0}


def _apply(s, items: list[dict], source: str, stats: dict, users: dict, progress=None) -> None:
    for n, item in enumerate(items, 1):
        if progress and n % 500 == 0:
            progress(n, len(items))
        rec = item if "phones" in item and "crm_id" in item else to_record(item)
        stats["seen"] += 1
        try:
            with s.begin_nested():
                out = upsert(s, rec, source, users)
            stats["updated" if out == "linked" else out] += 1
            if out == "linked":
                stats["linked"] += 1
        except CrmError as e:
            stats["failed"] += 1
            if len(stats["failures"]) < 200:
                stats["failures"].append({"crm_id": rec.get("crm_id") or "—", "name": (rec.get("company") or "")[:80],
                                          "reason": str(e)})
        except Exception as e:  # noqa: BLE001 — سجل واحد معطوب لا يوقف السحب
            stats["failed"] += 1
            if len(stats["failures"]) < 200:
                stats["failures"].append({"crm_id": rec.get("crm_id") or "—", "name": (rec.get("company") or "")[:80],
                                          "reason": f"خطأ داخلي ({type(e).__name__})"})


def sync_api(limit: int | None = None, client: httpx.Client | None = None) -> dict:
    """السحب من الواجهة الرسمية. limit = عينة (أول N عميل)."""
    with session_scope() as s:
        conn = get_conn(s)
        set_state(s, running=True, started_at=now_utc().isoformat(), error=None, requested=None)
    stats = _empty_stats()
    total = 0
    try:
        with (client or _client()) as cl:
            tok = token(conn, cl)
            skip = 0
            while True:
                n = min(PAGE, limit - skip) if limit else PAGE
                if n <= 0:
                    break
                items, total = fetch_page(conn, cl, tok, skip, n)
                if skip == 0 and items:
                    with session_scope() as s:
                        set_state(s, sample_fields=sorted(_flat(items[0]).keys())[:80])
                with session_scope() as s:
                    _apply(s, items, "api", stats, _users(s))
                skip += len(items)
                with session_scope() as s:
                    set_state(s, progress={"done": skip, "total": total})
                if not items or len(items) < n or (total and skip >= total):
                    break
    except CrmError as e:
        with session_scope() as s:
            set_state(s, running=False, error=str(e), error_at=now_utc().isoformat())
        raise
    with session_scope() as s:
        crm_total = s.scalar(select(__import__("sqlalchemy").func.count(Company.id)).where(Company.crm_id.isnot(None)))
        set_state(s, running=False, last_at=now_utc().isoformat(), source="api", sample=bool(limit),
                  last={**stats, "crm_total": total}, crm_customers=crm_total, error=None)
    return stats


FILE_QUEUE_KEY = "crm_file_queue"
BIG_FILE = 400_000  # بايت — الأكبر يُستورد في الخلفية عبر العامل حتى لا تنتظر الصفحة


def queue_file(filename: str, data: bytes) -> None:
    """يحفظ الملف الكبير ليستورده العامل خلال دقيقة، مع تقدّم يظهر في الصفحة."""
    import base64
    from .prospects import _sheets
    _sheets(filename, data)  # يتحقق أن الملف مقروء قبل قبوله
    with session_scope() as s:
        row = s.get(Setting, FILE_QUEUE_KEY)
        v = {"name": filename[:200], "b64": base64.b64encode(data).decode(), "at": now_utc().isoformat()}
        if row is None:
            s.add(Setting(key=FILE_QUEUE_KEY, value=v))
        else:
            row.value = v
        set_state(s, file_queued=filename[:200], error=None)


def run_file_queue() -> bool:
    import base64
    with session_scope() as s:
        row = s.get(Setting, FILE_QUEUE_KEY)
        v = dict((row.value if row else None) or {})
        if not v.get("b64"):
            return False
        row.value = {}  # يُسحب من الطابور قبل البدء (لا تكرار إن تعطل العامل)
        set_state(s, running=True, started_at=now_utc().isoformat(), progress={"done": 0, "total": 0}, file_queued=None)

    def prog(done, total):
        with session_scope() as s2:
            set_state(s2, progress={"done": done, "total": total}, started_at=now_utc().isoformat())
    try:
        import_file(v["name"], base64.b64decode(v["b64"]), progress=prog)
    except Exception as e:  # noqa: BLE001
        log.exception("crm file import failed")
        with session_scope() as s:
            set_state(s, error=f"تعذّر استيراد الملف ({type(e).__name__}: {str(e)[:120]})")
    finally:
        with session_scope() as s:
            set_state(s, running=False, progress=None)
    return True


def import_file(filename: str, data: bytes, progress=None) -> dict:
    """استيراد ملف مصدَّر من الـCRM (Excel أو CSV) بالقواعد نفسها."""
    from .prospects import UploadError, _sheets
    sheets = _sheets(filename, data)
    items = []
    for rows in sheets:
        rows = [[str(c or "").strip() for c in r] for r in rows if any(str(c or "").strip() for c in r)]
        if not rows:
            continue
        hdr_i = next((i for i, r in enumerate(rows[:15])
                      if any(str(c).strip().lower() in FIELDS["crm_id"] + FIELDS["company"] + FIELDS["phone"] for c in r)), None)
        if hdr_i is None:
            continue
        hdr = [str(c).strip().lower() for c in rows[hdr_i]]
        for r in rows[hdr_i + 1:]:
            item = {hdr[j]: r[j] for j in range(min(len(hdr), len(r))) if hdr[j]}
            items.append(to_record(item))
    if not items:
        raise UploadError("لم نجد صف عناوين يعرّف العملاء — يلزم عمود «رقم العميل» مع اسم الشركة أو الجوال")
    stats = _empty_stats()
    with session_scope() as s:
        _apply(s, items, "file", stats, _users(s), progress)
    with session_scope() as s:
        crm_total = s.scalar(select(__import__("sqlalchemy").func.count(Company.id)).where(Company.crm_id.isnot(None)))
        set_state(s, last_at=now_utc().isoformat(), source="file", file_name=filename[:200], sample=False,
                  last={**stats, "crm_total": len(items)}, crm_customers=crm_total, error=None)
    return stats


def test_connection(client: httpx.Client | None = None) -> dict:
    """يتحقق من الدخول ويقرأ ٣ عملاء فقط لعرض أسماء الحقول — بلا حفظ."""
    with session_scope() as s:
        conn = get_conn(s)
    with (client or _client()) as cl:
        tok = token(conn, cl)
        items, total = fetch_page(conn, cl, tok, 0, 3)
    fields = sorted(_flat(items[0]).keys())[:80] if items else []
    with session_scope() as s:
        set_state(s, sample_fields=fields, tested_at=now_utc().isoformat(), tested_total=total, error=None)
    return {"total": total, "fields": fields, "preview": [to_record(i) for i in items]}


def tick() -> bool:
    """العامل: سحب طلبته الإدارة، أو تحديث يومي تلقائي."""
    from .jobs import job_lock
    with session_scope() as s:
        queued = bool(((s.get(Setting, FILE_QUEUE_KEY) or Setting(value={})).value or {}).get("b64"))
    if queued:
        with job_lock("crm_file", minutes=60) as ok:
            if ok:
                return run_file_queue()
    with session_scope() as s:
        conn, st = get_conn(s), get_state(s)
        if not has_credentials(conn):
            return False
        req = st.get("requested")
        last = st.get("last_at")
        due = conn.get("daily") and (not last or __import__("datetime").datetime.fromisoformat(last) < now_utc() - timedelta(hours=24))
        if st.get("running") and st.get("started_at") and \
                __import__("datetime").datetime.fromisoformat(st["started_at"]) > now_utc() - timedelta(minutes=30):
            return False
    if not req and not due:
        return False
    with job_lock("crm_sync", minutes=30) as ok:
        if not ok:
            return False
        try:
            sync_api(limit=int(req["limit"]) if isinstance(req, dict) and req.get("limit") else None)
        except CrmError as e:
            log.warning("crm sync failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.exception("crm sync crashed")
            with session_scope() as s:
                set_state(s, running=False, error=f"خطأ داخلي ({type(e).__name__})")
    return True


def visible_query(s, user, q: str = ""):
    from .permissions import visible_owner_ids
    stmt = select(Company).where(Company.crm_id.isnot(None))
    ids = visible_owner_ids(s, user)
    if ids is not None:
        stmt = stmt.where(Company.owner_id.in_(ids or [0]))
    q = (q or "").strip()
    if q:
        conds = [Company.name.ilike(f"%{q}%"), Company.crm_id == q, Company.crm_number == q,
                 Company.crm_contact_name.ilike(f"%{q}%")]
        p = _phone(q)
        if p:
            conds.append(Company.id.in_(select(Contact.company_id).where(Contact.kind == "phone", Contact.value == p)))
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 6:
            conds.append(Company.id.in_(select(Contact.company_id).where(Contact.kind == "phone",
                                                                          Contact.value.like(f"%{digits[-9:]}%"))))
        stmt = stmt.where(or_(*conds))
    return stmt
