"""إعدادات المنصة من متغيرات البيئة.

كل الأسرار (مفاتيح API، كلمات مرور) تُقرأ من البيئة فقط ولا تُحفظ في الكود.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

RIYADH = ZoneInfo("Asia/Riyadh")
BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    v = _env(name)
    if not v:
        return default
    return v.lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env(
        "DATABASE_URL", f"sqlite:///{Path(_env('DATA_DIR', str(BASE_DIR / 'data'))) / 'mandoob.db'}"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", "dev-only-change-me"))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(BASE_DIR / "data"))))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8000"))

    # الذكاء الاصطناعي (Claude)
    # MANDOOB_ANTHROPIC_API_KEY له الأولوية حتى لا يتعارض مع إعدادات أدوات أخرى على نفس الجهاز
    anthropic_api_key: str = field(default_factory=lambda: _env("MANDOOB_ANTHROPIC_API_KEY") or _env("ANTHROPIC_API_KEY"))
    anthropic_base_url: str = field(default_factory=lambda: _env("MANDOOB_ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    ai_agent_model: str = field(default_factory=lambda: _env("AI_AGENT_MODEL"))
    ai_fetch_tool: str = field(default_factory=lambda: _env("AI_FETCH_TOOL", "web_fetch_20260209"))
    ai_search_tool: str = field(default_factory=lambda: _env("AI_SEARCH_TOOL", "web_search_20260209"))
    ai_model: str = field(default_factory=lambda: _env("AI_MODEL", "claude-opus-5-5"))
    ai_effort: str = field(default_factory=lambda: _env("AI_EFFORT", "low"))
    # البحث الآلي العميق: مستوى تفكير أعلى وعدد أكبر من عمليات البحث والصفحات لكل عبارة
    ai_agent_effort: str = field(default_factory=lambda: _env("AI_AGENT_EFFORT", "high"))
    ai_agent_searches: int = field(default_factory=lambda: int(_env("AI_AGENT_SEARCHES", "12")))
    ai_agent_fetches: int = field(default_factory=lambda: int(_env("AI_AGENT_FETCHES", "30")))

    # مصادر البحث
    google_places_api_key: str = field(default_factory=lambda: _env("GOOGLE_PLACES_API_KEY"))
    brave_search_api_key: str = field(default_factory=lambda: _env("BRAVE_SEARCH_API_KEY"))
    crawler_user_agent: str = field(default_factory=lambda: _env(
        "CRAWLER_USER_AGENT", "SayaraUpLeadBot/1.0 (+contact page verification)"))

    # البريد
    ms_tenant_id: str = field(default_factory=lambda: _env("MS_TENANT_ID"))
    ms_client_id: str = field(default_factory=lambda: _env("MS_CLIENT_ID"))
    ms_client_secret: str = field(default_factory=lambda: _env("MS_CLIENT_SECRET"))
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    google_refresh_token: str = field(default_factory=lambda: _env("GOOGLE_REFRESH_TOKEN"))
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: str = field(default_factory=lambda: _env("SMTP_PORT", "465"))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))

    # المكالمات الصوتية
    elevenlabs_api_key: str = field(default_factory=lambda: _env("ELEVENLABS_API_KEY"))
    elevenlabs_agent_id: str = field(default_factory=lambda: _env("ELEVENLABS_AGENT_ID"))
    elevenlabs_phone_number_id: str = field(default_factory=lambda: _env("ELEVENLABS_PHONE_NUMBER_ID"))
    voice_webhook_secret: str = field(default_factory=lambda: _env("VOICE_WEBHOOK_SECRET"))

    # مهمة المنصة الحالية: البحث وجمع البيانات فقط — الفريق يتواصل بنفسه.
    # الاتصال الآلي والإرسال التلقائي للعروض والإيميلات التسويقية محفوظان في الكود ومعطّلان افتراضيًا.
    # التفعيل مستقبلًا بقرار الإدارة: ENABLE_VOICE_CALLS=1 و/أو ENABLE_AUTO_OUTREACH=1 في إعدادات الخادم.
    # (كود الدخول للموظفين يُرسل بالبريد دائمًا ولا يتأثر.)
    voice_calls_enabled: bool = field(default_factory=lambda: _bool("ENABLE_VOICE_CALLS", False))
    auto_outreach_enabled: bool = field(default_factory=lambda: _bool("ENABLE_AUTO_OUTREACH", False))

    # تشغيل
    run_scheduler_in_web: bool = field(default_factory=lambda: _bool("RUN_SCHEDULER_IN_WEB", False))
    admin_email: str = field(default_factory=lambda: _env("ADMIN_EMAIL", "admin@sayaraup.local"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", "change-me-now"))

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
