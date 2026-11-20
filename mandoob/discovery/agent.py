"""البحث الآلي عن العملاء: وكيل ذكاء اصطناعي يبحث بنفسه على الويب ويتتبع كل نتيجة حتى بيانات التواصل.

كيف يعمل:
1. تخطيط البحث: يحوّل وصف المدير («أي نشاط عنده سيارات») إلى عشرات عبارات البحث بالعربية والإنجليزية
   (عبارات التشغيل، أدلة الناقلين والمقاولين والمصانع، قوائم العارضين، إعلانات التوسع والتوظيف).
2. لكل عبارة ومدينة: يستخدم Claude أداتي البحث وقراءة الصفحات (web_search / web_fetch) على خوادم Anthropic،
   فيكتشف المنشآت، ويفتح موقعها الرسمي وصفحة «اتصل بنا» والفروع، ويستخرج الهاتف والبريد المنشورين.
3. التحقق الآلي (ضد الاختلاق): لا يُقبل أي هاتف أو بريد إلا إذا وُجد نصًا في صفحة قرأها الوكيل فعلًا من نفس
   الرابط/النطاق الذي ذكره مصدرًا. القيم التي لا تظهر في الصفحات المقروءة تُرفض وتُسجَّل.
4. يعيد مرشحين جاهزين مع سبب الترشيح ورابطه، فتكمل المنصة: منع التكرار، الاكتمال، الإسناد.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Iterator

from .. import ai
from ..config import settings
from ..normalize import domain_of, normalize_arabic, normalize_email, normalize_phone
from .base import Candidate, FoundContact, SourceError, SourceUnavailable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- تخطيط عبارات البحث

# مكتبة عبارات لأنشطة تستخدم المركبات في عملها (تُستخدم إن لم يتوفر الذكاء الاصطناعي، وتُكمِّل خطته)
VEHICLE_ACTIVITY_QUERIES_AR = [
    "صيانة متنقلة", "صيانة تكييف متنقلة", "فني صيانة يجي للبيت", "توصيل وتركيب", "توصيل وتركيب أثاث",
    "توزيع مبرد", "توزيع مواد غذائية", "توصيل مياه شرب", "نقل موظفين", "نقل طالبات", "تأجير معدات مع النقل",
    "تأجير معدات ثقيلة مع السائق", "سطحة لنقل السيارات", "ونش سحب سيارات", "نقل عفش", "نقل أثاث مع الفك والتركيب",
    "صهاريج مياه", "شفط بيارات", "مكافحة حشرات", "غسيل سيارات متنقل", "تموين حفلات وتوصيل", "توصيل طلبات للشركات",
    "مقاول نقل بضائع", "مكتب نقليات", "شحن بري بين المدن", "تأجير باصات", "ليموزين للشركات", "نقل مبرد",
    "توزيع أدوية", "مخابز مركزية توزيع", "خدمات نظافة للمنشآت", "تنسيق حدائق وصيانة", "تركيب كاميرات وأنظمة أمن",
    "صيانة مصاعد", "توريد مواد بناء مع التوصيل", "خرسانة جاهزة", "نقل نفايات", "ورشة متنقلة",
]
VEHICLE_ACTIVITY_QUERIES_EN = [
    "mobile maintenance service", "delivery and installation", "refrigerated distribution", "staff transportation",
    "equipment rental with transport", "tow truck service", "furniture moving company", "water tanker delivery",
    "pest control company", "catering delivery", "fleet supervisor job", "delivery driver vacancies company",
]
SOURCE_HUNTING_QUERIES = [
    "دليل شركات النقل {city}", "دليل المقاولين {city}", "دليل المصانع {city}", "قائمة العارضين معرض النقل والخدمات اللوجستية",
    "قائمة العارضين معرض الأغذية {city}", "مطلوب مشرف أسطول {city}", "مطلوب سائق توزيع {city}",
    "افتتاح مستودع جديد {city}", "ترسية عقد تشغيل وصيانة {city}",
]

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["queries"],
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
}


def plan_queries(description: str, cities: list[str], limit: int = 60) -> list[str]:
    """يحوّل وصف المدير إلى عبارات بحث متنوعة (بدون اسم المدينة؛ تُضاف عند التنفيذ)."""
    base = VEHICLE_ACTIVITY_QUERIES_AR + VEHICLE_ACTIVITY_QUERIES_EN + SOURCE_HUNTING_QUERIES
    if not settings.ai_enabled:
        return base[:limit]
    try:
        out = ai.ai_json(
            "أنت مخطط بحث لفريق مبيعات سعودي يبحث عن منشآت وأصحاب أنشطة يستخدمون مركبات في عملهم.",
            f"وصف المدير: {description}\nالمدن: {', '.join(cities)}\n\n"
            f"اكتب حتى {limit} عبارة بحث متنوعة بالعربية والإنجليزية يستخدمها أصحاب هذه الأنشطة أو عملاؤهم "
            "(مثل «صيانة متنقلة»، «توصيل وتركيب»، «توزيع مبرد»، «نقل موظفين»، «تأجير معدات مع النقل»)، "
            "وعبارات تقود إلى مصادر قوائم: أدلة الناقلين والمقاولين والمصانع، قوائم العارضين في المعارض، "
            "إعلانات العقود والتوسع والتوظيف (مثل «مطلوب مشرف أسطول»). لا تكرر المعنى. "
            "استخدم {city} حيث يلزم اسم المدينة داخل العبارة، وإلا فلا تذكر المدينة.",
            PLAN_SCHEMA, note="plan", max_tokens=4000)
        qs = [q.strip() for q in out["queries"] if q.strip()]
        seen, merged = set(), []
        for q in qs + base:
            k = normalize_arabic(q).lower()
            if k not in seen:
                seen.add(k)
                merged.append(q)
        return merged[:max(limit, len(qs))]
    except ai.AIUnavailable as e:
        log.warning("plan fallback: %s", e)
        return base[:limit]


# ---------------------------------------------------------------- الوكيل

AGENT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["prospects"],
    "properties": {"prospects": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["name", "entity_type", "activity", "city", "address", "address_url", "website", "vehicle_evidence",
                     "evidence_url", "evidence_quote", "operator_status", "contacts", "discovered_via"],
        "properties": {
            "name": {"type": "string"},
            "entity_type": {"type": "string", "enum": ["company", "establishment", "factory", "sole_proprietor",
                                                       "association", "private_individual"]},
            "activity": {"type": "string"},
            "city": {"type": "string", "description": "مدينة عنوان المنشأة أو فرعها المنشور — لا مدن التغطية"},
            "address": {"type": "string", "description": "العنوان المنشور حرفيًا (الحي والمدينة) أو فارغ"},
            "address_url": {"type": "string", "description": "رابط الصفحة التي نُشر فيها العنوان"},
            "website": {"type": "string"},
            "vehicle_evidence": {"type": "string",
                                 "description": "جملة عربية تشرح لماذا يستخدم مركبات — فارغ إن لم يوجد دليل منشور"},
            "evidence_url": {"type": "string"},
            "evidence_quote": {"type": "string", "description": "اقتباس حرفي قصير من الصفحة"},
            "operator_status": {"type": "string", "enum": ["self", "contracted", "unknown"]},
            "discovered_via": {"type": "string", "description": "كيف اكتُشف: بحث، دليل، معرض، إعلان توظيف..."},
            "contacts": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["kind", "value", "source_url"],
                "properties": {"kind": {"type": "string", "enum": ["phone", "email"]},
                               "value": {"type": "string"}, "source_url": {"type": "string"}}}},
        }}}},
}

AGENT_SYSTEM = """أنت باحث مبيعات ميداني لشركة «سيارة آب» (حلول إدارة المركبات والوقود) في السعودية.
مهمتك اكتشاف منشآت داخل السعودية ضمن النشاط المطلوب — صغيرة أو كبيرة: من محل واحد لديه سيارة توصيل إلى شركة بمئات
المركبات — وجمع بيانات التواصل المنشورة لأعمالهم. الأولوية لمن تعلن أسطولًا أو توصيلًا أو فرقًا ميدانية أو نقلًا أو زيارات
للمواقع؛ وإن لم تجد دليلًا منشورًا على استخدام المركبات فأدرج المنشأة ضمن نشاطها واترك vehicle_evidence و evidence_quote
فارغين (تُسجّل «غير مؤكد» ويتحقق منها الفريق). entity_type نوع الجهة: company شركة، establishment مؤسسة فردية،
association جمعية أو جهة غير ربحية، factory مصنع، sole_proprietor صاحب نشاط حر. القواعد الصارمة:
- استخدم البحث ثم افتح الصفحات فعلًا (الموقع الرسمي، صفحة «اتصل بنا»، صفحات الفروع، صفحة النشاط في دليل أعمال).
- لا تكتب هاتفًا أو بريدًا إلا إذا رأيته منشورًا نصًا في صفحة فتحتها، واكتب رابط تلك الصفحة نفسها في source_url.
  ممنوع تخمين بريد (مثل info@النطاق) أو تكوين رقم. إن لم تجد البريد فاترك الاتصال ناقصًا.
- الهاتف والبريد يجب أن يكونا من موقع المنشأة الرسمي نفسه (اكتبه في website). بريد بنطاق آخر (مطوّر الموقع، منصة،
  شركة أخرى) لا يُقبل؛ البريد المجاني (gmail وغيره) يُقبل فقط إن كان منشورًا في موقع المنشأة. لا تأخذ الهاتف من موقع منشأة أخرى.
- أصحاب الأنشطة الفردية (سطحة، فني متنقل...) يُقبلون فقط ببيانات التواصل التي نشروها هم لأعمالهم.
- لا تجمع بيانات أفراد بسياراتهم الخاصة؛ إن صادفت ذلك فصنّفه private_individual ولن يُستخدم.
- vehicle_evidence جملة محددة مستندة إلى الصفحة (مثل «تذكر في موقعها أنها توزّع منتجاتها بأسطول مبرد في المنطقة الغربية»)
  مع evidence_url واقتباس حرفي evidence_quote. إن أعلنت التوصيل دون توضيح من يشغّل المركبات فـ operator_status = unknown.
- city و address: موقع المنشأة أو فرعها كما نشرته (حي، شارع، مدينة)، مع رابط الصفحة. عبارة مثل «نوصّل إلى جميع مناطق
  المملكة» ليست عنوانًا ولا تجعل المنشأة في تلك المدينة. لا تخلط بين المدن المتشابهة (ضباء في تبوك ليست دبي).
- تجنّب الأسماء والنطاقات المعروفة لدينا المذكورة في الطلب، وتجنب الأدلة نفسها والمنصات والوسطاء كعملاء."""


@dataclass
class FetchedPage:
    url: str
    text: str


@dataclass
class AgentResult:
    candidates: list[Candidate] = field(default_factory=list)
    rejected_values: list[dict] = field(default_factory=list)
    pages_read: int = 0
    searches: int = 0


def _collect_pages(blocks) -> tuple[list[FetchedPage], list[str]]:
    """الصفحات التي قرأها الوكيل فعلًا (من نتائج web_fetch) + مخرجات الفلترة الديناميكية."""
    pages, extra = [], []
    for b in blocks:
        t = getattr(b, "type", None)
        if t == "web_fetch_tool_result":
            c = getattr(b, "content", None)
            if getattr(c, "type", None) == "web_fetch_result":
                doc = getattr(c, "content", None)
                src = getattr(doc, "source", None)
                data = getattr(src, "data", None)
                if isinstance(data, str) and getattr(src, "type", "text") == "text":
                    pages.append(FetchedPage(url=c.url, text=data))
        elif t in ("bash_code_execution_tool_result", "code_execution_tool_result"):
            c = getattr(b, "content", None)
            out = getattr(c, "stdout", None)
            if isinstance(out, str) and out:
                extra.append(out)
    return pages, extra


def _digits(s: str) -> str:
    return re.sub(r"\D", "", normalize_arabic(s or ""))


def value_on_pages(kind: str, value: str, source_url: str, pages: list[FetchedPage], extra: list[str]) -> str | None:
    """يعيد رابط الصفحة التي يظهر فيها البيان نصًا (من نفس النطاق)، أو None إن لم يظهر."""
    sdom = domain_of(source_url)
    if kind == "email":
        e = normalize_email(value)
        if not e:
            return None

        def found(text: str) -> bool:
            t = text.lower().replace("[at]", "@").replace("(at)", "@").replace(" @ ", "@")
            return e in t
    else:
        p = normalize_phone(value)
        if not p:
            return None
        nat = p[4:] if p.startswith("+966") else p.lstrip("+")
        variants = {nat, "0" + nat, "966" + nat}

        def found(text: str) -> bool:
            d = _digits(text)
            return any(v in d for v in variants)
    same = [pg for pg in pages if domain_of(pg.url) == sdom] or []
    for pg in sorted(same, key=lambda pg: pg.url != source_url):
        if found(pg.text):
            return pg.url
    # مخرجات الفلترة الديناميكية: تُقبل فقط إن ذكرت رابط المصدر نفسه بجوار القيمة
    for out in extra:
        if source_url and source_url in out and found(out):
            return source_url
    return None


def quote_on_pages(quote: str, url: str, pages: list[FetchedPage]) -> bool:
    q = re.sub(r"\s+", " ", normalize_arabic(quote or "")).strip().lower()
    if len(q) < 8:
        return False
    for pg in pages:
        if domain_of(pg.url) == domain_of(url):
            t = re.sub(r"\s+", " ", normalize_arabic(pg.text)).lower()
            if q[:60] in t:
                return True
    return False


def _saudi_or_raw(city: str) -> str:
    from ..segments import saudi_city
    return saudi_city(city) or city


def same_entity(url: str, name: str, website: str | None, pages: list[FetchedPage]) -> bool:
    """هل صفحة المصدر تعود للجهة نفسها؟ نعم إن كانت من نطاق موقعها، أو ذكرت اسمها نصًا
    (مثل صفحة النشاط على منصة تواصل أو دليل أعمال)."""
    from ..normalize import normalize_name
    if website and domain_of(url) == domain_of(website if "://" in website else "https://" + website):
        return True
    words = [w for w in normalize_name(name).split() if len(w) >= 3]
    if not words:
        return False
    for pg in pages:
        if pg.url == url or domain_of(pg.url) == domain_of(url):
            t = normalize_arabic(pg.text).lower()
            hits = sum(1 for w in words if w in t)
            if hits >= max(1, (len(words) + 1) // 2):
                return True
    return False


def _location_on_pages(p: dict, pages: list[FetchedPage]) -> bool:
    """هل موقع المنشأة منشور فعلًا؟ العنوان ظهر نصًا في صفحة قرأها الوكيل، أو اسم المدينة ظهر في صفحة من موقعها."""
    addr = (p.get("address") or "").strip()
    if addr and quote_on_pages(addr, p.get("address_url") or p.get("website") or "", pages):
        return True
    from ..geo import PLACES, resolve_city
    pid = resolve_city(addr) or resolve_city(p.get("city") or "")
    if not pid:
        return False
    names = [PLACES[pid]["ar"], PLACES[pid]["en"], *PLACES[pid]["syn"]]
    site = domain_of(p["website"]) if p.get("website") else None
    for pg in pages:
        if site and domain_of(pg.url) == site or pg.url == p.get("address_url"):
            t = normalize_arabic(pg.text).lower()
            if any(normalize_arabic(n).lower() in t for n in names):
                return True
    return False


def parse_agent_output(out: dict, blocks, query: str, city: str) -> AgentResult:
    pages, extra = _collect_pages(blocks)
    res = AgentResult(pages_read=len(pages))
    for p in out.get("prospects", []):
        if p["entity_type"] == "private_individual":
            res.rejected_values.append({"name": p["name"], "why": "فرد بسيارة خاصة — لا يُجمع من البحث"})
            continue
        contacts: list[FoundContact] = []
        for ct in p["contacts"]:
            where = value_on_pages(ct["kind"], ct["value"], ct["source_url"], pages, extra)
            if where and not same_entity(where, p["name"], p["website"], pages):
                res.rejected_values.append({"name": p["name"], "kind": ct["kind"], "value": ct["value"],
                                            "why": "صفحة المصدر لا تعود للجهة نفسها"})
                continue
            if where:
                contacts.append(FoundContact(ct["kind"], ct["value"], where, "ai_fetch"))
            else:
                res.rejected_values.append({"name": p["name"], "kind": ct["kind"], "value": ct["value"],
                                            "why": "لم يظهر في الصفحات التي قرأها الوكيل"})
        evidence_ok = bool(p["vehicle_evidence"].strip()) and quote_on_pages(p["evidence_quote"], p["evidence_url"], pages)
        if p["vehicle_evidence"].strip():
            hint = p["vehicle_evidence"] if evidence_ok else p["vehicle_evidence"] + " (الاقتباس لم يُتحقق منه نصًا)"
        else:
            hint = f"منشأة ضمن نشاط «{p['activity'] or query}» — لم يُعثر على دليل منشور لاستخدام المركبات (غير مؤكد)"
        website = p["website"] if p["website"].startswith("http") else (f"https://{p['website']}" if p["website"] else "")
        from .sources import is_company_site, _site_root
        cand = Candidate(
            name=p["name"].strip(), source_type="ai_agent",
            source_url=p["evidence_url"] or (contacts[0].source_url if contacts else website),
            city=_saudi_or_raw(p["city"] or city), activity=p["activity"],
            website=_site_root(website) if website and is_company_site(website) else None,
            snippet=f"اكتُشف عبر: {p['discovered_via']} — عبارة البحث: {query}",
            fit_hint=hint,
            evidence_url=p["evidence_url"] or None, contacts=contacts)
        cand.prequalified = True
        cand.phrase = p["evidence_quote"][:300] if evidence_ok else None
        cand.operator_status = p["operator_status"]
        cand.entity_type = p["entity_type"]
        cand.vehicle_indicator = "confirmed" if evidence_ok else "unconfirmed"
        cand.address = (p.get("address") or "").strip()[:300] or None
        cand.location_verified = _location_on_pages(p, pages)
        res.candidates.append(cand)
    return res


class AgentSource:
    """مصدر «البحث الآلي»: نداء وكيل لكل (عبارة بحث × مدينة)."""
    name = "ai_agent"

    def __init__(self, known_fn=None, per_call: int = 10, max_searches: int | None = None, max_fetches: int | None = None):
        max_searches = max_searches or settings.ai_agent_searches
        max_fetches = max_fetches or settings.ai_agent_fetches
        self.known_fn = known_fn or (lambda: [])
        self.per_call, self.max_searches, self.max_fetches = per_call, max_searches, max_fetches
        self.last: AgentResult | None = None
        self.campaign_id: int | None = None  # تُنسب تكلفة النداء للحملة (تقارير التكلفة حسب البحث/الحملة)

    def search(self, query: str, city: str, state: dict) -> Iterator[Candidate]:
        if not settings.ai_enabled:
            raise SourceUnavailable("مفتاح Claude غير مضبوط")
        if state.get("done"):
            return
        q = query.replace("{city}", city)
        known = ", ".join(self.known_fn()[:200])
        prompt = (f"المدينة: {city}\nعبارة البحث: «{q}»\n"
                  f"اعثر على حتى {self.per_call} منشأة في {city} تمارس هذا النشاط فعليًا داخل السعودية، "
                  "وقدّم من تعلن أسطولًا أو توصيلًا أو فرقًا ميدانية أو نقلًا. "
                  "الأولوية لمن له هاتف سعودي وبريد إلكتروني منشوران معًا؛ ابحث عن البريد في صفحة «اتصل بنا» وتذييل "
                  "الموقع و«من نحن» وصفحة النشاط في الأدلة ومنصات التواصل. افتح الصفحات وانقل البيانات حرفيًا مع رابطها.\n"
                  f"أسماء ونطاقات لدينا مسبقًا (تجنّبها): {known or 'لا يوجد'}")
        try:
            out, blocks, searches = ai.ai_agent(AGENT_SYSTEM, prompt, AGENT_SCHEMA, max_searches=self.max_searches,
                                                max_fetches=self.max_fetches, note="agent",
                                                campaign_id=self.campaign_id)
        except ai.AIUnavailable as e:
            raise SourceError(f"البحث الآلي: {e}") from e
        state["done"] = True
        res = parse_agent_output(out, blocks, q, city)
        res.searches = searches
        self.last = res
        state["rejected_values"] = len(res.rejected_values)
        state["pages_read"] = res.pages_read
        for r in res.rejected_values[:20]:
            # السجل بلا بيانات العملاء (لا اسم ولا هاتف ولا بريد): النوع والسبب فقط
            log.info("agent rejected kind=%s why=%s", r.get("kind", "-"), r.get("why", ""))
        yield from res.candidates


def agent_complete(name: str, city: str, website: str | None, missing: list[str],
                   note: str = "complete") -> list[FoundContact]:
    """يبحث عن البيان الناقص (هاتف/بريد) لشركة معروفة، ويقبله فقط إن ظهر نصًا في صفحة قرأها الوكيل."""
    if not settings.ai_enabled:
        return []
    kinds = " و".join(missing)
    prompt = (f"الشركة: {name}\nالمدينة: {city}\nالموقع: {website or 'غير معروف'}\n"
              f"ابحث عن {kinds} المنشور لأعمال هذه الشركة (الموقع الرسمي، صفحة اتصل بنا، الفروع، أو صفحتها في دليل أعمال "
              "يسمح بنشر بيانات التواصل). افتح الصفحة وانقل القيمة حرفيًا مع رابطها. أعد عنصرًا واحدًا في prospects.")
    try:
        out, blocks, _ = ai.ai_agent(AGENT_SYSTEM, prompt, AGENT_SCHEMA, max_searches=4, max_fetches=8, note=note)
    except ai.AIUnavailable:
        return []
    res = parse_agent_output(out, blocks, "استكمال", city)
    found = []
    for c in res.candidates:
        found.extend(c.contacts)
    return found
