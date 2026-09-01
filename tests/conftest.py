from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_api_token="a" * 32,
        telegram_bot_token="123456:test-token",
        telegram_bot_api_url="http://telegram.test:8081",
        redis_url="redis://redis.test:6379/0",
        delivery_temp_dir=tmp_path,
        delivery_max_file_bytes=1024,
        source_allowed_hosts="files.example.test,*.objects.example.test",
    )
