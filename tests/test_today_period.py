"""«عملاء اليوم»: بحث يومي أو من تاريخ إلى تاريخ، حسب تاريخ الجمع بتوقيت الرياض (شاملًا يومي البداية والنهاية)،
مع فلتر البحث/الحملة على الجدول والعدادات والتكلفة وملف Excel — دون تشغيل بحث أو احتساب تكلفة جديدة."""
import io
import re
from datetime import date, datetime

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import func, select

from helpers import otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import Campaign, Company, Contact, CostEntry, DiscoveryRun
from mandoob.timeutil import riyadh_to_utc
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
# (الاسم، وقت الجمع بتوقيت الرياض، الحملة)
SAMPLE = [("قبل الفترة ٣١ أكتوبر ٢٣:٥٩", "2026-10-31T23:59", "A"),
          ("بداية ١ نوفمبر ٠٠:٠٠", "2026-11-01T00:00", "A"),
          ("نهاية ١ نوفمبر ٢٣:٥٩", "2026-11-01T23:59", "A"),
          ("منتصف ١٠ نوفمبر", "2026-11-10T12:00", "A"),
          ("حملة أخرى ١٠ نوفمبر", "2026-11-10T13:00", "B"),
          ("نهاية ٢٠ نوفمبر ٢٣:٥٩", "2026-11-20T23:59", "A"),
          ("بعد الفترة ٢١ نوفمبر ٠٠:٠٠", "2026-11-21T00:00", "A")]
S = {}


def setup_module():
    init_db()
    with session_scope() as s:
        camps = {}
        for k in ("A", "B"):
            c = Campaign(name=f"فترة-اختبار-{k}", status="paused", daily_target=10, send_enabled=False)
            s.add(c)
            s.flush()
            camps[k] = c.id
        for i, (name, at, k) in enumerate(SAMPLE):
            utc = riyadh_to_utc(datetime.fromisoformat(at))
            co = Company(name=name, name_norm=f"period-{i}", activity="نقل", city="الرياض", data_status="complete",
                         lead_source="discovery", campaign_id=camps[k], first_found_at=utc, counted_on=utc.date(),
                         website=f"https://period-{i}.example", domain=f"period-{i}.example")
            s.add(co)
            s.flush()
            co.contacts.append(Contact(kind="phone", value=f"+96658713{i:03d}", source_url=f"https://period-{i}.example/c",
                                       verified=True))
            co.contacts.append(Contact(kind="email", value=f"info@period-{i}.example",
                                       source_url=f"https://period-{i}.example/c", verified=True))
        s.add(CostEntry(day=date(2026, 11, 1), service="claude", units=1, cost_usd=2.5, campaign_id=camps["A"], note="agent"))
        s.add(CostEntry(day=date(2026, 11, 10), service="claude", units=1, cost_usd=1.0, campaign_id=camps["B"], note="agent"))
        S.update(camps)


def _names(html):
    return {n for n, _, _ in SAMPLE if n in html}


def _table_count(html):
    return int(re.search(r"الفترة المعروضة: .*? — (\d+) عميل", html).group(1))


def _xlsx_rows(content):
    ws = load_workbook(io.BytesIO(content)).active
    return [r for r in ws.iter_rows(min_row=4, values_only=True) if r and r[0]]


def test_daily_view_shows_only_that_day_and_excel_matches():
    with session_scope() as s:
        before = (s.scalar(select(func.count(CostEntry.id))), s.scalar(select(func.count(DiscoveryRun.id))))
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        qs = f"mode=day&day=2026-11-01&campaign={S['A']}"
        html = c.get(f"/today?{qs}").text
        assert _names(html) == {"بداية ١ نوفمبر ٠٠:٠٠", "نهاية ١ نوفمبر ٢٣:٥٩"}
        assert "الفترة المعروضة: " in html and _table_count(html) == 2
        assert 'value="2026-11-01"' in html and 'value="day" checked' in html          # الاختيار محفوظ
        assert "$2.5" in html                                                         # تكلفة الفترة والحملة
        x = c.get(f"/today.xlsx?{qs}")
        assert len(_xlsx_rows(x.content)) == 2 and "2026-11-01" in x.headers["content-disposition"]
    with session_scope() as s:  # استعراض فقط: لا بحث جديد ولا تكلفة إضافية
        assert (s.scalar(select(func.count(CostEntry.id))), s.scalar(select(func.count(DiscoveryRun.id)))) == before


def test_range_includes_both_end_days_fully_and_campaign_filter_applies_everywhere():
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        qs = f"mode=range&from=2026-11-01&to=2026-11-20&campaign={S['A']}"
        html = c.get(f"/today?{qs}").text
        expected = {"بداية ١ نوفمبر ٠٠:٠٠", "نهاية ١ نوفمبر ٢٣:٥٩", "منتصف ١٠ نوفمبر", "نهاية ٢٠ نوفمبر ٢٣:٥٩"}
        assert _names(html) == expected and _table_count(html) == 4
        assert 'value="range" checked' in html and 'name="to" value="2026-11-20"' in html
        x = c.get(f"/today.xlsx?{qs}")
        assert len(_xlsx_rows(x.content)) == _table_count(html) == 4                  # Excel = إجمالي الجدول
        # كل الحملات في الفترة نفسها: تُضاف سجلات الحملة الأخرى وتكلفتها
        qs_all = "mode=range&from=2026-11-01&to=2026-11-20"
        html_all = c.get(f"/today?{qs_all}").text
        assert expected | {"حملة أخرى ١٠ نوفمبر"} <= _names(html_all)
        assert len(_xlsx_rows(c.get(f"/today.xlsx?{qs_all}").content)) == _table_count(html_all)


def test_end_before_start_warns_and_empty_period_message():
    with TestClient(app) as c:
        otp_login(c, ADMIN, "admin")
        html = c.get("/today?mode=range&from=2026-11-20&to=2026-11-01").text
        assert "تاريخ النهاية قبل تاريخ البداية" in html and "الفترة المعروضة" not in html
        assert c.get("/today.xlsx?mode=range&from=2026-11-20&to=2026-11-01").status_code == 400
        html = c.get(f"/today?mode=day&day=2026-09-15&campaign={S['A']}").text
        assert "لا يوجد عملاء في الفترة المختارة" in html
        from mandoob.timeutil import riyadh_today
        html = c.get("/today").text                                                    # افتراضيًا: اليوم
        assert f'name="day" value="{riyadh_today().isoformat()}"' in html and 'value="day" checked' in html
