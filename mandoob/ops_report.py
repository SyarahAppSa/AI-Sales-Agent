"""تقرير المتابعة الدوري (كل بضع ساعات): من دخل ومن خرج، ونشاط الفريق مع العملاء، والجمع والتكلفة، والبريد،
ومحاولات التنزيل، والأعطال وبلاغات الموظفين. يُشغَّل على الخادم: python -m mandoob.ops_report --hours 5
لا يطبع رموز دخول ولا أسرارًا ولا بيانات تواصل العملاء — أسماء وأعداد فقط."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import func, select


def build(hours: float = 5.0) -> list[str]:
    from . import goal
    from .ai import CAP_LABELS, cap_category, get_caps
    from .contact import STATUSES
    from .db import session_scope
    from .models import (Activity, AuditLog, Company, ContactLog, CostEntry, CustomerMail, DirectoryRun, DiscoveryRun,
                         DownloadAttempt, Incident, LoginCode, Notification, ProspectRun, Setting, User)
    from .timeutil import fmt, now_utc, riyadh_today, to_riyadh
    now = now_utc()
    t0 = now - timedelta(hours=hours)
    today = riyadh_today()
    L: list[str] = []
    hm = lambda d: to_riyadh(d).strftime("%H:%M") if d else "—"  # noqa: E731
    with session_scope() as s:
        L.append(f"تقرير المتابعة — آخر {hours:g} ساعات (حتى {fmt(now)} بتوقيت الرياض)")
        # الدخول والخروج
        logs = list(s.scalars(select(AuditLog).where(AuditLog.at >= t0, AuditLog.action.in_(("login", "logout")))
                              .order_by(AuditLog.at)))
        ins = [a for a in logs if a.action == "login"]
        outs = [a for a in logs if a.action == "logout"]
        L.append(f"\n[الدخول والخروج] دخول {len(ins)} مرة من {len({a.actor_id for a in ins})} مستخدم، خروج {len(outs)}")
        for a in logs[-25:]:
            p = (a.details or {}).get("portal")
            how = (a.details or {}).get("method")
            L.append(f"  {hm(a.at)} {'دخل' if a.action == 'login' else 'خرج'}: {a.actor_name}"
                     f" ({'الإدارة' if p == 'admin' else 'الموظفين'}{' — ' + how if how else ''})")
        codes = s.scalar(select(func.count(LoginCode.id)).where(LoginCode.created_at >= t0)) or 0
        L.append(f"  أكواد دخول طُلبت: {codes}")
        users = list(s.scalars(select(User).where(User.active.is_(True))))
        L.append(f"  الحسابات النشطة: {len(users)} (مديرون {sum(u.role in ('admin', 'manager') for u in users)}،"
                 f" مندوبون {sum(u.role == 'rep' for u in users)})")
        # إجراءات إدارية
        adm = Counter(a.action for a in s.scalars(select(AuditLog).where(
            AuditLog.at >= t0, AuditLog.action.notin_(("login", "logout")))))
        if adm:
            L.append("  إجراءات إدارية: " + "، ".join(f"{k} {v}" for k, v in adm.most_common(8)))
        # نشاط الفريق مع العملاء
        cl = list(s.execute(select(ContactLog, Company.is_test).join(Company, Company.id == ContactLog.company_id)
                            .where(ContactLog.at >= t0, ContactLog.kind != "start")))
        cl = [c for c, test in cl if not test]
        per = defaultdict(Counter)
        for c in cl:
            per[c.user_name or "—"][c.status] += 1
        L.append(f"\n[التواصل مع العملاء] {len(cl)} تحديث على {len({c.company_id for c in cl})} عميل")
        for name, cnt in sorted(per.items(), key=lambda x: -sum(x[1].values())):
            L.append(f"  {name}: " + "، ".join(f"{STATUSES.get(k, k)} {v}" for k, v in cnt.most_common()))
        assigned = s.scalar(select(func.count(Activity.id)).where(Activity.at >= t0, Activity.kind == "assigned")) or 0
        L.append(f"  إسنادات/نقل: {assigned}")
        due = s.scalar(select(func.count(Company.id)).where(Company.next_followup_at.isnot(None),
                                                            Company.next_followup_at < now + timedelta(hours=24),
                                                            Company.is_test.is_(False))) or 0
        L.append(f"  متابعات مستحقة خلال 24 ساعة (أو متأخرة): {due}")
        # العملاء والجمع
        new_co = s.scalar(select(func.count(Company.id)).where(Company.first_found_at >= t0, Company.is_test.is_(False))) or 0
        crm = s.scalar(select(func.count(Company.id)).where(Company.crm_id.isnot(None))) or 0
        total = s.scalar(select(func.count(Company.id)).where(Company.is_test.is_(False))) or 0
        L.append(f"\n[العملاء] إجمالي المنشآت {total:,}، منها عملاء CRM {crm:,}. جديدة في الفترة: {new_co}")
        L.append(f"  هدف اليوم: {goal.achieved(s)} من {goal.target(s)} منشأة جديدة مكتملة")
        for M, lab in ((DirectoryRun, "اكتشاف"), (ProspectRun, "محتملون")):
            for r in s.scalars(select(M).where(M.created_at >= t0).order_by(M.id)):
                st = r.stats or {}
                comp = sum((v or {}).get("complete", 0) for k, v in st.items() if isinstance(v, dict) and not k.startswith("_"))
                last = (r.log or [""])[-1][:110]
                L.append(f"  دفعة {lab} #{r.id} ({r.status}) {hm(r.created_at)}: مكتملة {comp} — {last}")
        # التكلفة
        spent = goal.cost_today(s)
        cap = float(goal.get(s).get("cost_cap_usd") or 0)
        by = defaultdict(float)
        for nt, c in s.execute(select(CostEntry.note, CostEntry.cost_usd).where(CostEntry.day == today)):
            by[cap_category(nt or "") or "أخرى"] += float(c or 0)
        caps = get_caps(s)
        L.append(f"\n[التكلفة اليوم] ${spent:.2f} من حد ${cap:.0f}"
                 + (f" — تكلفة المنشأة المكتملة ≈ ${spent / goal.achieved(s):.2f}" if goal.achieved(s) else ""))
        for k, v in sorted(by.items(), key=lambda x: -x[1]):
            L.append(f"  {CAP_LABELS.get(k, k)}: ${v:.2f}" + (f" (سقف ${caps[k]:.0f})" if k in caps else ""))
        # البريد
        cm_out = s.scalar(select(func.count(CustomerMail.id)).where(CustomerMail.at >= t0, CustomerMail.direction == "out")) or 0
        cm_in = s.scalar(select(func.count(CustomerMail.id)).where(CustomerMail.at >= t0, CustomerMail.direction == "in",
                                                                   CustomerMail.status == "received")) or 0
        L.append(f"\n[بريد العملاء] أُرسل {cm_out}، ردود واردة {cm_in}")
        # التنزيل والأعطال والبلاغات
        da = list(s.scalars(select(DownloadAttempt).where(DownloadAttempt.at >= t0).order_by(DownloadAttempt.at)))
        L.append(f"\n[محاولات التنزيل] {len(da)}" + ("" if not da else ": " + "؛ ".join(
            f"{hm(d.at)} {d.user_name} ← {d.label} ({d.result})" for d in da[-8:])))
        inc = list(s.scalars(select(Incident).where(Incident.at >= t0).order_by(Incident.at)))
        L.append(f"[الأعطال] {len(inc)}" + ("" if not inc else ": " + "؛ ".join(
            f"{hm(i.at)} {i.service}: {i.message[:70]}" for i in inc[-6:])))
        rep = list(s.scalars(select(Notification).where(Notification.created_at >= t0,
                                                        Notification.title.like("بلاغ من%"))))
        seen, reps = set(), []
        for n in rep:
            if n.title not in seen:
                seen.add(n.title)
                reps.append(n.title[:120])
        L.append(f"[بلاغات الموظفين] {len(reps)}" + ("" if not reps else ": " + "؛ ".join(reps)))
        hb = s.get(Setting, "worker_heartbeat")
        hb_at = (hb.value or {}).get("at", "")[:19] if hb else "—"
        L.append(f"[العامل] آخر نبض {hb_at} UTC")
    return L


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=5.0)
    a = ap.parse_args()
    for line in build(a.hours):
        print("OPS " + line.replace("\n", "\nOPS "))
