"""زاحف مواقع الشركات: يقرأ الصفحة الرئيسية وصفحات التواصل/الفروع ويستخرج
الهاتف التجاري والبريد المؤسسي المنشورين مع رابط الصفحة التي ظهرا فيها.

- يحترم robots.txt ويحدّ عدد الصفحات لكل موقع.
- لا يُنشئ أي بريد بالتخمين: يقبل فقط ما هو منشور نصًا أو في رابط mailto أو حماية Cloudflare للبريد.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ..config import settings
from ..normalize import (GENERIC_EMAIL_DOMAINS, domain_of, normalize_email, normalize_phone)
from .base import FoundContact

log = logging.getLogger(__name__)

CONTACT_HINTS = ["contact", "اتصل", "تواصل", "اتصال", "about", "من-نحن", "من نحن", "branches", "فروع",
                 "locations", "support", "customer", "عملاء", "corporate", "business", "الشركات", "b2b"]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# أرقام سعودية: +966/00966/0 ثم 5x أو 1x..، والرقم الموحد 920 والمجاني 800
PHONE_RE = re.compile(
    r"(?:(?:\+|00)\s*966[\s\-]*\(?0?\)?[\s\-]*|\b0)(?:5\d|1[1-7]|[2-7])[\s\-]*\d{3}[\s\-]*\d{3,4}\b"
    r"|\b920\s*0?\d{2}[\s\-]*\d{4}\b|\b9200[\s\-]*\d{5}\b|\b800[\s\-]*\d{3}[\s\-]*\d{4}\b"
)


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.mailtos: list[str] = []
        self.tels: list[str] = []
        self.cf_emails: list[str] = []
        self.text_parts: list[str] = []
        self.title = ""
        self.meta_description = ""
        self._in_title = False
        self._skip = 0
        self._href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta" and (a.get("name") or "").lower() == "description":
            self.meta_description = a.get("content") or ""
        if a.get("data-cfemail"):
            self.cf_emails.append(a["data-cfemail"])
        if tag == "a":
            href = (a.get("href") or "").strip()
            if href.lower().startswith("mailto:"):
                self.mailtos.append(href[7:].split("?")[0])
            elif href.lower().startswith("tel:"):
                self.tels.append(href[4:])
            elif "/cdn-cgi/l/email-protection#" in href:
                self.cf_emails.append(href.split("#", 1)[1])
            self._href = href
            self._anchor_text = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._anchor_text).strip()))
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self.text_parts.append(data)
        if self._href is not None:
            self._anchor_text.append(data.strip())


def decode_cfemail(enc: str) -> str | None:
    try:
        key = int(enc[:2], 16)
        return "".join(chr(int(enc[i:i + 2], 16) ^ key) for i in range(2, len(enc), 2))
    except ValueError:
        return None


@dataclass
class PageExtract:
    url: str
    title: str = ""
    description: str = ""
    text: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)


def extract_page(url: str, html: str) -> PageExtract:
    p = _PageParser()
    try:
        p.feed(html)
    except Exception:  # pragma: no cover - HTML معطوب
        pass
    text = re.sub(r"\s+", " ", unescape(" ".join(p.text_parts))).strip()
    # فك التمويه الشائع: name [at] domain [dot] com
    deob = re.sub(r"\s*(\[at\]|\(at\)| at )\s*", "@", text, flags=re.I)
    deob = re.sub(r"\s*(\[dot\]|\(dot\))\s*", ".", deob, flags=re.I)
    emails = set(p.mailtos) | set(EMAIL_RE.findall(deob))
    emails |= {e for e in (decode_cfemail(x) for x in p.cf_emails) if e}
    phones = set(p.tels) | {m.group(0) for m in PHONE_RE.finditer(text)}
    return PageExtract(url=url, title=p.title.strip(), description=p.meta_description.strip(), text=text[:6000],
                       emails=sorted(emails), phones=sorted(phones), links=p.links)


@dataclass
class SiteResult:
    website: str
    contacts: list[FoundContact] = field(default_factory=list)
    pages: list[PageExtract] = field(default_factory=list)
    error: str | None = None

    @property
    def summary_text(self) -> str:
        parts = []
        for pg in self.pages[:3]:
            parts.append(f"[{pg.url}] {pg.title} — {pg.description} — {pg.text[:1200]}")
        return "\n".join(parts)


class Crawler:
    def __init__(self, http: httpx.Client | None = None, max_pages: int = 6):
        from ..security import public_url_guard
        # كل طلب (وكل إعادة توجيه) يُفحص: لا شبكات خاصة/محلية ولا خدمات البنية الداخلية (SSRF)
        self.http = http or httpx.Client(timeout=httpx.Timeout(15, connect=8), follow_redirects=True, max_redirects=5,
                                         event_hooks={"request": [public_url_guard]},
                                         headers={"User-Agent": settings.crawler_user_agent,
                                                  "Accept-Language": "ar,en;q=0.8"})
        self.max_pages = max_pages
        self._robots: dict[str, RobotFileParser | None] = {}

    def _allowed(self, url: str) -> bool:
        host = urlparse(url).scheme + "://" + (urlparse(url).hostname or "")
        if host not in self._robots:
            rp = RobotFileParser()
            try:
                r = self.http.get(host + "/robots.txt")
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp.parse([])
                self._robots[host] = rp
            except (httpx.HTTPError, Exception):  # noqa: BLE001 — يشمل BlockedURL
                self._robots[host] = None
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(settings.crawler_user_agent, url)

    def fetch(self, url: str) -> str | None:
        if not self._allowed(url):
            log.info("robots.txt يمنع %s", url)
            return None
        from ..security import MAX_PAGE_BYTES, BlockedURL
        try:
            with self.http.stream("GET", url) as r:
                ctype = r.headers.get("content-type", "")
                if r.status_code != 200 or "html" not in ctype:
                    return None
                buf = b""
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_PAGE_BYTES:  # حد حجم المحتوى المقروء
                        break
                return buf[:MAX_PAGE_BYTES].decode(r.encoding or "utf-8", errors="replace")
        except BlockedURL as e:
            log.warning("blocked fetch %s: %s", url, e)
            return None

    def crawl(self, website: str) -> SiteResult:
        if "://" not in website:
            website = "https://" + website
        res = SiteResult(website=website)
        site_domain = domain_of(website)
        queue = [website]
        seen: set[str] = set()
        while queue and len(res.pages) < self.max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                html = self.fetch(url)
            except httpx.HTTPError as e:
                if not res.pages:
                    res.error = f"تعذر فتح الموقع: {type(e).__name__}"
                continue
            if not html:
                continue
            page = extract_page(url, html)
            res.pages.append(page)
            if len(res.pages) == 1:
                # أولوية لصفحات التواصل والفروع
                scored = []
                for href, txt in page.links:
                    full = urljoin(url, href)
                    if domain_of(full) != site_domain or full in seen:
                        continue
                    blob = (href + " " + txt).lower()
                    if any(h in blob for h in CONTACT_HINTS):
                        scored.append(full.split("#")[0])
                for u in dict.fromkeys(scored):
                    queue.append(u)
        res.contacts = self._contacts_from_pages(res.pages, site_domain)
        return res

    @staticmethod
    def _contacts_from_pages(pages: list[PageExtract], site_domain: str | None) -> list[FoundContact]:
        out: list[FoundContact] = []
        seen: set[tuple[str, str]] = set()
        for pg in pages:
            for raw in pg.emails:
                e = normalize_email(raw)
                if not e:
                    continue
                ed = domain_of(e)
                # البريد المؤسسي: نطاق الشركة نفسها أو نطاق فرعي/شقيق واضح. نرفض البريد المجاني.
                if ed in GENERIC_EMAIL_DOMAINS:
                    continue
                if site_domain and not (ed == site_domain or ed.endswith("." + site_domain)
                                        or site_domain.endswith("." + ed)
                                        or ed.split(".")[0] == site_domain.split(".")[0]):
                    continue
                if ("email", e) not in seen:
                    seen.add(("email", e))
                    out.append(FoundContact("email", raw, pg.url, "website"))
            for raw in pg.phones:
                p = normalize_phone(raw)
                if p and ("phone", p) not in seen:
                    seen.add(("phone", p))
                    out.append(FoundContact("phone", raw.strip(), pg.url, "website"))
        return out
