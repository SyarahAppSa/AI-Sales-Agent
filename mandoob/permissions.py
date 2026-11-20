"""صلاحيات الموظفين: نطاق الاطلاع، البيانات الظاهرة، والإجراءات المسموحة — يتحقق منها الخادم في كل طلب."""
from __future__ import annotations

from sqlalchemy import select

from .models import Company, User

SCOPES = {"own": "العملاء المسندون إليه فقط", "team": "عملاء فريقه", "all": "جميع العملاء"}

DATA_PERMS = {
    "see_phone": "رقم الهاتف",
    "see_email": "البريد الإلكتروني",
    "see_offers": "العروض المرسلة",
    "see_replies": "ردود العملاء",
    "see_reports": "تقارير المبيعات",
}
ACTION_PERMS = {
    "add_note": "إضافة ملاحظة",
    "update_status": "تحديث حالة العميل والمتابعة",
    "send_offer": "إرسال عرض",
    "export": "تصدير تقرير إلى Excel",
    "emergency": "لوحة طوارئ (محاولات التنزيل وحالة الخوادم والسجل)",
}
# اسم العميل ونشاطه يظهران دائمًا لمن يحق له الاطلاع على السجل

PROFILES = {
    "sales_rep": {"label": "مندوب مبيعات", "scope": "own",
                  "perms": {"see_phone": True, "see_email": True, "see_offers": True, "see_replies": True,
                            "see_reports": False, "add_note": True, "update_status": True, "send_offer": False,
                            "export": False}},
    "view_only": {"label": "اطلاع فقط", "scope": "own",
                  "perms": {"see_phone": True, "see_email": True, "see_offers": True, "see_replies": True,
                            "see_reports": False, "add_note": False, "update_status": False, "send_offer": False,
                            "export": False}},
    "team_lead": {"label": "قائد فريق", "scope": "team",
                  "perms": {k: True for k in list(DATA_PERMS) + list(ACTION_PERMS)}},
    "custom": {"label": "مخصص", "scope": "own", "perms": {}},
}

ADMIN_ROLES = ("admin", "manager")


def is_admin(user: User) -> bool:
    return user.role in ADMIN_ROLES


def can(user: User, perm: str) -> bool:
    if is_admin(user):
        return True
    return bool((user.perms or {}).get(perm))


def visible_owner_ids(s, user: User) -> list[int] | None:
    """None = بلا قيد (الكل)."""
    if is_admin(user) or user.scope == "all":
        return None
    if user.scope == "team" and user.team:
        return [u.id for u in s.scalars(select(User).where(User.team == user.team))]
    return [user.id]


def scoped(q, s, user: User):
    ids = visible_owner_ids(s, user)
    return q if ids is None else q.where(Company.owner_id.in_(ids))


def can_view_company(s, user: User, company: Company | None) -> bool:
    if company is None:
        return False
    ids = visible_owner_ids(s, user)
    return ids is None or company.owner_id in ids


def describe(user: User) -> dict:
    p = user.perms or {}
    return {"scope": SCOPES.get(user.scope, user.scope),
            "data": [v for k, v in DATA_PERMS.items() if p.get(k)],
            "actions": [v for k, v in ACTION_PERMS.items() if p.get(k)] or ["الاطلاع فقط"]}


def backfill_default_perms(s) -> int:
    """الموظفون بلا صلاحيات محددة (أُضيفوا قبل البوابات) يأخذون إعداد «مندوب مبيعات»."""
    n = 0
    for u in s.scalars(select(User).where(User.role == "rep")):
        if not u.perms:
            u.profile, u.scope, u.perms = "sales_rep", "own", dict(PROFILES["sales_rep"]["perms"])
            n += 1
    return n
