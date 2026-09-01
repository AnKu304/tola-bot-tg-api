from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from app.config import get_settings
from app.telegram_client import TelegramBotAPIClient


async def logout_cloud(confirm: bool) -> int:
    if not confirm:
        print("Refusing to log out the cloud Bot API without --confirm", file=sys.stderr)
        return 2
    settings = get_settings()
    if not settings.bot_token:
        print("TELEGRAM_BOT_TOKEN is not configured", file=sys.stderr)
        return 2
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{settings.bot_token}/logOut"
        )
    body = response.json()
    if response.status_code >= 400 or body.get("ok") is not True:
        print(f"Cloud Bot API logOut failed: {body.get('description', response.status_code)}")
        return 1
    print("Cloud Bot API logOut succeeded. Route every bot request to the local server now.")
    return 0


async def check_local() -> int:
    settings = get_settings()
    client = TelegramBotAPIClient(settings)
    try:
        bot = await client.get_me()
        print(f"Local Bot API is ready for @{bot.get('username', 'unknown')}")
        return 0
    except Exception as exc:
        print(f"Local Bot API check failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Tola Telegram delivery operations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    logout_parser = subparsers.add_parser("logout-cloud")
    logout_parser.add_argument("--confirm", action="store_true")
    subparsers.add_parser("check-local")
    args = parser.parse_args()
    if args.command == "logout-cloud":
        code = asyncio.run(logout_cloud(args.confirm))
    else:
        code = asyncio.run(check_local())
    raise SystemExit(code)


if __name__ == "__main__":
    main()
