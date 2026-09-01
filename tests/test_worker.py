from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.downloader import DownloadResult
from app.models import DeliveryRequest, DeliveryState, MediaKind, StoredDelivery
from app.worker import process_job


class FakeQueue:
    def __init__(self) -> None:
        self.saved: list[DeliveryState] = []
        self.retries: list[int] = []

    async def save(self, job: StoredDelivery) -> None:
        self.saved.append(job.state)

    async def schedule_retry(self, job: StoredDelivery, delay_seconds: int) -> None:
        job.state = DeliveryState.RETRY_SCHEDULED
        self.retries.append(delay_seconds)


class FakeTelegram:
    async def send_file(
        self,
        request: DeliveryRequest,
        file_path: Path,
        content_type: str | None,
    ) -> dict[str, Any]:
        assert file_path.read_bytes() == b"generated"
        assert content_type == "video/mp4"
        return {
            "message_id": 99,
            # Exercise video-to-document fallback file_id handling.
            "document": {"file_id": "telegram-file-id"},
        }


@pytest.mark.asyncio
async def test_worker_sends_and_removes_temporary_file(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = StoredDelivery(
        id="job-1",
        state=DeliveryState.QUEUED,
        request=DeliveryRequest(
            chat_id=42,
            source_url="https://files.example.test/result.mp4",
            filename="result.mp4",
            media_kind=MediaKind.VIDEO,
        ),
    )
    queue = FakeQueue()

    async def fake_download(
        source_url: str,
        destination: Path,
        configured_settings: Settings,
        *,
        client: httpx.AsyncClient,
    ) -> DownloadResult:
        assert source_url.startswith("https://files.example.test/")
        assert configured_settings is settings
        assert client is not None
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"generated")
        return DownloadResult(size_bytes=9, content_type="video/mp4")

    monkeypatch.setattr("app.worker.download_to_path", fake_download)
    async with httpx.AsyncClient() as download_client:
        await process_job(
            job,
            queue,  # type: ignore[arg-type]
            FakeTelegram(),  # type: ignore[arg-type]
            settings,
            download_client=download_client,
        )

    assert job.state is DeliveryState.SENT
    assert job.telegram_message_id == 99
    assert job.telegram_file_id == "telegram-file-id"
    assert queue.saved == [
        DeliveryState.DOWNLOADING,
        DeliveryState.SENDING,
        DeliveryState.SENT,
    ]
    assert not (settings.delivery_temp_dir / job.id).exists()
