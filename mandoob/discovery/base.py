from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol


@dataclass
class FoundContact:
    kind: str          # phone / email
    raw: str
    source_url: str
    source_type: str = "website"
    label: str | None = None


@dataclass
class Candidate:
    name: str
    source_type: str                 # google_places / web_search / ai_search / directory / recorded
    source_url: str = ""
    city: str = ""
    activity: str = ""
    website: str | None = None
    name_en: str | None = None
    external_id: str | None = None
    snippet: str = ""
    fit_hint: str = ""                # سبب ترشيح أولي من المصدر (مثل الذكاء الاصطناعي)
    evidence_url: str | None = None
    contacts: list[FoundContact] = field(default_factory=list)  # بيانات منشورة مع مصدرها فقط
    lead_source: str | None = None
    signals: list = field(default_factory=list)  # إشارات توقيت مرفقة من المصدر (مثل خبر افتتاح مستودع)
    # من «البحث الآلي»: مرشح مؤهَّل مسبقًا بدليل من صفحة قرأها الوكيل
    prequalified: bool = False
    phrase: str | None = None
    operator_status: str | None = None
    entity_type: str | None = None


class SourceError(RuntimeError):
    """تعطل مصدر (شبكة، حصة، مفتاح). المنصة تحفظ نقطة التوقف وتنتقل لمصدر آخر."""


class SourceUnavailable(SourceError):
    """المصدر غير مفعّل (لا يوجد مفتاح)."""


class DiscoverySource(Protocol):
    name: str

    def search(self, query: str, city: str, state: dict) -> Iterator[Candidate]:
        """يعيد مرشحين. يحدّث `state` (نقطة التوقف) بعد كل صفحة حتى يمكن الاستئناف."""
        ...
