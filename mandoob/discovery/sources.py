"""مصادر الاكتشاف: خرائط جوجل (Places API الرسمية)، محرك بحث (Brave Search API)،
بحث بالذكاء الاصطناعي (Claude + web search)، واستيراد أدلة الأعمال المرخّصة.

ملاحظة امتثال: من خرائط جوجل نحفظ place_id والموقع الإلكتروني فقط لتحديد الشركة، ولا نخزّن
الهاتف أو البريد منها؛ بيانات الاتصال تُستخرج من موقع الشركة الرسمي أو من مصدر يجيز حفظها.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from typing import Iterator
from urllib.parse import urlparse

import httpx

from .. import ai
from ..config import settings
from ..costs import record_cost
from ..normalize import domain_of
from .base import Candidate, FoundContact, SourceError, SourceUnavailable

log = logging.getLogger(__name__)

# نطاقات لا تمثل موقع شركة (أدلة، شبكات اجتماعية، منصات حجز...)
NON_COMPANY_DOMAINS = {
    "google.com", "maps.google.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "youtube.com", "tiktok.com", "snapchat.com", "wikipedia.org", "haraj.com.sa",
    "yelp.com", "tripadvisor.com", "booking.com", "expedia.com", "kayak.com", "rentalcars.com",
    "discovercars.com", "qeeq.com", "vipcars.com", "economybookings.com", "yellowpages.com.sa",
    "daleeeel.com", "eyeofriyadh.com", "bayut.sa", "opensooq.com", "zawya.com", "dnb.com",
    "apple.com", "play.google.com", "apps.apple.com", "indeed.com", "bayt.com", "glassdoor.com",
    "naukrigulf.com", "maroof.sa", "wego.com", "argaam.com", "forbesmiddleeast.com",
}


def is_company_site(url: str | None) -> bool:
    d = domain_of(url)
    if not d:
        return False
    return not any(d == x or d.endswith("." + x) for x in NON_COMPANY_DOMAINS)


def _site_root(url: str) -> str:
    p = urlparse(url if "://" in url else "https://" + url)
    return f"{p.scheme or 'https'}://{p.hostname}"


class GooglePlacesSource:
    name = "google_places"
    URL = "https://places.googleapis.com/v1/places:searchText"
    FIELDS = ("places.id,places.displayName,places.formattedAddress,places.websiteUri,"
              "places.primaryTypeDisplayName,places.businessStatus,nextPageToken")

    def __init__(self, api_key: str | None = None, http: httpx.Client | None = None):
        self.api_key = api_key if api_key is not None else settings.google_places_api_key
        self.http = http or httpx.Client(timeout=30)

    def search(self, query: str, city: str, state: dict) -> Iterator[Candidate]:
        if not self.api_key:
            raise SourceUnavailable("GOOGLE_PLACES_API_KEY غير مضبوط")
        for _ in range(3):  # حتى 3 صفحات × 20 نتيجة لكل عبارة
            if state.get("done"):
                return
            body = {"textQuery": f"{query} في {city}", "languageCode": "ar", "regionCode": "SA",
                    "pageSize": 20}
            if state.get("page_token"):
                body["pageToken"] = state["page_token"]
            try:
                r = self.http.post(self.URL, json=body, headers={
                    "X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": self.FIELDS})
            except httpx.HTTPError as e:
                raise SourceError(f"خرائط جوجل: {e}") from e
            record_cost("google_places", 1)
            if r.status_code != 200:
                raise SourceError(f"خرائط جوجل أعادت {r.status_code}: {r.text[:200]}")
            data = r.json()
            for p in data.get("places", []):
                if p.get("businessStatus") not in (None, "OPERATIONAL"):
                    continue
                yield Candidate(
                    name=(p.get("displayName") or {}).get("text", ""),
                    source_type=self.name,
                    source_url=f"https://www.google.com/maps/place/?q=place_id:{p.get('id')}",
                    external_id=p.get("id"),
                    city=city,
                    activity=(p.get("primaryTypeDisplayName") or {}).get("text", ""),
                    website=p.get("websiteUri"),
                    snippet=p.get("formattedAddress", ""),
                )
            state["page_token"] = data.get("nextPageToken")
            if not state["page_token"]:
                state["done"] = True


class BraveSearchSource:
    name = "web_search"
    URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str | None = None, http: httpx.Client | None = None):
        self.api_key = api_key if api_key is not None else settings.brave_search_api_key
        self.http = http or httpx.Client(timeout=30)

    def search(self, query: str, city: str, state: dict) -> Iterator[Candidate]:
        if not self.api_key:
            raise SourceUnavailable("BRAVE_SEARCH_API_KEY غير مضبوط")
        for _ in range(3):
            if state.get("done"):
                return
            offset = int(state.get("offset", 0))
            arabic = bool(re.search(r"[؀-ۿ]", query))
            from ..targeting import CITY_EN
            q = f"{query} {city}" if arabic else f"{query} {CITY_EN.get(city, city)} Saudi Arabia"
            try:
                r = self.http.get(self.URL, params={"q": q, "country": "SA", "count": 20, "offset": offset,
                                                    "search_lang": "ar" if arabic else "en"},
                                  headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"})
            except httpx.HTTPError as e:
                raise SourceError(f"محرك البحث: {e}") from e
            record_cost("brave", 1)
            if r.status_code != 200:
                raise SourceError(f"محرك البحث أعاد {r.status_code}")
            results = (r.json().get("web") or {}).get("results", [])
            for item in results:
                url = item.get("url", "")
                if not is_company_site(url):
                    continue
                title = re.split(r"\s[|\-–—]\s", item.get("title", ""))[0].strip()
                yield Candidate(name=title, source_type=self.name, source_url=url, city=city,
                                website=_site_root(url), snippet=re.sub("<[^>]+>", "", item.get("description", "")))
            state["offset"] = offset + 1
            if len(results) < 20 or offset >= 8:
                state["done"] = True


AI_SEARCH_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["companies"],
    "properties": {"companies": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["name", "website", "city", "activity", "fit_reason", "evidence_url"],
        "properties": {
            "name": {"type": "string"}, "website": {"type": "string"}, "city": {"type": "string"},
            "activity": {"type": "string"}, "fit_reason": {"type": "string"},
            "evidence_url": {"type": "string"},
        }}}},
}


class AIWebSearchSource:
    """يكتشف شركات إضافية بالبحث على الويب عبر Claude. لا يُعتمد منه أي هاتف أو بريد؛
    بيانات الاتصال تُستخرج لاحقًا من موقع الشركة بالزاحف."""
    name = "ai_search"

    def __init__(self, exclude_domains_fn=None, per_call: int = 15):
        self.exclude_domains_fn = exclude_domains_fn or (lambda: [])
        self.per_call = per_call

    def search(self, query: str, city: str, state: dict) -> Iterator[Candidate]:
        if not settings.ai_enabled:
            raise SourceUnavailable("ANTHROPIC_API_KEY غير مضبوط")
        if state.get("done"):
            return
        known = ", ".join(self.exclude_domains_fn()[:150])
        prompt = (f"ابحث على الويب عن {self.per_call} شركة سعودية حقيقية ونشطة في مدينة {city} "
                  f"تطابق: «{query}». أعد فقط الشركات التي لها موقع إلكتروني رسمي. "
                  f"لا تكرر هذه النطاقات المعروفة لدينا: {known or 'لا يوجد'}. "
                  "لكل شركة: الاسم التجاري، الموقع الرسمي، المدينة، وصف النشاط، وجملة محددة لسبب مطابقتها "
                  "(مثل حجم الأسطول أو عدد الفروع) مع رابط الدليل. لا تخترع أي بيانات.")
        try:
            out = ai.ai_json("أنت باحث مبيعات دقيق. لا تذكر إلا ما وجدته فعلًا في نتائج البحث.", prompt,
                             AI_SEARCH_SCHEMA, web_search=True, max_searches=6, note="ai_search")
        except ai.AIUnavailable as e:
            raise SourceError(f"البحث بالذكاء الاصطناعي: {e}") from e
        state["done"] = True
        for c in out.get("companies", []):
            if not is_company_site(c.get("website")):
                continue
            yield Candidate(name=c["name"], source_type=self.name, source_url=c.get("evidence_url") or c["website"],
                            city=c.get("city") or city, activity=c.get("activity", ""),
                            website=_site_root(c["website"]), fit_hint=c.get("fit_reason", ""),
                            evidence_url=c.get("evidence_url"))


class DirectoryImportSource:
    """استيراد من دليل أعمال مرخّص (ملف CSV مصدّر من الدليل أو واجهته البرمجية).

    الأعمدة: name, city, activity, website, phone, phone_source_url, email, email_source_url, source_url
    لا يُقبل هاتف أو بريد بدون رابط مصدره.
    """
    name = "directory"

    @staticmethod
    def parse_csv(content: str, directory_name: str = "directory") -> list[Candidate]:
        rows = csv.DictReader(io.StringIO(content))
        out = []
        for row in rows:
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            if not row.get("name"):
                continue
            contacts = []
            if row.get("phone") and row.get("phone_source_url"):
                contacts.append(FoundContact("phone", row["phone"], row["phone_source_url"], directory_name))
            if row.get("email") and row.get("email_source_url"):
                contacts.append(FoundContact("email", row["email"], row["email_source_url"], directory_name))
            out.append(Candidate(name=row["name"], source_type="directory",
                                 source_url=row.get("source_url", ""), city=row.get("city", ""),
                                 activity=row.get("activity", ""), website=row.get("website") or None,
                                 contacts=contacts, fit_hint=row.get("fit_reason", ""),
                                 evidence_url=row.get("fit_evidence_url") or None))
        return out
