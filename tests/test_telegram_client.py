from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.models import DeliveryRequest, MediaKind
from app.telegram_client import TelegramBotAPIClient


@pytest.mark.asyncio
async def test_local_file_uri_is_sent_without_multipart(
    settings: Settings, tmp_path: Path
) -> None:
    file_path = tmp_path / "clip.mp4"
    file_path.write_bytes(b"video")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = (await request.aread()).decode()
        assert request.url.path.endswith("/sendVideo")
        assert "file%3A%2F%2F" in body
        assert "supports_streaming=true" in body
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 17, "video": {"file_id": "v1"}}},
        )

    request = DeliveryRequest(
        chat_id=42,
        source_url="https://files.example.test/clip.mp4",
        filename="clip.mp4",
        media_kind=MediaKind.VIDEO,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        result = await client.send_file(request, file_path, "video/mp4")

    assert result["message_id"] == 17


@pytest.mark.asyncio
async def test_invalid_video_falls_back_once_to_document(
    settings: Settings, tmp_path: Path
) -> None:
    file_path = tmp_path / "clip.bin"
    file_path.write_bytes(b"not-video")
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "sendVideo":
            return httpx.Response(
                400,
                json={"ok": False, "error_code": 400, "description": "Failed to process"},
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 18, "document": {"file_id": "document-v1"}},
            },
        )

    request = DeliveryRequest(
        chat_id=42,
        source_url="https://files.example.test/clip.bin",
        filename="clip.bin",
        media_kind=MediaKind.VIDEO,
        fallback_to_document=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        result = await client.send_file(request, file_path, "application/octet-stream")

    assert methods == ["sendVideo", "sendDocument"]
    assert result["document"]["file_id"] == "document-v1"
