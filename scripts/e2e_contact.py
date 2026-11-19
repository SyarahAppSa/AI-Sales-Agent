"""اختبار التسليم على الخادم: العميل نفسه بحسابي موظفين تجريبيين؛ الأول يسجّل «لم يرد» مع ملاحظة، والثاني يرى التحديث
فورًا ولا يكرر المحاولة، والملاحظة واسم الموظف والتوقيت في ملف Excel. تُحذف البيانات التجريبية بعده."""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from sqlalchemy import select  # noqa: E402

import mandoob.auth as A  # noqa: E402
from mandoob.db import session_scope  # noqa: E402
from mandoob.models import Activity, Company, Contact, ContactLog, LoginCode, Task, User  # noqa: E402
from mandoob.permissions import PROFILES  # noqa: E402
from mandoob.web.app import app  # noqa: E402

codes = {}
A.send_code_email = lambda s, user, code, portal: codes.__setitem__(user.email, code) or True
NOTE = "تحدثت مع مدير الأسطول وطلب العرض يوم الأحد"
EM = ["e2e.contact.one@syarahapp.invalid", "e2e.contact.two@syarahapp.invalid"]
with session_scope() as s:
    ids = []
    for em, nm in zip(EM, ["موظف تجريبي ١", "موظف تجريبي ٢"]):
        u = s.scalar(select(User).where(User.email == em)) or User(name=nm, email=em, password_hash="!", role="rep")
        u.team, u.scope, u.profile, u.active = "فريق اختبار التواصل", "team", "custom", True
        u.perms = {**PROFILES["sales_rep"]["perms"], "export": True}
        s.add(u)
        s.flush()
        ids.append(u.id)
    c = Company(name="منشأة اختبار التواصل (تجريبية)", name_norm="e2e-contact-co", activity="نقل", city="الرياض",
                data_status="complete", lead_source="test_seed", is_test=True, owner_id=ids[0])
    s.add(c)
    s.flush()
    c.contacts.append(Contact(kind="phone", value="+000999000111", source_url="test://e2e", verified=True))
    cid = c.id


def login(cl, em):
    cl.post("/portal/login/request", data={"email": em})
    r = cl.post("/portal/login/verify", data={"code": codes.get(em, "")}, follow_redirects=False)
    return r.headers.get("location")


base = f"/portal/companies/{cid}"
H = {"referer": f"https://mandoob.live{base}"}
try:
    with TestClient(app, base_url="https://mandoob.live") as one, TestClient(app, base_url="https://mandoob.live") as two:
        print("E2EC login", login(one, EM[0]), login(two, EM[1]), flush=True)
        one.post(f"{base}/contact/start", headers=H)
        print("E2EC two sees lock:", "جارٍ التواصل بواسطة موظف تجريبي ١" in two.get(base).text, flush=True)
        r = two.post(f"{base}/contact/start", headers=H, follow_redirects=True)
        print("E2EC two start while locked refused:", "جارٍ التواصل بواسطة موظف تجريبي ١" in r.text, flush=True)
        one.post(f"{base}/contact/update", data={"status": "no_answer", "note": NOTE}, headers=H)
        p = two.get(base).text
        print("E2EC two sees no_answer+note+name immediately:", "لم يرد" in p and NOTE in p and "موظف تجريبي ١" in p, flush=True)
        r = two.post(f"{base}/contact/start", headers=H, follow_redirects=True)
        print("E2EC same-day repeat refused:", "لا تكرر الاتصال اليوم" in r.text, flush=True)
        x = two.get("/portal/export.xlsx")
        rows = [[("" if v is None else str(v)) for v in r] for r in load_workbook(io.BytesIO(x.content)).active.iter_rows(values_only=True)]
        head = rows[0]
        line = next(r for r in rows if "منشأة اختبار التواصل" in r[0])
        print("E2EC excel:", {k: line[head.index(k)] for k in ["المندوب المسؤول", "حالة التواصل", "آخر ملاحظة", "كاتب الملاحظة",
                                                              "وقت الملاحظة", "تاريخ آخر محاولة", "صاحب آخر محاولة"]}, flush=True)
    with session_scope() as s:
        logs = list(s.scalars(select(ContactLog).where(ContactLog.company_id == cid).order_by(ContactLog.id)))
        print("E2EC history:", [(l.kind, l.status, l.user_name) for l in logs], flush=True)
finally:
    with session_scope() as s:  # تنظيف البيانات التجريبية
        for M in (ContactLog, Task, Activity):
            s.query(M).filter(M.company_id == cid).delete(synchronize_session=False)
        s.delete(s.get(Company, cid))
        for i in ids:
            s.query(LoginCode).filter(LoginCode.user_id == i).delete(synchronize_session=False)
            s.get(User, i).active = False
    print("E2EC cleanup done (test company deleted, test reps deactivated)", flush=True)
