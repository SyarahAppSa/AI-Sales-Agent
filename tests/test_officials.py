"""«المسؤول الموثّق»: يُقبل الاسم فقط إن ظهر مع اسم الشركة في الدليل؛ قديم/سابق ← «يحتاج تحقق»؛ لا تكرار للشخص."""
import io
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from helpers import otp_login
from mandoob.db import init_db, session_scope
from mandoob.models import Company, CompanyOfficial, Contact
from mandoob.web.app import app

ADMIN = "admin@sayaraup.test"
S = {}


def page(url, text):
    return NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url=url,
                                                        content=NS(source=NS(type="text", data=text))))


def search(*pairs):
    return NS(type="web_search_tool_result", content=[{"url": u, "title": t} for u, t in pairs])


def setup_module():
    init_db()
    with session_scope() as s:
        c = Company(name="شركة الصقر للنقل المبرد", name_en="Al Saqr Cold Transport", name_norm="officials-saqr",
                    activity="نقل مبرد", city="الرياض", data_status="complete", lead_source="discovery",
                    website="https://alsaqr-cold.example", domain="alsaqr-cold.example", decision_unit="إدارة الأسطول والتشغيل")
        s.add(c)
        s.flush()
        c.contacts.append(Contact(kind="phone", value="+966114906217", source_url="https://alsaqr-cold.example/c", verified=True))
        S["cid"] = c.id


def test_verified_official_needs_check_and_rejected_and_no_duplicates(monkeypatch):
    from mandoob import ai, officials as O
    out = {"officials": [
        {"name": "فهد العتيبي", "title": "مدير الأسطول", "summary": "يدير أسطول الشاحنات المبردة (120 شاحنة)",
         "evidence_url": "https://alsaqr-cold.example/team", "source_type": "company_site", "relevance": "fleet",
         "current": True, "evidence_date": "2026", "company_evidence": "فريق الإدارة", "professional_email": "fahad@alsaqr-cold.example",
         "professional_phone": ""},
        {"name": "Khalid Al Harbi", "title": "Operations Manager", "summary": "Runs daily transport operations",
         "evidence_url": "https://sa.linkedin.com/in/khalid-alharbi", "source_type": "linkedin", "relevance": "operations",
         "current": True, "evidence_date": "2019", "company_evidence": "", "professional_email": "", "professional_phone": ""},
        {"name": "سالم المختلق", "title": "مدير المشتريات", "summary": "x", "evidence_url": "https://news.example/a",
         "source_type": "news", "relevance": "procurement", "current": True, "evidence_date": "2026",
         "company_evidence": "", "professional_email": "", "professional_phone": ""}]}
    blocks = [page("https://alsaqr-cold.example/team",
                   "فريق الإدارة في شركة الصقر للنقل المبرد: فهد العتيبي — مدير الأسطول. للتواصل: fahad@alsaqr-cold.example"),
              search(("https://sa.linkedin.com/in/khalid-alharbi", "Khalid Al Harbi - Operations Manager - Al Saqr Cold Transport | LinkedIn"),
                     ("https://news.example/a", "خبر عن قطاع النقل"))]
    monkeypatch.setattr(ai, "ai_agent", lambda *a, **k: (out, blocks, 2))
    res = O.find_for(S["cid"])
    names = {f["name"]: f for f in res["found"]}
    assert names["فهد العتيبي"]["status"] == "verified"
    assert names["Khalid Al Harbi"]["status"] == "needs_check" and "2019" in names["Khalid Al Harbi"]["why"]
    assert res["rejected"] and res["rejected"][0]["name"] == "سالم المختلق"          # لا يظهر في أي دليل
    O.find_for(S["cid"])                                                                 # مصدر جديد لا يكرر الشخص
    with session_scope() as s:
        offs = s.query(CompanyOfficial).filter(CompanyOfficial.company_id == S["cid"]).all()
        assert len(offs) == 2
        p = next(o for o in offs if o.is_primary)
        assert p.name == "فهد العتيبي" and p.email == "fahad@alsaqr-cold.example"
        assert s.get(Company, S["cid"]).official_status == "verified"
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        html = cl.get("/officials").text
        assert "المسؤول الموثّق" in html and "فهد العتيبي" in html and "مدير الأسطول" in html
        assert "تواصل مهني للمسؤول" in html and "هاتف الشركة العام" in html and "إدارة الأسطول والتشغيل" in html
        assert "إيقاف" in html
        cl.post("/officials/control", data={"action": "pause"})
        assert "استئناف" in cl.get("/officials").text and O.run_cycle(force=True) == 0
        cl.post("/officials/control", data={"action": "resume"})
        rows = list(load_workbook(io.BytesIO(cl.get("/officials.xlsx").content)).active.iter_rows(values_only=True))
        head = [str(x) for x in rows[0]]
        assert "المسؤول الموثّق" in head and "بريد المسؤول المهني" in head and "هاتف الشركة العام" in head
        line = next(r for r in rows if r[0] == "شركة الصقر للنقل المبرد")
        assert line[head.index("المسؤول الموثّق")] == "فهد العتيبي"


def test_none_found_shows_message(monkeypatch):
    from mandoob import ai, officials as O
    with session_scope() as s:
        c = Company(name="مؤسسة بلا مسؤول منشور", name_norm="officials-none", activity="تجارة", city="جدة",
                    data_status="complete", lead_source="discovery")
        s.add(c)
        s.flush()
        cid = c.id
    monkeypatch.setattr(ai, "ai_agent", lambda *a, **k: ({"officials": []}, [], 1))
    O.find_for(cid)
    with session_scope() as s:
        assert s.get(Company, cid).official_status == "none"
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        assert "لم يُعثر على مسؤول موثّق" in cl.get("/officials").text


def test_counts_cards_filter_lists():
    import re
    with TestClient(app) as cl:
        otp_login(cl, ADMIN, "admin")
        html = cl.get("/officials").text
        n_ver = int(re.search(r'ostatus=verified"><div class="l">مسؤول موثّق</div><div class="v"[^>]*>(\d+)', html).group(1))
        page = cl.get("/officials?ostatus=verified").text
        shown = int(re.search(r"\((\d+) منشأة\)", page).group(1))
        assert n_ver >= 1 and shown == n_ver and "فهد العتيبي" in page
        assert "لم يُعثر على مسؤول موثّق" in cl.get("/officials?ostatus=none").text
