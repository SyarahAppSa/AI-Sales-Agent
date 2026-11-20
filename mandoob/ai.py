"""غلاف بسيط لاستدعاء Claude بمخرجات JSON منظمة، مع تسجيل التكلفة.

كل الدوال التي تستخدمه لها بديل حتمي (بدون ذكاء اصطناعي) حتى تعمل المنصة والاختبارات بدون مفتاح.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

# دولار لكل مليون رمز (إدخال، إخراج) — للتقدير فقط
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.0, 40.0),
}
WEB_SEARCH_PRICE_PER_1000 = 10.0


class AIUnavailable(RuntimeError):
    pass


_client = None


def client():
    global _client
    if not settings.ai_enabled:
        raise AIUnavailable("ANTHROPIC_API_KEY غير مضبوط")
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key, base_url=settings.anthropic_base_url,
                                      max_retries=3, timeout=600)
    return _client


def _record_cost(usage, web_searches: int = 0, campaign_id: int | None = None, note: str = "") -> float:
    pin, pout = PRICES.get(settings.ai_model, (4.0, 20.0))
    cost = 0.0
    if usage is not None:
        tin = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        tcache = getattr(usage, "cache_read_input_tokens", 0) or 0
        tout = getattr(usage, "output_tokens", 0) or 0
        cost = tin / 1e6 * pin + tcache / 1e6 * pin * 0.1 + tout / 1e6 * pout
    cost += web_searches / 1000 * WEB_SEARCH_PRICE_PER_1000
    try:
        from .costs import record_cost
        record_cost("claude", 1, cost, campaign_id=campaign_id, note=note)
    except Exception:  # pragma: no cover - لا نوقف العمل بسبب تسجيل التكلفة
        log.exception("cost record failed")
    return cost


def ai_json(system: str, prompt: str, schema: dict, *, web_search: bool = False, max_searches: int = 5,
            campaign_id: int | None = None, note: str = "", max_tokens: int = 16000) -> dict[str, Any]:
    """يستدعي Claude ويعيد JSON مطابقًا للمخطط."""
    import anthropic

    c = client()
    tools = []
    if web_search:
        tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches,
                      "user_location": {"type": "approximate", "country": "SA", "timezone": "Asia/Riyadh"}})
    messages: list[dict] = [{"role": "user", "content": prompt}]
    searches = 0
    for _ in range(4):  # يتعامل مع pause_turn في أدوات الخادم
        kwargs: dict[str, Any] = dict(
            model=settings.ai_model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": settings.ai_effort,
                           "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if tools:
            kwargs["tools"] = tools
        try:
            resp = c.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise AIUnavailable(f"تجاوز حد الطلبات: {e}") from e
        except anthropic.APIStatusError as e:
            raise AIUnavailable(f"خطأ من خدمة الذكاء الاصطناعي ({e.status_code})") from e
        except anthropic.APIConnectionError as e:
            raise AIUnavailable("تعذر الاتصال بخدمة الذكاء الاصطناعي") from e
        st = getattr(resp.usage, "server_tool_use", None)
        searches += (getattr(st, "web_search_requests", 0) or 0) if st else 0
        if resp.stop_reason == "pause_turn":
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
            continue
        _record_cost(resp.usage, searches, campaign_id, note)
        if resp.stop_reason == "refusal":
            raise AIUnavailable("رفض النموذج الطلب")
        texts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        if not texts:
            raise AIUnavailable("لا يوجد نص في الاستجابة")
        raw = texts[-1]
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                return json.loads(raw[start:end + 1])
            raise AIUnavailable("استجابة غير صالحة")
    raise AIUnavailable("لم يكتمل البحث")


def ai_agent(system: str, prompt: str, schema: dict, *, max_searches: int = 8, max_fetches: int = 20,
             note: str = "agent", campaign_id: int | None = None, effort: str | None = None,
             deadline_s: int = 420) -> tuple[dict, list, int]:
    """نداء وكيل بأداتي البحث وقراءة الصفحات. يعيد (JSON، كل كتل الاستجابة عبر الجولات، عدد عمليات البحث).

    الكتل تُعاد كاملة ليتحقق المستدعي أن كل هاتف/بريد ظهر نصًا في صفحة قرأها الوكيل فعلًا."""
    import anthropic

    c = client()
    model = settings.ai_agent_model or settings.ai_model
    tools = [
        {"type": settings.ai_search_tool, "name": "web_search", "max_uses": max_searches,
         "user_location": {"type": "approximate", "country": "SA", "timezone": "Asia/Riyadh"}},
        {"type": settings.ai_fetch_tool, "name": "web_fetch", "max_uses": max_fetches, "max_content_tokens": 12000},
    ]
    messages: list[dict] = [{"role": "user", "content": prompt}]
    all_blocks: list = []
    searches = 0
    import time as _time
    t_end = _time.time() + deadline_s  # مهلة كلية للنداء: لا يعلق البحث اليومي على نداء واحد
    for _ in range(8):  # جولات pause_turn للأدوات الخادمية
        try:
            with c.beta.messages.stream(
                model=model, max_tokens=32000, system=system, messages=messages, tools=tools,
                output_config={"effort": effort or settings.ai_agent_effort,
                               "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                timeout=min(180, deadline_s),
            ) as stream:
                for _ev in stream:
                    if _time.time() > t_end:
                        raise AIUnavailable("تجاوز النداء مهلته — يُنتقل للمصدر/العبارة التالية")
                resp = stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise AIUnavailable(f"تجاوز حد الطلبات: {e}") from e
        except anthropic.APIStatusError as e:
            raise AIUnavailable(f"خطأ من خدمة الذكاء الاصطناعي ({e.status_code}): {str(e)[:200]}") from e
        except anthropic.APIConnectionError as e:
            raise AIUnavailable("تعذر الاتصال بخدمة الذكاء الاصطناعي") from e
        all_blocks.extend(resp.content)
        st = getattr(resp.usage, "server_tool_use", None)
        searches += (getattr(st, "web_search_requests", 0) or 0) if st else 0
        _record_cost(resp.usage, 0, campaign_id, note)
        if resp.stop_reason == "pause_turn":
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
            continue
        if resp.stop_reason == "refusal":
            raise AIUnavailable("رفض النموذج الطلب")
        _record_cost(None, searches, campaign_id, note + ":searches")
        texts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        if not texts:
            raise AIUnavailable("لا يوجد نص في الاستجابة")
        raw = texts[-1]
        try:
            return json.loads(raw), all_blocks, searches
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                return json.loads(raw[start:end + 1]), all_blocks, searches
            raise AIUnavailable("استجابة غير صالحة")
    raise AIUnavailable("لم يكتمل البحث")


def today_key() -> date:
    from .timeutil import riyadh_today
    return riyadh_today()
