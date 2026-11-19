"""فحص كل مصدر بحث على حدة من الخادم: هل هو متاح؟ ماذا أعاد؟ وما المطلوب إن لم يكن مُعدًّا؟ (لا يحفظ شيئًا)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mandoob.config import settings  # noqa: E402
from mandoob.discovery.base import SourceError, SourceUnavailable  # noqa: E402

CITY = sys.argv[1] if len(sys.argv) > 1 else "جدة"
QUERY = sys.argv[2] if len(sys.argv) > 2 else "صيانة تكييف متنقلة"


def line(*a):
    print("SOURCE", *a, flush=True)


# 1) Claude API
try:
    import anthropic
    c = anthropic.Anthropic(api_key=settings.anthropic_api_key, base_url=settings.anthropic_base_url)
    r = c.messages.create(model=settings.ai_model, max_tokens=10, messages=[{"role": "user", "content": "اكتب: متصل"}])
    line("claude_api", "OK", r.model)
except Exception as e:  # noqa: BLE001
    line("claude_api", "FAIL", type(e).__name__, str(e)[:150])

from mandoob.discovery.agent import AgentSource  # noqa: E402
from mandoob.discovery.sources import AISignalsSource, BraveSearchSource, GooglePlacesSource  # noqa: E402

for name, src, q in [("google_places", GooglePlacesSource(), QUERY), ("brave_web_search", BraveSearchSource(), QUERY),
                     ("ai_agent (Claude web search + fetch)", AgentSource(per_call=5), QUERY),
                     ("signals (news/jobs)", AISignalsSource(), "توزيع")]:
    try:
        cands = list(src.search(q, CITY, {}))
        withc = [x for x in cands if x.contacts]
        line(name, "OK", f"candidates={len(cands)}", f"with_verified_contacts={len(withc)}")
        for x in cands[:3]:
            line("  ", x.name, "|", x.city, "|", x.website or "-", "|",
                 ", ".join(f"{fc.kind}:{fc.raw}@{fc.source_url}" for fc in x.contacts) or "no contacts")
        if hasattr(src, "last") and src.last:
            line("  ", f"pages_read={src.last.pages_read}", f"searches={src.last.searches}",
                 f"rejected_values={len(src.last.rejected_values)}")
    except SourceUnavailable as e:
        line(name, "NOT_CONFIGURED", str(e))
    except SourceError as e:
        line(name, "ERROR", str(e)[:200])
