from __future__ import annotations

import logging

from app.logging_config import configure_logging


def test_http_client_loggers_cannot_leak_bot_token_at_info() -> None:
    configure_logging("INFO")
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
