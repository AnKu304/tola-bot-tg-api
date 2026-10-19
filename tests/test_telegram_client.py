from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.models import DeliveryRequest, MediaKind
from app.telegram_client import TelegramAPIError, TelegramBotAPIClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "expected_method"),
    [(10 * 1024 * 1024 - 1, "sendPhoto"), (10 * 1024 * 1024, "sendPhoto"),
     (10 * 1024 * 1024 + 1, "sendDocument"), (11_863_964, "sendDocument")],
)
async def test_photo_size_routes_original_without_reencoding(
    settings: Settings, tmp_path: Path, size: int, expected_method: str
) -> None:
    file_path = tmp_path / "original.png"
    with file_path.open("wb") as source:
        source.write(b"synthetic-original")
        source.truncate(size)
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", 1)[-1])
        body = (await request.aread()).decode()
        assert "file%3A%2F%2F" in body
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 17}})

    request = DeliveryRequest(chat_id=42, source_url="https://files.example.test/original.png",
                              filename="original.png", media_kind=MediaKind.PHOTO)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        await client.send_file(request, file_path, "image/png")
    assert methods == [expected_method]
    assert file_path.stat().st_size == size
    with file_path.open("rb") as source:
        assert source.read(18) == b"synthetic-original"


@pytest.mark.asyncio
@pytest.mark.parametrize("description", [
    "Bad Request: file of size 11863964 bytes is too big for a photo; "
    "the maximum size is 10485760 bytes",
    "Bad Request: PHOTO_INVALID_DIMENSIONS",
])
async def test_known_photo_rejection_falls_back_once(
    settings: Settings, tmp_path: Path, description: str
) -> None:
    file_path = tmp_path / "original.png"
    file_path.write_bytes(b"synthetic-original")
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "sendPhoto":
            return httpx.Response(400, json={"ok": False, "error_code": 400,
                                            "description": description})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 17}})

    request = DeliveryRequest(chat_id=42, source_url="https://files.example.test/original.png",
                              filename="original.png", media_kind=MediaKind.PHOTO)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        await client.send_file(request, file_path, "image/png")
    assert methods == ["sendPhoto", "sendDocument"]
    assert file_path.read_bytes() == b"synthetic-original"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1, 10 * 1024 * 1024 + 1])
async def test_photo_unknown_transport_adds_no_fallback_within_same_call(
    settings: Settings, tmp_path: Path, size: int
) -> None:
    file_path = tmp_path / "original.png"
    with file_path.open("wb") as source:
        source.truncate(size)
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", 1)[-1])
        raise httpx.ReadTimeout("synthetic uncertain response", request=request)

    request = DeliveryRequest(chat_id=42, source_url="https://files.example.test/original.png",
                              filename="original.png", media_kind=MediaKind.PHOTO)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        with pytest.raises(TelegramAPIError) as exc_info:
            await client.send_file(request, file_path, "image/png")
    assert exc_info.value.retryable is True
    assert methods == ["sendPhoto" if size <= 10 * 1024 * 1024 else "sendDocument"]


@pytest.mark.asyncio
async def test_oversize_photo_document_rejection_does_not_repeat_document(
    settings: Settings, tmp_path: Path
) -> None:
    file_path = tmp_path / "original.png"
    with file_path.open("wb") as source:
        source.truncate(10 * 1024 * 1024 + 1)
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(400, json={"ok": False, "error_code": 400,
                                        "description": "PHOTO_INVALID_DIMENSIONS"})

    request = DeliveryRequest(chat_id=42, source_url="https://files.example.test/original.png",
                              filename="original.png", media_kind=MediaKind.PHOTO)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        with pytest.raises(TelegramAPIError):
            await client.send_file(request, file_path, "image/png")
    assert methods == ["sendDocument"]


@pytest.mark.asyncio
async def test_photo_explicit_media_only_request_keeps_no_document_policy(
    settings: Settings, tmp_path: Path
) -> None:
    file_path = tmp_path / "original.png"
    with file_path.open("wb") as source:
        source.truncate(10 * 1024 * 1024 + 1)
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(400, json={"ok": False, "error_code": 400,
                                        "description": "PHOTO_INVALID_DIMENSIONS"})

    request = DeliveryRequest(chat_id=42, source_url="https://files.example.test/original.png",
                              filename="original.png", media_kind=MediaKind.PHOTO,
                              fallback_to_document=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        with pytest.raises(TelegramAPIError):
            await client.send_file(request, file_path, "image/png")
    assert methods == ["sendPhoto"]


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
        assert "width=816" in body
        assert "height=1104" in body
        assert "duration=15" in body
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 17, "video": {"file_id": "v1"}}},
        )

    request = DeliveryRequest(
        chat_id=42,
        source_url="https://files.example.test/clip.mp4",
        filename="clip.mp4",
        media_kind=MediaKind.VIDEO,
        width=816, height=1104, duration=15,
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
        body = (await request.aread()).decode()
        if method == "sendVideo":
            assert "width=816" in body and "height=1104" in body
            return httpx.Response(
                400,
                json={"ok": False, "error_code": 400, "description": "Failed to process"},
            )
        assert "width=" not in body and "height=" not in body and "duration=" not in body
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
        width=816, height=1104, duration=15,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        result = await client.send_file(request, file_path, "application/octet-stream")

    assert methods == ["sendVideo", "sendDocument"]
    assert result["document"]["file_id"] == "document-v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
async def test_transient_http_errors_are_retryable(
    settings: Settings, status_code: int
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=b"not-json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        with pytest.raises(TelegramAPIError) as exc_info:
            await client.get_me()

    assert exc_info.value.retryable is True
    assert exc_info.value.code == "telegram_invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], "unexpected", 42])
async def test_non_object_json_is_a_stable_invalid_response(
    settings: Settings, body: object
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        with pytest.raises(TelegramAPIError) as exc_info:
            await client.get_me()

    assert exc_info.value.retryable is False
    assert exc_info.value.code == "telegram_invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", [408, 425, 429, 500, 503])
async def test_transient_json_errors_are_retryable(
    settings: Settings, error_code: int
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            error_code,
            json={
                "ok": False,
                "error_code": error_code,
                "description": "temporary failure",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        with pytest.raises(TelegramAPIError) as exc_info:
            await client.get_me()

    assert exc_info.value.retryable is True
    assert exc_info.value.code == f"telegram_{error_code}"


@pytest.mark.asyncio
async def test_retryable_media_error_never_falls_back_to_document(
    settings: Settings, tmp_path: Path
) -> None:
    file_path = tmp_path / "clip.mp4"
    file_path.write_bytes(b"video")
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            503,
            json={"ok": False, "error_code": 503, "description": "Failed to process"},
        )

    request = DeliveryRequest(
        chat_id=42,
        source_url="https://files.example.test/clip.mp4",
        filename="clip.mp4",
        media_kind=MediaKind.VIDEO,
        fallback_to_document=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        with pytest.raises(TelegramAPIError) as exc_info:
            await client.send_file(request, file_path, "video/mp4")

    assert exc_info.value.retryable is True
    assert methods == ["sendVideo"]


@pytest.mark.asyncio
async def test_non_format_client_error_never_falls_back_to_document(
    settings: Settings, tmp_path: Path
) -> None:
    file_path = tmp_path / "clip.mp4"
    file_path.write_bytes(b"video")
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "chat not found"},
        )

    request = DeliveryRequest(
        chat_id=42,
        source_url="https://files.example.test/clip.mp4",
        filename="clip.mp4",
        media_kind=MediaKind.VIDEO,
        fallback_to_document=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotAPIClient(settings, client=http_client)
        with pytest.raises(TelegramAPIError):
            await client.send_file(request, file_path, "video/mp4")

    assert methods == ["sendVideo"]
