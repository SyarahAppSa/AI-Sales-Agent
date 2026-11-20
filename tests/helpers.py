"""الدخول في الاختبارات بالطريقة الحقيقية: طلب رمز، ثم قراءته من رسالة البريد التي وصلت (صندوق التجربة)."""
import re
from email import message_from_bytes, policy

from sqlalchemy import select


def ensure_mail():
    from mandoob.db import session_scope
    from mandoob.models import MailAccount
    with session_scope() as s:
        if not s.scalar(select(MailAccount).where(MailAccount.active.is_(True))):
            s.add(MailAccount(provider="sandbox", sender_address="sales@sayaraup.test", sender_name="سيارة آب"))


def last_code_for(email: str) -> str | None:
    from mandoob.mail.sandbox import ROOT
    files = sorted((ROOT / "outbox").glob("*.eml"), key=lambda p: p.stat().st_mtime_ns)
    for f in reversed(files):
        m = message_from_bytes(f.read_bytes(), policy=policy.default)
        if str(m["To"]).strip().lower() == email.lower() and ("رمز الدخول" in str(m["Subject"]) or "رمز دخولك" in str(m["Subject"]) or "ign-in code" in str(m["Subject"])):
            body = m.get_body(preferencelist=("plain",)).get_content()
            return re.search(r"\b(\d{6})\b", body).group(1)
    return None


def otp_login(client, email: str, portal: str = "admin"):
    base = "/admin/login" if portal == "admin" else "/portal/login"
    ensure_mail()
    r = client.post(f"{base}/request", data={"email": email}, follow_redirects=False)
    assert r.status_code == 303
    code = last_code_for(email)
    assert code, f"no code email for {email}"
    r = client.post(f"{base}/verify", data={"code": code}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == ("/" if portal == "admin" else "/portal"), r.headers
    return code
