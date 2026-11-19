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
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    ai_model: str = field(default_factory=lambda: _env("AI_MODEL", "claude-opus-5-5"))
    ai_effort: str = field(default_factory=lambda: _env("AI_EFFORT", "low"))

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

    # المكالمات الصوتية
    elevenlabs_api_key: str = field(default_factory=lambda: _env("ELEVENLABS_API_KEY"))
    elevenlabs_agent_id: str = field(default_factory=lambda: _env("ELEVENLABS_AGENT_ID"))
    elevenlabs_phone_number_id: str = field(default_factory=lambda: _env("ELEVENLABS_PHONE_NUMBER_ID"))
    voice_webhook_secret: str = field(default_factory=lambda: _env("VOICE_WEBHOOK_SECRET"))

    # تشغيل
    run_scheduler_in_web: bool = field(default_factory=lambda: _bool("RUN_SCHEDULER_IN_WEB", False))
    admin_email: str = field(default_factory=lambda: _env("ADMIN_EMAIL", "admin@sayaraup.local"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", "change-me-now"))

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
