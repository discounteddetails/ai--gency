"""Environment configuration (secrets and deployment wiring). Business rules live in the Sheet's Settings tab."""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Claude
    ANTHROPIC_API_KEY: SecretStr
    ANTHROPIC_MODEL: str = "claude-opus-5-5"
    DAILY_TOKEN_BUDGET: int = 3_000_000

    # Composio (Instagram + Messenger)
    COMPOSIO_API_KEY: SecretStr
    COMPOSIO_USER_ID: str | None = None
    COMPOSIO_IG_ACCOUNT_ID: str | None = None
    COMPOSIO_FB_ACCOUNT_ID: str | None = None
    FB_PAGE_ID: str
    IG_USER_ID: str
    POLL_SECONDS: int = 60
    POLL_SECONDS_NIGHT: int = 120

    # Google
    GOOGLE_SERVICE_ACCOUNT_JSON: SecretStr
    GOOGLE_SHEET_ID: str
    GOOGLE_CALENDAR_ID: str
    GOOGLE_MAPS_API_KEY: SecretStr

    # Telegram (owner channel)
    TELEGRAM_BOT_TOKEN: SecretStr
    TELEGRAM_OWNER_CHAT_ID: int
    TELEGRAM_WEBHOOK_SECRET: SecretStr

    # Runtime
    PUBLIC_BASE_URL: str
    SLOT_SIGNING_KEY: SecretStr
    DB_PATH: str = "/data/agent.db"
    DEBOUNCE_SECONDS: float = 20
    OWNER_TAKEOVER_HOURS: int = 12
    SHADOW_MODE_DEFAULT: bool = True
    HEALTHCHECK_URL: str | None = None
    LOG_LEVEL: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
