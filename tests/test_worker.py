from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.downloader import DownloadResult
from app.models import DeliveryRequest, DeliveryState, MediaKind, StoredDelivery
from app.telegram_client import TelegramAPIError
from app.worker import cleanup_stale_work_dirs, consume, create_worker_redis, process_job


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
async def test_worker_redis_timeout_exceeds_blocking_queue_wait(
    settings: Settings,
) -> None:
    redis = create_worker_redis(settings)
    try:
        connection_settings = redis.connection_pool.connection_kwargs
        assert connection_settings["socket_timeout"] == 15.0
        assert connection_settings["socket_timeout"] > 5
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_worker_sends_and_removes_temporary_file(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
    caplog.set_level(logging.INFO, logger="app.worker")
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
    assert "stage=downloading" in caplog.text
    assert "stage=sending" in caplog.text
    assert "stage=sent" in caplog.text
    assert "error_class=none" in caplog.text
    assert f"release={settings.app_release}" in caplog.text
    assert "correlation_id=job-1" in caplog.text
    assert "chat_id" not in caplog.text
    assert "signature=" not in caplog.text


@pytest.mark.asyncio
async def test_worker_rejects_downloaded_size_mismatch_before_send(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = StoredDelivery(
        id="job-size-mismatch",
        state=DeliveryState.QUEUED,
        request=DeliveryRequest(
            chat_id=42,
            source_url="https://files.example.test/result.mp4",
            filename="result.mp4",
            expected_size_bytes=10,
        ),
    )
    queue = FakeQueue()

    async def fake_download(*args: Any, **kwargs: Any) -> DownloadResult:
        destination = args[1]
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"short")
        return DownloadResult(size_bytes=5, content_type="video/mp4")

    class TelegramMustNotRun:
        async def send_file(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("mismatched source must not be sent")

    monkeypatch.setattr("app.worker.download_to_path", fake_download)
    async with httpx.AsyncClient() as download_client:
        await process_job(
            job,
            queue,  # type: ignore[arg-type]
            TelegramMustNotRun(),  # type: ignore[arg-type]
            settings,
            download_client=download_client,
        )

    assert job.state is DeliveryState.FAILED
    assert job.error_code == "source_size_mismatch"
    assert job.downloaded_size_bytes == 5
    assert queue.retries == []


@pytest.mark.asyncio
async def test_worker_respects_telegram_retry_after(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = StoredDelivery(
        id="job-retry",
        state=DeliveryState.QUEUED,
        request=DeliveryRequest(
            chat_id=42,
            source_url="https://files.example.test/result.mp4",
            filename="result.mp4",
        ),
    )
    queue = FakeQueue()

    async def fake_download(*args: Any, **kwargs: Any) -> DownloadResult:
        destination = args[1]
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"generated")
        return DownloadResult(size_bytes=9, content_type="video/mp4")

    class RateLimitedTelegram:
        async def send_file(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise TelegramAPIError(
                "retry later",
                code="telegram_429",
                retryable=True,
                retry_after=17,
            )

    monkeypatch.setattr("app.worker.download_to_path", fake_download)
    async with httpx.AsyncClient() as download_client:
        await process_job(
            job,
            queue,  # type: ignore[arg-type]
            RateLimitedTelegram(),  # type: ignore[arg-type]
            settings,
            download_client=download_client,
        )

    assert job.state is DeliveryState.RETRY_SCHEDULED
    assert job.error_code == "telegram_429"
    assert queue.retries == [17]
    assert not (settings.delivery_temp_dir / job.id).exists()


@pytest.mark.asyncio
async def test_consumer_acknowledges_missing_or_terminal_jobs(settings: Settings) -> None:
    class QueueWithStaleItems:
        def __init__(self) -> None:
            self.items = ["missing", "sent"]
            self.acknowledged: list[str] = []

        async def promote_due_retries(self) -> int:
            return 0

        async def dequeue(self) -> str:
            if self.items:
                return self.items.pop(0)
            raise asyncio.CancelledError

        async def get(self, job_id: str) -> StoredDelivery | None:
            if job_id == "missing":
                return None
            return StoredDelivery(
                id=job_id,
                state=DeliveryState.SENT,
                request=DeliveryRequest(
                    chat_id=42,
                    source_url="https://files.example.test/result.mp4",
                    filename="result.mp4",
                ),
            )

        async def acknowledge(self, job_id: str) -> None:
            self.acknowledged.append(job_id)

    queue = QueueWithStaleItems()
    async with httpx.AsyncClient() as download_client:
        with pytest.raises(asyncio.CancelledError):
            await consume(
                1,
                queue,  # type: ignore[arg-type]
                FakeTelegram(),  # type: ignore[arg-type]
                settings,
                download_client,
            )
    assert queue.acknowledged == ["missing", "sent"]


def test_startup_cleanup_removes_only_delivery_work_directories(tmp_path: Path) -> None:
    orphan = tmp_path / str(uuid.uuid4())
    orphan.mkdir()
    (orphan / "result.mp4").write_bytes(b"partial")
    unrelated = tmp_path / "keep-me"
    unrelated.mkdir()

    assert cleanup_stale_work_dirs(tmp_path) == (1, 0)
    assert not orphan.exists()
    assert unrelated.exists()


def test_startup_cleanup_reports_failure_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orphan = tmp_path / str(uuid.uuid4())
    orphan.mkdir()
    monkeypatch.setattr("app.worker.shutil.rmtree", lambda _path: (_ for _ in ()).throw(OSError()))

    assert cleanup_stale_work_dirs(tmp_path) == (0, 1)
    assert orphan.exists()


@pytest.mark.asyncio
async def test_worker_applies_total_download_timeout(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.source_total_timeout_seconds = 0.01
    job = StoredDelivery(
        id="job-timeout",
        state=DeliveryState.QUEUED,
        request=DeliveryRequest(
            chat_id=42,
            source_url="https://files.example.test/result.mp4",
            filename="result.mp4",
        ),
    )
    queue = FakeQueue()

    async def slow_download(*args: Any, **kwargs: Any) -> DownloadResult:
        await asyncio.sleep(1)
        raise AssertionError("the overall timeout must cancel the download")

    monkeypatch.setattr("app.worker.download_to_path", slow_download)
    async with httpx.AsyncClient() as download_client:
        await process_job(
            job,
            queue,  # type: ignore[arg-type]
            FakeTelegram(),  # type: ignore[arg-type]
            settings,
            download_client=download_client,
        )

    assert job.state is DeliveryState.RETRY_SCHEDULED
    assert job.error_code == "download_timeout"
    assert queue.retries == [settings.delivery_retry_base_seconds]


@pytest.mark.asyncio
async def test_worker_stops_retrying_at_max_attempts(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = StoredDelivery(
        id="job-last-attempt",
        state=DeliveryState.QUEUED,
        attempts=settings.delivery_max_attempts - 1,
        request=DeliveryRequest(
            chat_id=42,
            source_url="https://files.example.test/result.mp4",
            filename="result.mp4",
        ),
    )
    queue = FakeQueue()

    async def fake_download(*args: Any, **kwargs: Any) -> DownloadResult:
        destination = args[1]
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"generated")
        return DownloadResult(size_bytes=9, content_type="video/mp4")

    class UnavailableTelegram:
        async def send_file(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise TelegramAPIError(
                "temporarily unavailable",
                code="telegram_unavailable",
                retryable=True,
            )

    monkeypatch.setattr("app.worker.download_to_path", fake_download)
    async with httpx.AsyncClient() as download_client:
        await process_job(
            job,
            queue,  # type: ignore[arg-type]
            UnavailableTelegram(),  # type: ignore[arg-type]
            settings,
            download_client=download_client,
        )

    assert job.state is DeliveryState.FAILED
    assert job.attempts == settings.delivery_max_attempts
    assert queue.retries == []
