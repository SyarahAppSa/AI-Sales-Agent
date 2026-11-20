"""«مواقع التواصل الاجتماعي» في الحملة: ملف Excel أمام كل مصدر ← معاينة داخل المنصة ← «اعتماد واستيراد العملاء».

- المعاينة تعرض الملف كما ورد: الاسم ثم رقم التواصل أولًا، ثم البريد والمدينة والنشاط والملاحظات وبقية الأعمدة.
  الأرقام تُعرض كما كُتبت في الملف (لا تُحذف +966 ولا الصفر)، وكل رقم في سطر.
- الاستيراد منفصل عن الرفع: يربط كل صف بالحملة والمكان المستهدف والمصدر. العميل الموجود (بالهاتف أو البريد أو
  الاسم) لا يُكرَّر: يُضاف إليه المصدر فقط، ويبقى مسؤوله وسجل تواصله وملاحظاته كما هي.
- التواصل يدوي من فريق المبيعات؛ لا إرسال آلي.
"""
from __future__ import annotations

import re

from sqlalchemy import select

from .models import CampaignLead, CampaignSourceFile, Company, CompanySource, Contact
from .normalize import normalize_email, normalize_name, normalize_phone
from .prospects import UploadError, _guess_columns, _map_headers, _sheets, normalize_arabic_digits
from .timeutil import now_utc

SOCIAL_SOURCES = {  # بالترتيب والأسماء المطلوبة
    "snapchat": "سناب شات", "tiktok": "تيك توك", "syarah_site": "الموقع الإلكتروني لسيارة اب", "facebook": "فيس بوك",
    "instagram": "إنستغرام", "x": "إكس", "chatgpt": "شات جي بي تي", "google": "جوجل", "linkedin": "لينكدإن",
}
MAX_ROWS = 20000
NOTE_WORDS = ("ملاحظ", "note", "تعليق", "comment", "remarks")
PHONE_WORDS = ("جوال", "هاتف", "تلفون", "phone", "mobile", "tel", "واتس", "whatsapp", "رقم التواصل", "contact")
_SPLIT = re.compile(r"\s*(?:[;,/|\n،]+|\s[-–]\s|\s{2,})\s*")


def clean(v) -> str:
    """567775573.0 ← 567775573، والتاريخ بلا ساعة. لا يُلمس + ولا الصفر في البداية."""
    v = str(v if v is not None else "").strip()
    if re.fullmatch(r"\+?\d+\.0", v):
        v = v[:-2]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} 00:00:00", v):
        v = v[:10]
    return v


def split_phones(v: str) -> list[str]:
    return [p for p in _SPLIT.split(clean(v)) if p.strip()]


def _is_phoneish(v: str) -> bool:
    d = re.sub(r"[\s\-()+]", "", normalize_arabic_digits(clean(v)))
    return bool(re.fullmatch(r"(?:00966|966|0)?(?:5\d{8}|1\d{8}|9200\d{5}|800\d{7})", d))


def phone_e164(raw: str) -> str | None:
    d = re.sub(r"[\s\-()]", "", normalize_arabic_digits(clean(raw)))
    if re.fullmatch(r"[15]\d{8}", d):
        d = "0" + d
    return normalize_phone(d)


def parse_file(filename: str, data: bytes) -> tuple[list[str], list[list[str]], dict]:
    """يقرأ كل أوراق الملف ويعيد (العناوين، الصفوف، الأعمدة المعروفة). العناوين في أي من أول ١٥ صفًا."""
    headers: list[str] = []
    out: list[list[str]] = []
    for rows in _sheets(filename, data):
        rows = [[clean(c) for c in r] for r in rows if any(clean(c) for c in r)]
        if not rows:
            continue
        hdr_i = None
        for i, r in enumerate(rows[:15]):
            mm = _map_headers(r)
            if ({"name", "phone"} & set(mm)) and len([c for c in r if c]) >= 2 and not any(_is_phoneish(c) for c in r):
                hdr_i = i
                break
        if hdr_i is None:
            g = _guess_columns(rows)
            width = max(len(r) for r in rows)
            sheet_hdr = [""] * width
            labels = {"name": "الاسم", "phone": "رقم التواصل", "email": "البريد الإلكتروني"}
            for k, j in g.items():
                if j < width and k in labels:
                    sheet_hdr[j] = labels[k]
            sheet_hdr = [h or f"عمود {j + 1}" for j, h in enumerate(sheet_hdr)]
            body = rows
        else:
            sheet_hdr = [h or f"عمود {j + 1}" for j, h in enumerate(rows[hdr_i])]
            body = rows[hdr_i + 1:]
        idx = []
        for h in sheet_hdr:  # توحيد أعمدة الأوراق ذات العناوين نفسها
            if h not in headers:
                headers.append(h)
            idx.append(headers.index(h))
        for r in body:
            row = [""] * len(headers)
            for j, v in enumerate(r):
                if j < len(idx):
                    row[idx[j]] = v
            out.append(row)
    out = [r + [""] * (len(headers) - len(r)) for r in out]
    if not out:
        raise UploadError("لم نجد صفوفًا في الملف — تأكد أن فيه عمودًا للاسم أو لرقم التواصل")
    if len(out) > MAX_ROWS:
        raise UploadError(f"الملف أكبر من الحد ({MAX_ROWS} صف) — قسّمه إلى ملفات أصغر")
    return headers, out, detect_columns(headers, out)


def detect_columns(headers: list[str], rows: list[list[str]]) -> dict:
    m = _map_headers(headers)
    low = [h.lower() for h in headers]
    phones = [j for j, h in enumerate(low) if any(w in h for w in PHONE_WORDS)]
    if "phone" in m and m["phone"] not in phones:
        phones.insert(0, m["phone"])
    if not phones:  # بلا عنوان واضح: العمود الذي أغلب قيمه أرقام هواتف
        sample = rows[:300]
        for j in range(len(headers)):
            vals = [r[j] for r in sample if r[j]]
            if vals and sum(1 for v in vals if any(_is_phoneish(p) for p in split_phones(v))) / len(vals) > .6:
                phones.append(j)
    cols = {"phone": sorted(set(phones))}
    for k in ("name", "email", "city", "activity"):
        if k in m and m[k] not in cols["phone"]:
            cols[k] = m[k]
    note = next((j for j, h in enumerate(low) if any(w in h for w in NOTE_WORDS)), None)
    if note is not None:
        cols["note"] = note
    return cols


def view(f: CampaignSourceFile, limit: int | None = None, offset: int = 0) -> dict:
    """جدول المعاينة: الاسم، رقم التواصل (كل رقم في سطر)، البريد، المدينة، النشاط، الملاحظات، ثم بقية الأعمدة."""
    c = f.cols or {}
    phones = c.get("phone") or []
    order: list = []
    used = set(phones)
    if "name" in c:
        order.append(("col", c["name"], "اسم العميل / الشركة"))
        used.add(c["name"])
    else:
        order.append(("empty", None, "اسم العميل / الشركة"))
    order.append(("phones", None, "رقم التواصل"))
    for k, lbl in (("email", "البريد الإلكتروني"), ("city", "المدينة"), ("activity", "النشاط"), ("note", "الملاحظات")):
        if k in c and c[k] not in used:
            order.append(("col", c[k], f.headers[c[k]] if c[k] < len(f.headers) else lbl))
            used.add(c[k])
    for j, h in enumerate(f.headers):
        if j not in used:
            order.append(("col", j, h))
    rows = f.rows or []
    part = rows[offset: offset + limit if limit else None]
    out = []
    for i, r in enumerate(part, start=offset):
        cells = []
        for kind, j, _ in order:
            if kind == "phones":
                cells.append({"phones": [p for jj in phones if jj < len(r) for p in split_phones(r[jj])]})
            elif kind == "empty":
                cells.append({"text": ""})
            else:
                cells.append({"text": r[j] if j < len(r) else ""})
        out.append({"n": i + 1, "cells": cells})
    return {"headers": [o[2] for o in order], "rows": out, "total": len(rows)}


# ---------------------------------------------------------------- الاستيراد

def _existing(s, phones: list[str], email: str | None, name_norm: str) -> Company | None:
    for p in phones:
        ct = s.scalar(select(Contact).where(Contact.kind == "phone", Contact.value == p))
        if ct:
            return ct.company
    if email:
        ct = s.scalar(select(Contact).where(Contact.kind == "email", Contact.value == email))
        if ct:
            return ct.company
    if name_norm:
        return s.scalar(select(Company).where(Company.name_norm == name_norm).order_by(Company.id).limit(1))
    return None


def import_file(s, f: CampaignSourceFile, place: str, user_id: int | None = None) -> dict:
    """يستورد صفوف الملف إلى الحملة. يعيد {new, duplicate, failed, failures}."""
    from .companies import log_activity
    from .models import Campaign
    c = f.cols or {}
    camp = s.get(Campaign, f.campaign_id)
    real_cid = None if camp is not None and camp.kind == "social" else f.campaign_id  # الصفحة المستقلة ليست حملة
    label = SOCIAL_SOURCES.get(f.source_key, f.source_key)
    stats = {"new": 0, "duplicate": 0, "failed": 0, "failures": []}
    url = f"campaign:{f.campaign_id}/file:{f.id}"
    for i, r in enumerate(f.rows or []):
        get = lambda k: (r[c[k]].strip() if k in c and c[k] < len(r) else "")  # noqa: E731
        raw_phones = [p for j in c.get("phone") or [] if j < len(r) for p in split_phones(r[j])]
        pairs: dict[str, str] = {}
        for x in raw_phones:
            if (e := phone_e164(x)) and e not in pairs:
                pairs[e] = x
        phones = list(pairs)
        email = normalize_email(get("email")) if "@" in get("email") else None
        name = get("name")
        def fail(reason):
            stats["failed"] += 1
            stats["failures"].append({"row": i + 1, "reason": reason})
        if not name and not phones and not email:
            fail("صف بلا اسم ولا رقم ولا بريد")
            continue
        if not phones and not email:
            fail(f"رقم التواصل غير صالح أو فارغ ({' / '.join(raw_phones)})" if raw_phones else "لا يوجد رقم تواصل ولا بريد")
            continue
        if not name:
            fail("اسم العميل فارغ")
            continue
        nn = normalize_name(name)
        comp = _existing(s, phones, email, nn)
        if comp is None:
            comp = Company(name=name[:300], name_norm=nn[:300], city=(get("city") or place)[:80],
                           activity=get("activity")[:200], campaign_id=real_cid,
                           data_status="complete" if phones else "incomplete", stage="new",
                           last_note=get("note") or None, last_note_at=now_utc() if get("note") else None)
            s.add(comp)
            s.flush()
            stats["new"] += 1
            log_activity(s, comp.id, "import", f"أُضيف من {label}", f"الملف: {f.file_name} — صف {i + 1}", user_id=user_id)
        else:
            stats["duplicate"] += 1
            if comp.campaign_id is None and real_cid:
                comp.campaign_id = real_cid
        found = [("phone", v, raw) for v, raw in pairs.items()] + ([("email", email, get("email"))] if email else [])
        for kind, v, raw in found:
            if s.scalar(select(Contact.id).where(Contact.kind == kind, Contact.value == v)) is None:
                s.add(Contact(company_id=comp.id, kind=kind, value=v, raw_value=raw[:200],
                              source_url=url, source_type=f"social:{f.source_key}", verified=False))
                s.flush()
        if not s.scalar(select(CompanySource.id).where(CompanySource.company_id == comp.id,
                                                       CompanySource.source_type == f"social:{f.source_key}",
                                                       CompanySource.url == url)):
            s.add(CompanySource(company_id=comp.id, source_type=f"social:{f.source_key}", url=url,
                                snippet=f"{label} — {f.file_name}"[:1000]))
        lead = s.scalar(select(CampaignLead).where(CampaignLead.campaign_id == f.campaign_id,
                                                   CampaignLead.company_id == comp.id,
                                                   CampaignLead.source_key == f.source_key))
        if lead is None:
            s.add(CampaignLead(campaign_id=f.campaign_id, company_id=comp.id, source_key=f.source_key,
                               place=place[:200], file_id=f.id, row_idx=i))
            s.flush()
    stats["failures"] = stats["failures"][:500]
    f.status, f.stats, f.imported_at, f.place = "imported", stats, now_utc(), place[:200]
    return stats


def import_campaign(s, camp, user_id: int | None = None) -> list[tuple[CampaignSourceFile, dict]]:
    files = list(s.scalars(select(CampaignSourceFile).where(CampaignSourceFile.campaign_id == camp.id,
                                                            CampaignSourceFile.status == "staged")
                           .order_by(CampaignSourceFile.id)))
    return [(f, import_file(s, f, camp.target_place or "", user_id)) for f in files]


def campaign_leads(s, cid: int, source: str = "") -> list[dict]:
    """عملاء الحملة مجمّعين: كل عميل مرة واحدة مع كل مصادره."""
    rows = list(s.execute(select(CampaignLead, Company).join(Company, Company.id == CampaignLead.company_id)
                          .where(CampaignLead.campaign_id == cid).order_by(CampaignLead.id)))
    by: dict[int, dict] = {}
    for lead, comp in rows:
        d = by.setdefault(comp.id, {"company": comp, "sources": [], "place": lead.place})
        if lead.source_key not in d["sources"]:
            d["sources"].append(lead.source_key)
    out = list(by.values())
    if source:
        out = [d for d in out if source in d["sources"]]
    return out
