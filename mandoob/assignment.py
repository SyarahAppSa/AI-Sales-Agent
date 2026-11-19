"""توزيع الشركات على المندوبين: مندوب مسؤول واحد لكل شركة،
حسب القطاع والمدينة، ثم الأقل حملًا من الفرص المفتوحة."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Company, User

OPEN_STAGES = ("new", "contacted", "offer_sent", "needs_followup", "replied", "interested", "meeting")


def _load(s: Session, user_id: int) -> int:
    return s.scalar(select(func.count(Company.id)).where(Company.owner_id == user_id,
                                                         Company.stage.in_(OPEN_STAGES))) or 0


def pick_owner(s: Session, company: Company) -> User | None:
    reps = list(s.scalars(select(User).where(User.role == "rep", User.active.is_(True))))
    if not reps:
        return None

    def matches(u: User) -> int:
        score = 0
        if u.sectors:
            if company.sector_key not in u.sectors:
                return -1
            score += 2
        if u.cities:
            if company.city not in u.cities:
                return -1
            score += 1
        return score

    scored = [(matches(u), u) for u in reps]
    eligible = [(m, u) for m, u in scored if m >= 0]
    if not eligible:
        eligible = [(0, u) for u in reps]  # لا يوجد تخصص مطابق: يوزع على الجميع
    best_match = max(m for m, _ in eligible)
    pool = [u for m, u in eligible if m == best_match]
    loads = [(_load(s, u.id), u.id, u) for u in pool]
    loads.sort(key=lambda x: (x[0] >= x[2].max_open_leads, x[0], x[1]))
    return loads[0][2]


def assign_owner(s: Session, company: Company, *, force_user_id: int | None = None) -> User | None:
    from .companies import log_activity
    if company.owner_id and not force_user_id:
        return company.owner
    user = s.get(User, force_user_id) if force_user_id else pick_owner(s, company)
    if user is None:
        return None
    company.owner_id = user.id
    s.flush()
    log_activity(s, company.id, "assigned", f"أُسندت الشركة إلى المندوب {user.name}")
    return user
