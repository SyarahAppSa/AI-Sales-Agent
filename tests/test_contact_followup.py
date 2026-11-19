"""حالة التواصل والملاحظات ومنع تكرار التواصل بين الموظفين + تنزيل Excel لكل القوائم والتقارير.

اختبار التسليم: العميل نفسه بحسابي موظفين؛ الأول يسجّل «لم يرد» مع ملاحظة ← الثاني يرى التحديث فورًا، ولا تتكرر
المحاولة، والملاحظة واسم الموظف والتوقيت تظهر في ملف Excel."""
import io
import threading

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import func, select

from helpers import otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import Company, Contact, ContactLog, Task, User
from mandoob.timeutil import riyadh_today
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
R1, R2 = "follow.one@syarahapp.test", "follow.two@syarahapp.test"
NOTE = "تحدثت مع مدير الأسطول وطلب العرض يوم الأحد"
S = {}


def _xlsx(content):
    wb = load_workbook(io.BytesIO(content))
    return [[("" if v is None else str(v)) for v in r] for ws in wb.worksheets for r in ws.iter_rows(values_only=True)]


def setup_module():
    init_db()
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        for em, nm in [(R1, "ريم المتابِعة"), (R2, "سالم الزميل")]:
            adm.post("/staff/add", data={"name": nm, "email": em, "team": "فريق المتابعة", "profile": "custom",
                                         "customize": "on", "scope": "team", "see_phone": "on", "see_email": "on",
                                         "update_status": "on", "export": "on", "see_reports": "on", "add_note": "on"})
    with session_scope() as s:
        S["r1"] = s.scalar(select(User.id).where(User.email == R1))
        S["r2"] = s.scalar(select(User.id).where(User.email == R2))
        c = Company(name="منشأة المتابعة المشتركة", name_norm="follow-shared", activity="نقل", city="الرياض",
                    data_status="complete", lead_source="discovery", owner_id=S["r1"], website="https://follow.example",
                    domain="follow.example", decision_unit="مدير الأسطول")
        s.add(c)
        s.flush()
        c.contacts.append(Contact(kind="phone", value="+966599887766", source_url="https://follow.example/c", verified=True))
        S["cid"] = c.id


def test_two_reps_same_customer_no_answer_visible_immediately_and_in_excel():
    cid = S["cid"]
    base = f"/portal/companies/{cid}"
    with TestClient(app) as one, TestClient(app) as two:
        otp_login(one, R1, "portal")
        otp_login(two, R2, "portal")
        assert "منشأة المتابعة المشتركة" in two.get("/portal").text            # الاثنان يريان العميل (نطاق الفريق)
        # الأول يضغط «بدء التواصل» ← الثاني يرى «جارٍ التواصل بواسطة ريم المتابِعة» فورًا، ولا يستطيع البدء
        one.post(f"{base}/contact/start", headers={"referer": f"http://testserver{base}"})
        assert "جارٍ التواصل بواسطة ريم المتابِعة" in two.get(base).text
        assert "جارٍ التواصل بواسطة ريم المتابِعة" in two.get("/portal").text
        r = two.post(f"{base}/contact/start", headers={"referer": f"http://testserver{base}"}, follow_redirects=True)
        assert "جارٍ التواصل بواسطة ريم المتابِعة" in r.text
        # الأول يسجّل «لم يرد» مع ملاحظة
        one.post(f"{base}/contact/update", data={"status": "no_answer", "note": NOTE},
                 headers={"referer": f"http://testserver{base}"})
        page = two.get(base).text                                                 # يرى التحديث فورًا
        assert "لم يرد" in page and NOTE in page and "ريم المتابِعة" in page and "آخر محاولة" in page
        assert "تمت محاولة اليوم" in page
        # المحاولة لا تتكرر: الثاني لا يستطيع بدء الاتصال في اليوم نفسه
        r = two.post(f"{base}/contact/start", headers={"referer": f"http://testserver{base}"}, follow_redirects=True)
        assert "لا تكرر الاتصال اليوم" in r.text
        with session_scope() as s:
            c = s.get(Company, cid)
            assert c.contact_lock_by_id is None and c.attempts_count == 1 and c.last_attempt_by_id == S["r1"]
        # الملاحظة واسم الموظف والتوقيت في ملف Excel (من حساب الموظف الثاني)
        x = two.get("/portal/export.xlsx")
        rows = _xlsx(x.content)
        flat = " ".join(" ".join(r) for r in rows)
        assert NOTE in flat and "ريم المتابِعة" in flat and "لم يرد" in flat
        head = rows[0]
        line = next(r for r in rows if "منشأة المتابعة المشتركة" in r)
        assert line[head.index("آخر ملاحظة")] == NOTE and line[head.index("كاتب الملاحظة")] == "ريم المتابِعة"
        assert line[head.index("وقت الملاحظة")] and line[head.index("تاريخ آخر محاولة")]
        assert line[head.index("المندوب المسؤول")] == "ريم المتابِعة"
        off = _xlsx(two.get("/portal/officials.xlsx").content)
        assert any(NOTE in " ".join(r) for r in off)
    with TestClient(app) as adm:                                                  # وفي تقارير الإدارة
        otp_login(adm, ADMIN, "admin")
        flat = " ".join(" ".join(r) for r in _xlsx(adm.get("/companies.xlsx?status=complete").content))
        assert NOTE in flat and "ريم المتابِعة" in flat
        today = riyadh_today().isoformat()
        assert NOTE in adm.get(f"/companies/{cid}").text


def test_simultaneous_start_only_one_wins_and_history_kept_across_reassign():
    from mandoob.contact import ContactBlocked, start_contact, update_followup
    with session_scope() as s:
        c = Company(name="منشأة التزامن", name_norm="follow-race", activity="نقل", city="جدة", data_status="complete",
                    lead_source="discovery", owner_id=S["r1"])
        s.add(c)
        s.flush()
        cid = c.id
    wins, gate = [], threading.Barrier(2)

    def go(uid):
        gate.wait()
        try:
            with session_scope() as s:
                start_contact(s, s.get(Company, cid), s.get(User, uid))
            wins.append(uid)
        except ContactBlocked:
            pass
    ts = [threading.Thread(target=go, args=(u,)) for u in (S["r1"], S["r2"])]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1                                                         # موظف واحد فقط
    from datetime import timedelta
    from mandoob.timeutil import now_utc
    with session_scope() as s:
        c = s.get(Company, cid)
        winner = s.get(User, wins[0])
        update_followup(s, c, winner, "call_later", "طلب الاتصال بعد العصر", now_utc() + timedelta(days=1), S["r2"])
    with session_scope() as s:
        c = s.get(Company, cid)
        update_followup(s, c, s.get(User, S["r2"]), "offer_sent", "أرسلت العرض على البريد")
        c.owner_id = S["r2"]                                                      # نقل العميل لمندوب آخر
    with session_scope() as s:
        c = s.get(Company, cid)
        assert c.contact_status == "offer_sent" and c.last_note == "أرسلت العرض على البريد"   # الحالة باقية
        logs = list(s.scalars(select(ContactLog).where(ContactLog.company_id == cid).order_by(ContactLog.id)))
        assert [x.kind for x in logs] == ["start", "update", "update"]           # السجل السابق محفوظ
        assert logs[1].note == "طلب الاتصال بعد العصر" and logs[1].next_owner_name == "سالم الزميل"
        assert s.scalar(select(Task).where(Task.company_id == cid, Task.owner_id == S["r2"]))  # موعد المتابعة ومسؤولها


def test_do_not_contact_leaves_followups_and_blocks_start():
    from mandoob.contact import ContactBlocked, start_contact, update_followup
    from mandoob.reports import needs_followup_today
    from datetime import timedelta
    from mandoob.timeutil import now_utc
    with session_scope() as s:
        c = Company(name="منشأة لا تتواصلوا", name_norm="follow-dnc", activity="نقل", city="الدمام",
                    data_status="complete", lead_source="discovery", owner_id=S["r1"])
        s.add(c)
        s.flush()
        u = s.get(User, S["r1"])
        update_followup(s, c, u, "call_later", "اتصل لاحقًا", now_utc() - timedelta(hours=1))
        assert any(i["company"].id == c.id for i in needs_followup_today(s, u))
        update_followup(s, c, u, "do_not_contact", "طلب عدم الاتصال")
        s.flush()
        assert not any(i["company"].id == c.id for i in needs_followup_today(s, u))
        try:
            start_contact(s, c, s.get(User, S["r2"]))
            raise AssertionError("started")
        except ContactBlocked as e:
            assert "عدم التواصل" in str(e)


def test_every_list_and_report_downloads_excel_with_all_filtered_rows():
    with session_scope() as s:
        for i in range(60):  # أكثر من صفحة (٥٠ لكل صفحة)
            s.add(Company(name=f"منشأة صفحات {i}", name_norm=f"pages-{i}", activity="نقل", city="الرياض",
                          data_status="complete", lead_source="discovery", is_test=False))
    with TestClient(app) as adm:
        otp_login(adm, ADMIN, "admin")
        page = adm.get("/companies?status=complete&q=منشأة صفحات").text
        assert "تنزيل Excel" in page and "60 سجل" in page
        rows = [r for r in _xlsx(adm.get("/companies.xlsx?status=complete&q=منشأة صفحات").content) if r and r[0].startswith("منشأة صفحات")]
        assert len(rows) == 60                                                    # كل الصفحات
        for url in ["/followups.xlsx", "/daily-report.xlsx", "/weekly-report.xlsx", "/leads.xlsx", "/offers-report.xlsx",
                    "/today.xlsx"]:
            r = adm.get(url)
            assert r.status_code == 200 and r.content[:2] == b"PK", url
        for url in ["/followups", "/daily-report", "/weekly-report", "/leads", "/offers-report", "/today", "/companies"]:
            assert "تنزيل Excel" in adm.get(url).text, url
    with TestClient(app) as two:
        otp_login(two, R2, "portal")
        for url in ["/portal", "/portal/officials", "/portal/reports"]:
            assert "تنزيل Excel" in two.get(url).text, url
        assert two.get("/portal/reports.xlsx").content[:2] == b"PK"
