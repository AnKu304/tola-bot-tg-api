from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.models import DeliveryRequest, MediaKind


class TelegramAPIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "telegram_error",
        retryable: bool,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class TelegramBotAPIClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.telegram_connect_timeout_seconds,
                read=settings.telegram_send_timeout_seconds,
                write=settings.telegram_send_timeout_seconds,
                pool=settings.telegram_connect_timeout_seconds,
            )
        )

    def _method_url(self, method: str) -> str:
        root = self.settings.telegram_bot_api_url.rstrip("/")
        return f"{root}/bot{self.settings.bot_token}/{method}"

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def get_me(self) -> dict[str, Any]:
        response = await self.client.post(self._method_url("getMe"))
        return self._parse_response(response)

    async def send_file(
        self,
        request: DeliveryRequest,
        file_path: Path,
        content_type: str | None,
    ) -> dict[str, Any]:
        try:
            return await self._send_as(request.media_kind, request, file_path, content_type)
        except TelegramAPIError as exc:
            if (
                request.fallback_to_document
                and request.media_kind is not MediaKind.DOCUMENT
                and not exc.retryable
                and self._is_media_format_error(str(exc))
            ):
                return await self._send_as(
                    MediaKind.DOCUMENT, request, file_path, content_type
                )
            raise

    async def _send_as(
        self,
        media_kind: MediaKind,
        request: DeliveryRequest,
        file_path: Path,
        content_type: str | None,
    ) -> dict[str, Any]:
        method, field = {
            MediaKind.DOCUMENT: ("sendDocument", "document"),
            MediaKind.VIDEO: ("sendVideo", "video"),
            MediaKind.PHOTO: ("sendPhoto", "photo"),
            MediaKind.AUDIO: ("sendAudio", "audio"),
            MediaKind.ANIMATION: ("sendAnimation", "animation"),
        }[media_kind]
        payload: dict[str, str] = {
            "chat_id": str(request.chat_id),
            "disable_notification": str(request.disable_notification).lower(),
            "protect_content": str(request.protect_content).lower(),
        }
        if request.caption:
            payload["caption"] = request.caption
        if request.parse_mode:
            payload["parse_mode"] = request.parse_mode
        if request.reply_markup:
            payload["reply_markup"] = json.dumps(
                request.reply_markup, ensure_ascii=False, separators=(",", ":")
            )
        if media_kind is MediaKind.VIDEO:
            payload["supports_streaming"] = str(request.supports_streaming).lower()

        try:
            if self.settings.telegram_use_local_file_uri:
                # The path is on a local shared volume and has no network I/O.
                payload[field] = file_path.resolve().as_uri()  # noqa: ASYNC240
                response = await self.client.post(self._method_url(method), data=payload)
            else:
                with file_path.open("rb") as upload:
                    response = await self.client.post(
                        self._method_url(method),
                        data=payload,
                        files={field: (request.filename, upload, content_type)},
                    )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TelegramAPIError(
                "local Telegram Bot API is temporarily unavailable",
                retryable=True,
                code="telegram_unavailable",
            ) from exc
        return self._parse_response(response)

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramAPIError(
                f"local Telegram Bot API returned HTTP {response.status_code}",
                retryable=response.status_code >= 500,
                code="telegram_invalid_response",
            ) from exc
        if response.status_code < 400 and body.get("ok") is True:
            result = body.get("result")
            return result if isinstance(result, dict) else {"result": result}

        error_code = int(body.get("error_code") or response.status_code or 500)
        parameters = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
        retry_after = parameters.get("retry_after")
        description = str(body.get("description") or f"Telegram error {error_code}")
        raise TelegramAPIError(
            description[:500],
            retryable=error_code == 429 or error_code >= 500,
            retry_after=int(retry_after) if retry_after is not None else None,
            code=f"telegram_{error_code}",
        )

    @staticmethod
    def _is_media_format_error(message: str) -> bool:
        lowered = message.lower()
        markers = (
            "wrong file identifier/http url specified",
            "failed to get duration",
            "failed to process",
            "wrong type of the web page content",
            "photo_invalid",
            "video_content_type_invalid",
            "file is too big for",
        )
        return any(marker in lowered for marker in markers)
