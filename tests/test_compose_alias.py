"""Check the rendered network contract rather than the YAML anchor text."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_delivery_http_alias_belongs_only_to_api() -> None:
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        env={**os.environ, "ENV_FILE": ".env.example"},
        capture_output=True,
        text=True,
        check=True,
    )
    compose = json.loads(result.stdout)
    services = compose["services"]
    assert compose["networks"]["tola-telegram"]["name"] == "tola-telegram"
    assert services["api"]["networks"]["tola-telegram"]["aliases"] == [
        "tola-telegram-delivery"
    ]
    assert "tola-telegram" in services["worker"]["networks"]
    for name, service in services.items():
        if name == "api":
            continue
        aliases = (service.get("networks", {}).get("tola-telegram") or {}).get("aliases", [])
        assert "tola-telegram-delivery" not in aliases, name
    assert services["telegram-bot-api"]["networks"]["tola-telegram"]["aliases"] == [
        "tola-telegram-bot-api"
    ]
