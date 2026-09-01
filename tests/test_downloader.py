from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.downloader import DownloadError, download_to_path


@pytest.mark.asyncio
async def test_download_streams_to_disk(settings: Settings, tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "files.example.test"
        return httpx.Response(
            200,
            headers={"Content-Type": "video/mp4", "Content-Length": "6"},
            content=b"video!",
        )

    destination = tmp_path / "result.mp4"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await download_to_path(
            "https://files.example.test/result.mp4",
            destination,
            settings,
            client=client,
        )

    assert destination.read_bytes() == b"video!"
    assert result.size_bytes == 6
    assert result.content_type == "video/mp4"


@pytest.mark.asyncio
async def test_download_revalidates_redirect_host(
    settings: Settings, tmp_path: Path
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.test/private"})

    destination = tmp_path / "result.mp4"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="host is not allowed"):
            await download_to_path(
                "https://files.example.test/result.mp4",
                destination,
                settings,
                client=client,
            )
    assert not destination.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("content_length", ["1025", "not-a-number", "-1"])
async def test_download_rejects_invalid_or_large_content_length(
    settings: Settings,
    tmp_path: Path,
    content_length: str,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": content_length},
            content=b"body",
        )

    destination = tmp_path / "result.bin"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DownloadError):
            await download_to_path(
                "https://files.example.test/result.bin",
                destination,
                settings,
                client=client,
            )
    assert not destination.exists()
