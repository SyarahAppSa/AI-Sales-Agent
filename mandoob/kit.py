"""«ملفات طلبات العملاء»: ما يطلبه العميل أثناء المكالمة (بروفايل، سجل تجاري، رقم ضريبي، روابط...).

- الإدارة ترفع الملفات وتدخل الروابط والأرقام، وتعاين قبل النشر، وتستبدل النسخة أو توقف مشاركتها.
- المندوب يرى المنشور فقط: تحميل/نسخ رابط/نسخ رقم، و«نسخ رسالة للعميل» يرسلها يدويًا.
- التحميل المسموح محصور في هذه المواد المعتمدة (المسار /kit/...)؛ بقية التنزيلات تبقى محظورة.
- رابط المشاركة /kit/s/<رمز> يفتح المستند المعتمد وحده دون حساب، ويتوقف فور إيقاف المشاركة أو إلغاء النشر.
"""
from __future__ import annotations

import secrets
from datetime import date

from sqlalchemy import func, select

from .config import settings
from .models import KitItem, KitLog

KINDS = {"file": "ملف", "number": "رقم + شهادة", "address": "عنوان + إثبات", "template": "نموذج Excel", "app": "روابط تطبيق",
         "link": "رابط", "email": "بريد إلكتروني", "phone": "رقم تواصل", "text": "نص / دليل"}

DEFAULTS = [  # بالترتيب المطلوب
    ("file", "بروفايل سيارة اب التعريفي", ""),
    ("file", "السجل التجاري", "النسخة المعتمدة"),
    ("number", "الرقم الضريبي وشهادة التسجيل الضريبي", ""),
    ("address", "العنوان الوطني", ""),
    ("template", "نموذج Excel لإضافة المركبات", "نموذج فارغ جاهز للتعبئة"),
    ("app", "رابط تطبيق السائق", "آيفون أو أندرويد حسب جهاز العميل"),
    ("link", "رابط خريطة المحطات", ""),
    ("link", "رابط مواقع تركيب الشرائح", ""),
    ("link", "رابط بوابة إدارة الشركات «أدمن الشركات»", "رابط دخول العميل إلى بوابة شركته"),
    ("email", "البريد الإلكتروني الرسمي للشركة", ""),
    ("phone", "الرقم الموحد للتواصل", ""),
    ("text", "دليل بدء استخدام الخدمة", ""),
    ("text", "خطوات تفعيل الشركة وإضافة المركبات", ""),
    ("text", "الأسئلة الشائعة", ""),
]


def new_token() -> str:
    return secrets.token_urlsafe(18)


def ensure_defaults(s) -> None:
    if s.scalar(select(func.count(KitItem.id))):
        return
    for i, (kind, title, note) in enumerate(DEFAULTS, start=1):
        s.add(KitItem(position=i, kind=kind, title=title, note=note, share_token=new_token(), published=False))
    s.flush()


def has_content(it: KitItem) -> bool:
    return bool(it.file_data or (it.value or "").strip() or (it.url or "").strip() or (it.url2 or "").strip()
                or (it.body or "").strip())


def visible_to_reps(it: KitItem) -> bool:
    return bool(it.published and has_content(it))


def expired(it: KitItem) -> bool:
    return bool(it.expires_on and it.expires_on < date.today())


def share_url(it: KitItem) -> str | None:
    """رابط عام للمستند المعتمد وحده (للملفات والنصوص)."""
    if not (it.share_enabled and it.published and (it.file_data or (it.body or "").strip())):
        return None
    return f"{settings.public_base_url.rstrip('/')}/kit/s/{it.share_token}"


def copy_label(it: KitItem) -> str:
    return {"number": "نسخ الرقم", "address": "نسخ العنوان", "email": "نسخ البريد", "phone": "نسخ الرقم"}.get(it.kind, "نسخ")


def message_lines(it: KitItem) -> list[str]:
    """أسطر العنصر في «رسالة للعميل»."""
    out = []
    if (it.value or "").strip():
        out.append(f"{it.title}: {it.value.strip()}")
    if it.kind == "app":
        if it.url:
            out.append(f"{it.title} (آيفون): {it.url}")
        if it.url2:
            out.append(f"{it.title} (أندرويد): {it.url2}")
    elif (it.url or "").strip():
        out.append(f"{it.title}: {it.url.strip()}")
    link = share_url(it)
    if link:
        label = it.title if not out else ("الشهادة" if it.kind == "number" else "إثبات العنوان" if it.kind == "address" else it.title)
        out.append(f"{label}: {link}" if out else f"{it.title}: {link}")
    return out


def log(s, action: str, it: KitItem | None, user=None, company_id: int | None = None) -> None:
    s.add(KitLog(action=action, item_id=it.id if it else None, item_title=(it.title if it else "")[:200],
                 version=it.version if it else 0, company_id=company_id,
                 user_id=user.id if user else None, user_name=(user.name if user else "عميل عبر رابط المشاركة")[:120]))
