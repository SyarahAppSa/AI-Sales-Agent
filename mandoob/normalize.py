"""توحيد الأسماء والنطاقات والهواتف والبريد لاكتشاف التكرار."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

_AR_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# كلمات لا تميّز الشركة (تُحذف قبل المقارنة)
_STOP_WORDS = {
    "شركه", "شركة", "مؤسسه", "مؤسسة", "مجموعه", "مجموعة", "المحدوده", "المحدودة", "ذات", "مسؤوليه",
    "مسئوليه", "مسؤولية", "للتجاره", "التجاريه", "فرع", "و", "ال",
    "co", "company", "est", "establishment", "group", "llc", "ltd", "limited", "inc", "the",
    "trading", "corp", "corporation", "sa", "ksa", "saudi", "arabia", "w.l.l", "wll",
}

GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "live.com", "icloud.com", "msn.com",
    "outlook.sa", "hotmail.sa", "yahoo.co.uk", "aol.com", "proton.me", "protonmail.com",
}

# ترتيب تفضيل البريد للعروض التجارية
EMAIL_PREFERENCE = [
    ("procurement", "المشتريات"), ("purchasing", "المشتريات"), ("purchase", "المشتريات"),
    ("business", "تطوير الأعمال"), ("bd", "تطوير الأعمال"), ("sales", "المبيعات"),
    ("partnership", "الشراكات"), ("fleet", "الأسطول"), ("info", "عام"), ("contact", "عام"),
    ("hello", "عام"), ("cs", "خدمة العملاء"), ("care", "خدمة العملاء"),
    ("support", "الدعم"), ("customerservice", "خدمة العملاء"),
]
_PLATFORM_EMAIL_DOMAINS = {"wordpress.com", "wordpress.org", "wix.com", "wixpress.com", "sentry.io", "sentry-next.wixpress.com",
                           "example.com", "example.org", "example.net", "domain.com", "yourdomain.com", "email.com",
                           "mysite.com", "test.com", "godaddy.com", "squarespace.com", "shopify.com", "weebly.com",
                           "sentry.wixpress.com", "w3.org", "schema.org", "jquery.com", "cloudflare.com", "wpengine.com"}
_BAD_EMAIL_PREFIXES = ("noreply", "no-reply", "donotreply", "hr", "careers", "jobs", "recruit",
                       "cv", "privacy", "abuse", "postmaster", "webmaster")


def normalize_arabic(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = _AR_DIACRITICS.sub("", t)
    t = t.translate(_AR_DIGITS)
    t = re.sub("[إأآٱ]", "ا", t)
    t = t.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return t


def normalize_name(name: str) -> str:
    t = normalize_arabic(name).lower()
    t = re.sub(r"[^\w\s]", " ", t)
    words = []
    for w in t.split():
        if w in _STOP_WORDS:
            continue
        if w.startswith("ال") and len(w) > 3:
            w = w[2:]
        if w in _STOP_WORDS or ("ال" + w) in _STOP_WORDS:
            continue
        words.append(w)
    return " ".join(words).strip()


def domain_of(url_or_email: str | None) -> str | None:
    if not url_or_email:
        return None
    s = url_or_email.strip().lower()
    if "@" in s and "/" not in s:
        d = s.split("@", 1)[1]
    else:
        if "://" not in s:
            s = "http://" + s
        d = urlparse(s).hostname or ""
    d = d.strip(".")
    if d.startswith("www."):
        d = d[4:]
    return d or None


def normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    e = email.strip().strip(".,;:()<>[]\"'").lower()
    e = e.replace("mailto:", "")
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", e):
        return None
    dom = e.split("@", 1)[1]
    if dom in _PLATFORM_EMAIL_DOMAINS or any(dom.endswith("." + d) for d in _PLATFORM_EMAIL_DOMAINS) \
            or re.search(r"\.(png|jpe?g|gif|svg|webp)$", e):
        return None  # بريد منصة الاستضافة أو قالب أو اسم صورة — ليس بريد المنشأة
    return e


def email_label(email: str) -> tuple[int, str]:
    """يعيد (رتبة التفضيل، الوصف). الرتبة الأقل = الأفضل للعرض التجاري."""
    local = email.split("@", 1)[0]
    if local.startswith(_BAD_EMAIL_PREFIXES):
        return 99, "غير مناسب للعروض"
    for i, (prefix, label) in enumerate(EMAIL_PREFERENCE):
        if local == prefix or local.startswith(prefix + ".") or local.startswith(prefix + "_") \
                or local.startswith(prefix + "-") or local == prefix + "s":
            return i, label
    return 50, "مباشر"


def is_offer_suitable_email(email: str) -> bool:
    return email_label(email)[0] < 99


def normalize_phone(raw: str | None, default_country: str = "966") -> str | None:
    """يحوّل الرقم إلى صيغة E.164. يعيد None إذا لم يكن رقمًا صالحًا.

    يدعم الأرقام السعودية: 05xxxxxxxx، 011xxxxxxx، 920xxxxxx، 800xxxxxxx، +966...
    """
    if not raw:
        return None
    s = normalize_arabic(raw)
    s = re.sub(r"(?i)(ext|تحويلة|x)\s*\.?\s*\d+$", "", s)
    digits = re.sub(r"[^\d+]", "", s)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+"):
        num = digits[1:]
        if not num.isdigit() or not (8 <= len(num) <= 15):
            return None
        if num.startswith("966"):
            nat = num[3:]
            if nat.startswith("0"):
                nat = nat[1:]
            return _validate_sa("+966" + nat)
        return "+" + num
    if not digits.isdigit():
        return None
    if default_country == "966":
        if digits.startswith("966"):
            return _validate_sa("+" + digits)
        if digits.startswith("920") and len(digits) == 9:
            return _validate_sa("+966" + digits)
        if digits.startswith("800") and len(digits) == 10:
            return _validate_sa("+966" + digits)
        if digits.startswith("0"):
            return _validate_sa("+966" + digits[1:])
        if digits.startswith("5") and len(digits) == 9:
            return _validate_sa("+966" + digits)
    return None


def _validate_sa(e164: str) -> str | None:
    nat = e164[4:]
    if nat.startswith("5") and len(nat) == 9:  # جوال
        return e164
    if nat[:1] in "1234567" and len(nat) == 9:  # أرضي (011، 012، 013 ...)
        return e164
    if nat.startswith("920") and len(nat) == 9:  # الرقم الموحد
        return e164
    if nat.startswith("800") and len(nat) == 10:  # مجاني
        return e164
    return None


def format_phone_display(e164: str | None) -> str:
    """الرقم كاملًا متصلًا كما حُفظ (مثل +966539555610) — برمز الدولة ودون مسافات حتى لا ينقسم على سطرين."""
    if not e164:
        return ""
    return re.sub(r"[\s\-()]", "", e164)


def names_similar(a: str, b: str) -> bool:
    """تطابق تقريبي للأسماء بعد التوحيد (تشابه كلمات ≥ 0.8 أو احتواء كامل)."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    sa, sb = set(na.split()), set(nb.split())
    if not sa or not sb:
        return False
    inter = len(sa & sb)
    jacc = inter / len(sa | sb)
    small, big = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return jacc >= 0.8 or (len(small) >= 2 and small <= big)
