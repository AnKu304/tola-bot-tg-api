from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_api_token: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_bot_api_url: str = "http://telegram-bot-api:8081"
    telegram_use_local_file_uri: bool = True

    redis_url: str = "redis://redis:6379/0"
    redis_queue_name: str = "tola:telegram:deliveries"
    redis_processing_queue_name: str = "tola:telegram:delivery-processing"
    redis_retry_queue_name: str = "tola:telegram:delivery-retries"
    redis_key_prefix: str = "tola:telegram"

    delivery_temp_dir: Path = Path("/var/lib/tola-bot/files")
    delivery_max_file_bytes: int = 2_000_000_000
    delivery_job_ttl_seconds: int = 7 * 24 * 60 * 60
    delivery_max_attempts: int = 4
    delivery_worker_concurrency: int = 2
    delivery_retry_base_seconds: int = 5
    delivery_worker_heartbeat_ttl_seconds: int = 30

    source_allowed_hosts: str = ""
    source_allow_http: bool = False
    source_max_redirects: int = 5
    source_connect_timeout_seconds: float = 15.0
    source_read_timeout_seconds: float = 180.0

    telegram_connect_timeout_seconds: float = 15.0
    telegram_send_timeout_seconds: float = 2 * 60 * 60

    log_level: str = "INFO"

    @property
    def api_token(self) -> str:
        return self.app_api_token.get_secret_value()

    @property
    def bot_token(self) -> str:
        return self.telegram_bot_token.get_secret_value()

    @property
    def allowed_source_host_patterns(self) -> tuple[str, ...]:
        return tuple(
            item.strip().lower().rstrip(".")
            for item in self.source_allowed_hosts.split(",")
            if item.strip()
        )

    def runtime_errors(self) -> list[str]:
        errors: list[str] = []
        if len(self.api_token) < 24:
            errors.append("APP_API_TOKEN must contain at least 24 characters")
        if not self.bot_token:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        if not self.allowed_source_host_patterns:
            errors.append("SOURCE_ALLOWED_HOSTS must contain at least one trusted host")
        if self.delivery_max_file_bytes <= 0 or self.delivery_max_file_bytes > 2_000_000_000:
            errors.append("DELIVERY_MAX_FILE_BYTES must be between 1 and 2000000000")
        if self.delivery_worker_concurrency < 1:
            errors.append("DELIVERY_WORKER_CONCURRENCY must be at least 1")
        return errors


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
