"""اختبار المسار كاملًا على الخادم للمرحلة الحالية (بحث وجمع بيانات فقط).

  prep <حد الدفعة>      ← يطبع الإعدادات، يعيد فحص السجلات بقاعدة «الوسيلة تخص المنشأة»، يحفظ أداة البحث ويطلب دفعة
  wait                  ← ينتظر العامل حتى ينهي الدفعة ثم يطبع: الوقت، المكتملة الجديدة، المكررة، التكلفة، والسجلات
  portal                ← موظف تجريبي: إسناد سجلات، دخول بكود، عرض، تصدير، تحديث حالة، تقرير العروض، ثم التنظيف
  sched <دقائق> <هدف>   ← جدولة تشغيل يومي بعد N دقيقة بهدف صغير (لاختبار التشغيل والمنصة مغلقة)
  schedreport           ← نتيجة التشغيل المجدول
  restore               ← إعادة الهدف ٤٠٠ وإيقاف الجدولة حتى قرار الإدارة
  zero                  ← صفر مكالمات وصفر إيميلات تسويقية
"""
import io
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import func, select  # noqa: E402

from mandoob import jobs, prospecting as P  # noqa: E402
from mandoob.config import settings  # noqa: E402
from mandoob.db import init_db, session_scope  # noqa: E402
from mandoob.models import (Call, Campaign, Company, CostEntry, DiscoveryRun, OutboundMessage, User)  # noqa: E402
from mandoob.timeutil import now_riyadh, riyadh_today, to_riyadh  # noqa: E402

init_db()
cmd = sys.argv[1]
CITIES = ["الرياض", "جدة", "الدمام"]


def zero():
    """المكالمات والرسائل على الشركات الحقيقية (السجلات التدريبية الوهمية فيها مكالمات ورسائل مصطنعة للشرح فقط)."""
    real = select(Company.id).where(Company.is_test.is_(False))
    with session_scope() as s:
        print("E2E zero real_calls", s.scalar(select(func.count(Call.id)).where(Call.company_id.in_(real))),
              "| real_marketing_emails_sent", s.scalar(select(func.count(OutboundMessage.id)).where(
                  OutboundMessage.company_id.in_(real), OutboundMessage.sent_at.isnot(None))),
              "| queued_now", s.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.status == "queued")),
              "| training_demo_calls", s.scalar(select(func.count(Call.id)).where(Call.company_id.notin_(real))),
              "| flags voice", settings.voice_calls_enabled, "outreach", settings.auto_outreach_enabled, flush=True)


if cmd == "prep":
    batch = int(sys.argv[2])
    from mandoob.auth import mail_ready
    from mandoob.companies import is_complete, log_activity, missing_fields
    with session_scope() as s:
        print("E2E mail_ready", mail_ready(s), flush=True)
        moved = []
        for c in s.scalars(select(Company).where(Company.is_test.is_(False), Company.lead_source == "discovery",
                                                 Company.data_status == "complete")):
            if not is_complete(c):
                c.data_status, c.counted_on = "incomplete", None
                log_activity(s, c.id, "data", "نُقل إلى قيد الاستكمال بعد التدقيق", "ينقص: " + "، ".join(missing_fields(c)))
                moved.append(f"#{c.id} {c.name}")
        print("E2E recheck_moved", len(moved), moved, flush=True)
        print("E2E real_complete_now", s.scalar(select(func.count(Company.id)).where(
            Company.is_test.is_(False), Company.lead_source == "discovery", Company.data_status == "complete")), flush=True)
        camp = P.save_config(s, cities=CITIES, regions=[], activities=P.DEFAULT_ACTIVITIES, daily_target=400,
                             scheduled=False, time="06:00")
        s.flush()
        req = P.request_run(s, camp.id, batch, None)
        print("E2E requested", req, "campaign", camp.id, flush=True)
    zero()

elif cmd == "wait":
    t0 = time.time()
    while time.time() - t0 < 3300:
        with session_scope() as s:
            req = P.pending_request(s)
        if req.get("state") in ("done", "failed"):
            break
        time.sleep(20)
    print("E2E request", req, flush=True)
    start = datetime.fromisoformat(req["requested_at"])
    began = datetime.fromisoformat(req.get("started_at") or req["requested_at"])
    end = datetime.fromisoformat(req.get("finished_at") or datetime.utcnow().isoformat())
    with session_scope() as s:
        cid = req["campaign_id"]
        run = s.get(DiscoveryRun, req.get("run_id")) if req.get("run_id") else None
        new = list(s.scalars(select(Company).where(Company.campaign_id == cid, Company.first_found_at >= start)
                             .order_by(Company.id)))
        comp = [c for c in new if c.data_status == "complete"]
        cost = s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(CostEntry.at >= start)) or 0
        steps = dict(s.execute(select(CostEntry.note, func.sum(CostEntry.cost_usd)).where(CostEntry.at >= start)
                               .group_by(CostEntry.note)).all())
        print("E2E worker_picked_after_sec", int((began - start).total_seconds()),
              "| run_minutes", round((end - began).total_seconds() / 60, 1),
              "| riyadh", f"{to_riyadh(began):%H:%M:%S}", "→", f"{to_riyadh(end):%H:%M:%S}", flush=True)
        print("E2E new_complete", len(comp), "| new_incomplete", len(new) - len(comp),
              "| duplicates_skipped", run.duplicate_count if run else "?", "| rejected", run.rejected_count if run else "?",
              "| cost_usd", round(float(cost), 3), "| per_complete", round(float(cost) / max(len(comp), 1), 3), flush=True)
        print("E2E cost_steps", {k: round(float(v), 3) for k, v in steps.items()}, flush=True)
        if run and run.shortfall_reason:
            print("E2E shortfall", run.shortfall_reason.replace("\n", " / "), flush=True)
        for c in new:
            ph, em = c.primary("phone"), c.primary("email")
            print("E2E row", c.id, "|", c.name, "|", c.activity[:60], "|", c.city, "|", c.data_status, "|",
                  ph.value if ph else "-", "|", ph.source_url if ph else "-", "|", em.value if em else "-", "|",
                  em.source_url if em else "-", "|", f"{to_riyadh(c.first_found_at):%Y-%m-%d %H:%M}", flush=True)
    zero()

elif cmd == "portal":
    from fastapi.testclient import TestClient
    import mandoob.auth as A
    from mandoob.web.app import app
    codes = {}
    A.send_code_email = lambda s, user, code, portal: codes.__setitem__(user.email, code) or True
    REP = "rep.test@syarahapp.invalid"
    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == REP))
        if u is None:
            from mandoob.permissions import PROFILES
            u = User(name="موظف تجريبي", email=REP, password_hash="!", role="rep", profile="custom", scope="own",
                     perms={**PROFILES["sales_rep"]["perms"], "export": True})
            s.add(u)
            s.flush()
        u.active = True
        rep_id = u.id
        ids = [c.id for c in s.scalars(select(Company).where(
            Company.is_test.is_(False), Company.lead_source == "discovery", Company.data_status == "complete",
            Company.owner_id.is_(None)).order_by(Company.id.desc()).limit(5))]
    print("E2E portal picked", ids, flush=True)
    with TestClient(app, base_url="https://mandoob-web.onrender.com") as c:
        r = c.post("/admin/login/password", data={"email": settings.admin_email, "password": settings.admin_password},
                   follow_redirects=False)
        if r.status_code != 303 or r.headers.get("location") != "/":
            c.post("/admin/login/request", data={"email": settings.admin_email})
            c.post("/admin/login/verify", data={"code": codes.get(settings.admin_email, "")})
        assert c.get("/prospecting").status_code == 200, "admin login failed"
        r = c.post("/companies/assign", data={"owner_id": str(rep_id), "company_ids": [str(i) for i in ids]},
                   follow_redirects=False)
        print("E2E assign status", r.status_code, flush=True)
    with session_scope() as s:
        print("E2E assigned_to_rep", s.scalar(select(func.count(Company.id)).where(Company.owner_id == rep_id)), flush=True)
    with TestClient(app, base_url="https://mandoob-web.onrender.com") as c:
        c.post("/portal/login/request", data={"email": REP})
        r = c.post("/portal/login/verify", data={"code": codes.get(REP, "")}, follow_redirects=False)
        print("E2E rep_login", r.status_code, r.headers.get("location"), flush=True)
        home = c.get("/portal").text
        with session_scope() as s:
            names = [s.get(Company, i).name for i in ids]
        print("E2E rep_sees", sum(n in home for n in names), "of", len(names), "| copy_buttons", "نسخ كل الأرقام" in home,
              flush=True)
        x = c.get("/portal/export.xlsx")
        from openpyxl import load_workbook
        rows = list(load_workbook(io.BytesIO(x.content)).active.iter_rows(values_only=True))
        print("E2E export http", x.status_code, "rows", len(rows) - 1, "| headers", rows[0], flush=True)
        stamp = now_riyadh().strftime("%Y-%m-%dT%H:%M")
        r1 = c.post(f"/portal/companies/{ids[0]}/status", data={"stage": "offer_sent", "offer_at": stamp,
                                                                "offer_note": "اختبار: عرض أُرسل يدويًا"}, follow_redirects=False)
        r2 = c.post(f"/portal/companies/{ids[1]}/status", data={"stage": "contacted"}, follow_redirects=False)
        r3 = c.post(f"/portal/companies/{ids[2]}/status", data={"stage": "needs_followup", "next_title": "اختبار متابعة",
                                                                "next_at": stamp}, follow_redirects=False)
        print("E2E status_updates", r1.status_code, r2.status_code, r3.status_code, flush=True)
    with session_scope() as s:
        for i in ids[:3]:
            co = s.get(Company, i)
            print("E2E status", co.id, co.name, "|", co.stage, "|", co.offer_sent_at, "|",
                  co.offer_sent_by.name if co.offer_sent_by else "-", flush=True)
    with TestClient(app, base_url="https://mandoob-web.onrender.com") as c:
        c.post("/admin/login/password", data={"email": settings.admin_email, "password": settings.admin_password})
        rep_page = c.get("/offers-report").text
        print("E2E offers_report_shows", names[0] in rep_page and "موظف تجريبي" in rep_page, flush=True)
    # التنظيف: إعادة السجلات الحقيقية كما كانت وإيقاف الحساب التجريبي
    from mandoob.models import Activity, Task
    with session_scope() as s:
        for i in ids:
            co = s.get(Company, i)
            co.owner_id = co.assigned_at = co.offer_sent_at = co.offer_sent_by_id = co.offer_note = None
            co.contacted_at = co.contacted_by_id = None
            co.stage, co.next_action, co.next_action_at, co.last_action = "new", None, None, None
            s.query(Task).filter(Task.company_id == i, Task.owner_id == rep_id).delete(synchronize_session=False)
            s.add(Activity(company_id=i, kind="data", title="أُلغي إسناد الاختبار وأُعيدت الحالة «لم يتم التواصل»"))
        s.get(User, rep_id).active = False
    print("E2E cleanup done (records reset, test rep deactivated)", flush=True)
    zero()

elif cmd == "sched":
    mins, target = int(sys.argv[2]), int(sys.argv[3])
    at = now_riyadh() + timedelta(minutes=mins)
    with session_scope() as s:
        camp = P.save_config(s, cities=CITIES, regions=[], activities=P.DEFAULT_ACTIVITIES, daily_target=target,
                             scheduled=True, time=at.strftime("%H:%M"))
        print("E2E sched campaign", camp.id, "target", target, "at", at.strftime("%H:%M"), "riyadh; now",
              now_riyadh().strftime("%H:%M:%S"), flush=True)
    jobs.put_setting("e2e_sched", {"at_utc": datetime.utcnow().replace(second=0).isoformat(), "mins": mins})
    state = jobs.get_setting("job_state")
    state.pop("discovery_done", None)
    jobs.put_setting("job_state", state)

elif cmd == "schedreport":
    with session_scope() as s:
        cfg = P.get_config(s)
        run = s.scalar(select(DiscoveryRun).where(DiscoveryRun.campaign_id == cfg["campaign_id"],
                                                  DiscoveryRun.run_date == riyadh_today()))
        print("E2E sched cfg", {k: cfg[k] for k in ("daily_target", "scheduled", "time")}, "job_state",
              jobs.get_setting("job_state"), flush=True)
        if run:
            print("E2E sched run", run.status, "start", f"{to_riyadh(run.started_at):%H:%M:%S}", "end",
                  f"{to_riyadh(run.finished_at):%H:%M:%S}" if run.finished_at else "-", "complete", run.complete_count,
                  "target", run.target, "log_tail", (run.log or [])[-4:], flush=True)
        mark = jobs.get_setting("e2e_sched")
        if mark:
            since = datetime.fromisoformat(mark["at_utc"])
            new = list(s.scalars(select(Company).where(Company.campaign_id == cfg["campaign_id"],
                                                       Company.first_found_at >= since)))
            first = min((c.first_found_at for c in new), default=None)
            last = max((c.first_found_at for c in new), default=None)
            cost = s.scalar(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(CostEntry.at >= since)) or 0
            print("E2E sched since_request", "new", len(new), "complete", sum(c.data_status == "complete" for c in new),
                  "| first_found", f"{to_riyadh(first):%H:%M:%S}" if first else "-",
                  "| last_found", f"{to_riyadh(last):%H:%M:%S}" if last else "-", "| cost_usd", round(float(cost), 3),
                  "| discovery_done_marked", jobs.get_setting("job_state").get("discovery_done"), flush=True)

elif cmd == "restore":
    with session_scope() as s:
        camp = P.save_config(s, cities=CITIES, regions=[], activities=P.DEFAULT_ACTIVITIES, daily_target=400,
                             scheduled=False, time="06:00")
        print("E2E restored target", camp.daily_target, "status", camp.status, flush=True)

elif cmd == "zero":
    zero()
