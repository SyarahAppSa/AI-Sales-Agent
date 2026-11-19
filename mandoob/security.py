"""ضوابط الحماية المشتركة (مراجعة الأمن — ملف secure-mandoob-platform):

- public_url_guard: يمنع الزاحف من الوصول لعناوين الشبكة الخاصة/المحلية/البنية الداخلية (SSRF) عند كل طلب وكل إعادة توجيه.
- harden_sheet: كل خلايا Excel نصوص لا صيغ (يمنع حقن الصيغ من بيانات المواقع الخارجية).
- SecurityHeaders + SameOriginGuard: ترويسات الحماية، ورفض الطلبات المغيّرة للحالة القادمة من موقع آخر (CSRF).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

MAX_PAGE_BYTES = 2_000_000  # حد ما يُقرأ من صفحة خارجية


class BlockedURL(Exception):
    pass


def _ip_blocked(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved
            or a.is_unspecified or getattr(a, "is_site_local", False)
            or (a.version == 6 and a.ipv4_mapped is not None and _ip_blocked(str(a.ipv4_mapped))))


def check_public_url(url: str, resolver=socket.getaddrinfo) -> None:
    """يرفع BlockedURL إن لم يكن الرابط http(s) على منفذ ويب عادي ويحل إلى عنوان عام فقط."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise BlockedURL(f"مخطط غير مسموح: {p.scheme}")
    if p.port not in (None, 80, 443):
        raise BlockedURL(f"منفذ غير مسموح: {p.port}")
    host = p.hostname
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
            raise BlockedURL(f"اسم داخلي: {host}")
        try:
            addrs = list({ai[4][0] for ai in resolver(host, None)})
        except OSError as e:
            raise BlockedURL(f"تعذر حل الاسم: {host}") from e
    for ip in addrs:
        if _ip_blocked(ip.split("%")[0]):
            raise BlockedURL(f"عنوان داخلي محجوب: {host} → {ip}")


def public_url_guard(request) -> None:
    """خطّاف httpx يعمل قبل كل طلب، بما فيه طلبات إعادة التوجيه."""
    check_public_url(str(request.url))


def harden_sheet(wb) -> None:
    """لا صيغ في ملفات التصدير: أي قيمة تبدأ بـ = (مثل اسم شركة من موقع خارجي) تُخزن نصًا."""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f" or (isinstance(cell.value, str) and cell.value[:1] in ("=", "+", "-", "@")
                                             and cell.data_type != "s"):
                    cell.data_type = "s"


CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; connect-src 'self'; "
       "frame-ancestors 'none'; form-action 'self'; base-uri 'self'; object-src 'none'")


class SecurityHeaders(BaseHTTPMiddleware):
    def __init__(self, app, https: bool = False):
        super().__init__(app)
        self.https = https

    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h.setdefault("Content-Security-Policy", CSP)
        if self.https:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path not in ("/healthz",) and not request.url.path.startswith("/static"):
            h.setdefault("Cache-Control", "no-store")
        return resp


class SameOriginGuard(BaseHTTPMiddleware):
    """الطلبات المغيّرة للحالة (POST وغيرها) تُقبل فقط من صفحات المنصة نفسها: Origin أو Referer من المضيف نفسه،
    و Sec-Fetch-Site ليست cross-site. (ملف الجلسة SameSite=Lax طبقة ثانية.)"""
    SAFE = ("GET", "HEAD", "OPTIONS")
    EXEMPT = ("/webhooks/",)

    async def dispatch(self, request, call_next):
        if request.method not in self.SAFE and not request.url.path.startswith(self.EXEMPT):
            host = request.headers.get("host", "")
            site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            o_host = urlparse(origin).netloc if origin and origin != "null" else ""
            if site == "cross-site" or origin == "null" or (o_host and o_host != host):
                return PlainTextResponse("طلب مرفوض: مصدره ليس صفحات المنصة", status_code=403)
        return await call_next(request)
