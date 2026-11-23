from datetime import datetime

from mandoob.normalize import names_similar, normalize_name, normalize_phone, email_label
from mandoob.replyparse import classify_intent, parse_requested_time
from mandoob.targeting import parse_request_rules


def test_phone_normalization():
    assert normalize_phone("011 456 7890") == "+966114567890"
    assert normalize_phone("٠٥٥١٢٣٤٥٦٧") == "+966551234567"
    assert normalize_phone("+966 (0) 12 654 3210") == "+966126543210"
    assert normalize_phone("920012345") == "+966920012345"
    assert normalize_phone("12345") is None


def test_name_dedupe():
    assert normalize_name("شركة النخبة للنقل المحدودة") == normalize_name("النخبة للنقل")
    assert names_similar("Al Nokhba Transport Co.", "AL NOKHBA TRANSPORT")


def test_email_preference():
    assert email_label("procurement@x.sa")[0] < email_label("info@x.sa")[0] < email_label("someone@x.sa")[0]
    assert email_label("hr@x.sa")[0] == 99


def test_targeting_example_from_brief():
    r = parse_request_rules("ابحث يوميًا عن شركات النقل والخدمات اللوجستية والتوصيل وتأجير السيارات في الرياض وجدة "
                            "والدمام. أعطِ الأولوية للشركات التي تدير أسطول مركبات، واستبعد الوسطاء وعملاء «سيارة اب» "
                            "الحاليين. ٢٠٠ شركة نقل وخدمات لوجستية، و١٢٠ شركة توصيل، و٨٠ شركة تأجير سيارات")
    quotas = {c["sector_key"]: c["quota"] for c in r["categories"]}
    assert quotas == {"transport_logistics": 200, "delivery": 120, "car_rental": 80}
    assert r["daily_target"] == 400
    assert r["exclude_existing_customers"] is True
    assert any("وسطاء" in e for e in r["exclusions"])
    assert r["categories"][0]["cities"] == ["الرياض", "جدة", "الدمام"]
    assert "تدير أسطول مركبات" in r["categories"][0]["priority_signals"]


def test_refrigerated_synonyms():
    r = parse_request_rules("شركات النقل المبرد في الرياض")
    cat = r["categories"][0]
    assert cat["sector_key"] == "refrigerated_transport"
    assert "سلاسل التبريد" in cat["keywords_ar"] and "التوزيع المبرد" in cat["keywords_ar"]


def test_reply_parsing_sunday_afternoon():
    received = datetime(2026, 11, 19, 8, 0)  # الخميس 11:00 بتوقيت الرياض
    assert classify_intent("مهتم، اتصلوا بي الأحد بعد الظهر") == "callback_request"
    at, label = parse_requested_time("مهتم، اتصلوا بي الأحد بعد الظهر", received)
    assert at == datetime(2026, 11, 22, 10, 0)  # الأحد 1:00 م بتوقيت الرياض = 10:00 UTC
    assert "الأحد" in label and "بعد الظهر" in label


def test_intents():
    assert classify_intent("غير مهتم شكرا") == "not_interested"
    assert classify_intent("الرجاء إيقاف المراسلات") == "unsubscribe"
    assert classify_intent("نبغى عرض سعر") == "quote_request"


def test_long_text_is_clipped_not_failing_insert():
    """فحص الإطلاق: «النشاط» الطويل من البحث الآلي أسقط دفعة اكتشاف كاملة على Postgres."""
    from mandoob.db import init_db, session_scope
    from mandoob.models import Company
    init_db()
    long_activity = "محل حلويات ومخبز " + "شرح طويل " * 60
    with session_scope() as s:
        c = Company(name="حلويات اختبار القص", name_norm="clip-test", activity=long_activity)
        s.add(c)
        s.flush()
        cid = c.id
    with session_scope() as s:
        a = s.get(Company, cid).activity
        assert len(a) == 200 and a.startswith("محل حلويات ومخبز") and a.endswith("…")


def test_platform_and_placeholder_emails_rejected():
    """فحص Overture: موقع على WordPress أعطى blog@wordpress.com — بريد المنصة ليس بريد المنشأة."""
    from mandoob.normalize import normalize_email
    for bad in ("blog@wordpress.com", "x@sentry-next.wixpress.com", "you@example.com", "logo@2x.png", "a@b.wix.com"):
        assert normalize_email(bad) is None, bad
    assert normalize_email("Info@Yasoublogistics.com") == "info@yasoublogistics.com"


def test_owner_password_fallback_when_mail_auth_rejected(monkeypatch):
    """إذا رفض خادم البريد كلمة السر، يُتاح لمالك المنصة الدخول الاحتياطي بكلمة المرور فلا تُغلق المنصة عليه."""
    from mandoob import auth
    from mandoob.db import init_db, session_scope
    from mandoob.models import Setting
    init_db()
    with session_scope() as s:
        base = auth.mail_ready(s)
    auth._mark_mail_auth(True)
    with session_scope() as s:
        assert auth.mail_ready(s) is False
    auth._mark_mail_auth(False)
    with session_scope() as s:
        assert auth.mail_ready(s) == base and not s.get(Setting, auth.MAIL_AUTH_FAIL_KEY).value["failed"]
