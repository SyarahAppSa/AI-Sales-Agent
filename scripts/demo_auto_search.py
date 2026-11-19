"""بيانات عرض لشاشتي «البحث الآلي» و«عملاء اليوم» (بدون مفتاح): يحاكي ردود وكيل Claude بصفحات تجريبية .example.
للاختبار الحقيقي استخدم scripts/live_auto_search.py."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mandoob import ai  # noqa: E402
from mandoob.config import settings  # noqa: E402
import test_agent as T  # noqa: E402

for out, _ in T.BATCHES:
    for p in out["prospects"]:
        p["name"] += " (تجريبية)"
ai.ai_agent = T.fake_agent
settings.anthropic_api_key = "demo"

from mandoob.db import init_db, session_scope  # noqa: E402
from mandoob.discovery.agent import AgentSource  # noqa: E402
from mandoob.discovery.runner import run_campaign_discovery  # noqa: E402
from mandoob.models import Campaign, Category  # noqa: E402

init_db()
with session_scope() as s:
    camp = Campaign(name="البحث الآلي — الرياض", status="active", daily_target=400, send_enabled=False,
                    request_text="ابحث لي يوميًا في الرياض عن المنشآت وأصحاب الأنشطة الذين يستخدمون سيارات في عملهم.")
    s.add(camp)
    s.flush()
    s.add(Category(campaign_id=camp.id, sector_key="vehicle_users", label="أي نشاط يستخدم مركبات", cities=["الرياض"],
                   keywords_ar=T.__dict__.get("Q", []) or ["صيانة متنقلة", "توصيل وتركيب", "توزيع مبرد", "نقل موظفين",
                                                          "تأجير معدات مع النقل", "سطحة لنقل السيارات",
                                                          "مطلوب مشرف أسطول {city}"],
                   keywords_en=["mobile maintenance service"], quota=400))
    cid = camp.id
for _ in range(2):
    run_campaign_discovery(cid, sources={"ai_agent": AgentSource()}, crawler=T.NoCrawl(), max_new=2)
print("ok", cid)
