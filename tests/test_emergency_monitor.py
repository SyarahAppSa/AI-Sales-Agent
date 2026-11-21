"""«لوحة طوارئ» ← المراقبة والمتابعة: «يحتاج تدخلك» يلتقط الدفعة العالقة والمهمة المتأخرة وخطأ Claude وفشل كود الدخول،
و«الحالة الحين» وClaude والبحث والإيميلات تظهر بأرقام — بلا أي بيانات عملاء."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from helpers import otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import Company, DirectoryRun, Incident, Task
from mandoob.timeutil import now_utc
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"


def test_monitor_flags_and_renders():
    init_db()
    from mandoob import ai, emergency as E
    err = ai._api_err("خطأ من خدمة الذكاء الاصطناعي (529)")
    assert isinstance(err, ai.AIUnavailable)
    E.note_incident("email", "login_code", "تعذّر إرسال كود دخول من بريد المنصة")
    with session_scope() as s:
        old = now_utc() - timedelta(hours=1)
        s.add(DirectoryRun(status="running", sources=["overture"], params={}, started_at=old, heartbeat=old))
        c = Company(name="منشأة مراقبة متأخرة", name_norm="منشأة مراقبة متأخرة", data_status="incomplete")
        s.add(c)
        s.flush()
        s.add(Task(company_id=c.id, title="اتصال متأخر", due_at=now_utc() - timedelta(days=1)))
    with session_scope() as s:
        m = E.monitor(s)
        att = {a["title"]: a for a in m["attention"]}
        assert att["دفعات عالقة الآن"]["value"] >= 1 and att["دفعات عالقة الآن"]["bad"]
        assert att["مهام متابعة متأخرة"]["value"] >= 1
        assert att["فشل إرسال أكواد الدخول (24 ساعة)"]["value"] >= 1
        cl = {a["title"]: a for a in m["claude"]}
        assert cl["أخطاء آخر 10 د"]["value"] >= 1 and cl["أخطاء آخر 10 د"]["bad"]
        assert s.query(Incident).filter_by(service="claude").count() >= 1
        assert m["needs"] >= 3
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        html = cl.get("/emergency").text
        for t in ("يحتاج تدخلك", "الحالة الحين", "Claude (الذكاء الاصطناعي)", "البحث والجمع", "الإيميلات",
                  "قاعدة البيانات", "آخر نبضة من العامل", "مهام متابعة متأخرة", "طلبات آخر 10 د", "بريد المنصة"):
            assert t in html, t
        assert "mon-tile bad" in html and "منشأة مراقبة متأخرة" not in html
    with session_scope() as s:  # تنظيف: لا تبقى الدفعة «عالقة» لاختبارات أخرى
        for r in s.query(DirectoryRun).filter_by(status="running"):
            r.status = "stopped"
