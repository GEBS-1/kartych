from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    max_bot_token: str = ""
    max_bot_username: str = ""
    max_api_base: str = "https://platform-api2.max.ru"

    public_base_url: str = "http://127.0.0.1:8000"
    webhook_path: str = "/webhook"
    webhook_secret: str = "ChangeMeWebhookSecret1"

    database_url: str = "postgresql+asyncpg://cupcard:cupcard@localhost:5432/cupcard"
    redis_url: str = ""

    watchdog_interval_seconds: int = Field(default=60, ge=15)
    subscribe_on_startup: bool = True
    update_types: str = "bot_started,message_created,message_callback,bot_stopped"

    public_host: str = "your-domain.ru"
    letsencrypt_email: str = "you@example.com"
    max_ssl_verify: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def webhook_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}{self.webhook_path}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def miniapp_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/app"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def update_type_list(self) -> list[str]:
        return [item.strip() for item in self.update_types.split(",") if item.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"prod", "production"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
