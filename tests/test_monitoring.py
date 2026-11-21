"""ربط Sentry: معطّل بلا SENTRY_DSN، ويحجب بيانات العملاء والأسرار قبل الإرسال."""
from fastapi.testclient import TestClient

from helpers import otp_login
from mandoob import monitoring
from mandoob.web.app import app


def test_scrub_removes_customer_data_and_secrets():
    ev = {"request": {"url": "https://mandoob.live/companies?q=info@acme.sa", "cookies": {"session": "x"},
                      "data": {"phone": "0551234567"}, "headers": {"Cookie": "session=x", "User-Agent": "UA",
                                                                    "Authorization": "Bearer t"}},
          "user": {"email": "a@b.sa"}, "message": "فشل الاتصال بـ +966551234567 و sales@acme.sa",
          "extra": {"api_key": "sk-123", "note": "الرقم 0551234567"}}
    out = monitoring.before_send(ev, {})
    assert "cookies" not in out["request"] and "data" not in out["request"] and "user" not in out
    assert out["request"]["headers"] == {"User-Agent": "UA"} and "[email]" in out["request"]["url"]
    assert "+966551234567" not in out["message"] and "sales@acme.sa" not in out["message"]
    assert out["extra"]["api_key"] == "[filtered]" and "0551234567" not in out["extra"]["note"]


def test_sentry_disabled_without_dsn_and_settings_shows_status():
    assert not monitoring.enabled()
    with TestClient(app) as cl:
        otp_login(cl, "admin@sayaraup.test", "admin")
        html = cl.get("/settings").text
        assert "Sentry (مراقبة الأخطاء)" in html and "إرسال حدث تجريبي إلى Sentry" in html
        r = cl.post("/settings/sentry-test", follow_redirects=True)
        assert "Sentry غير مربوط" in r.text
