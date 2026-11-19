"""حذف البيانات التجريبية كاملة بطلب الإدارة: ٥٠٠ عميل تجريبي + ١٠٠ شركة تدريبية وكل ما يتعلق بها
(رسائل، مكالمات، اجتماعات، مهام، تنبيهات، إشارات، طلبات)، والحملة والعروض التدريبية، وحساب الموظف التجريبي.
البيانات الحقيقية (is_test = False) لا تُمس.  التشغيل: python scripts/purge_test_data.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import func, select, update  # noqa: E402

from mandoob.db import init_db, session_scope  # noqa: E402
from mandoob.models import (Activity, Call, Campaign, Category, Company, CompanyRelation, CompanySignal,  # noqa: E402
                            Consent, DiscoveryRun, InboundLead, InboundMessage, LoginCode, Meeting, Notification,
                            Offer, OutboundMessage, Setting, Suppression, Task, User)

init_db()
with session_scope() as s:
    real_before = s.scalar(select(func.count(Company.id)).where(Company.is_test.is_(False)))
    ids = [i for (i,) in s.execute(select(Company.id).where(Company.is_test.is_(True)))]
    print("PURGE test_companies", len(ids), "| real_companies_before", real_before, flush=True)
    s.execute(update(OutboundMessage).where(OutboundMessage.company_id.in_(ids)).values(parent_id=None))
    for model in (Notification, Activity, Task, Meeting, Call, CompanySignal, Consent, InboundLead, InboundMessage,
                  OutboundMessage, Suppression):
        n = s.query(model).filter(model.company_id.in_(ids)).delete(synchronize_session=False)
        print("PURGE", model.__tablename__, n, flush=True)
    s.query(CompanyRelation).filter(CompanyRelation.company_id.in_(ids) | CompanyRelation.related_id.in_(ids)) \
        .delete(synchronize_session=False)
    s.execute(update(Company).where(Company.parent_id.in_(ids)).values(parent_id=None))
    for c in s.scalars(select(Company).where(Company.id.in_(ids))):
        s.delete(c)  # جهات الاتصال والمصادر تُحذف معها
    s.flush()
    camps = list(s.scalars(select(Campaign).where(Campaign.name.like("حملة تدريبية%") |
                                                 Campaign.name.like("حملة اختبار الحماية%"))))
    for camp in camps:
        if s.scalar(select(func.count(Company.id)).where(Company.campaign_id == camp.id)):
            continue
        s.query(DiscoveryRun).filter(DiscoveryRun.campaign_id == camp.id).delete(synchronize_session=False)
        s.query(Category).filter(Category.campaign_id == camp.id).delete(synchronize_session=False)
        s.delete(camp)
    print("PURGE training_campaigns", len(camps), flush=True)
    offers = [o for o in s.scalars(select(Offer)) if "__training__" in (o.segments or [])]
    for o in offers:
        used = s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.offer_id == o.id)) or \
            s.scalar(select(func.count(Category.id)).where(Category.offer_id == o.id))
        if not used:
            s.delete(o)
    print("PURGE training_offers", len(offers), flush=True)
    for u in s.scalars(select(User).where(User.email.like("%@syarahapp.invalid"))):
        s.query(LoginCode).filter(LoginCode.user_id == u.id).delete(synchronize_session=False)
        s.query(Notification).filter(Notification.user_id == u.id).delete(synchronize_session=False)
        u.active = False
        try:
            with s.begin_nested():
                s.delete(u)
            print("PURGE test_user_deleted", u.email, flush=True)
        except Exception:  # noqa: BLE001 — مرتبط بسجل التدقيق: يبقى موقوفًا
            print("PURGE test_user_kept_deactivated (audit log)", flush=True)
    row = s.get(Setting, "demo_view")
    if row:
        row.value = {"on": False}
with session_scope() as s:
    print("PURGE after test_companies", s.scalar(select(func.count(Company.id)).where(Company.is_test.is_(True))),
          "| real_companies", s.scalar(select(func.count(Company.id)).where(Company.is_test.is_(False))), flush=True)
