from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.downloader import DownloadError, download_to_path
from app.logging_config import configure_logging
from app.models import DeliveryState, StoredDelivery
from app.queue import DeliveryQueue
from app.telegram_client import TelegramAPIError, TelegramBotAPIClient

logger = logging.getLogger(__name__)


def create_worker_redis(settings: Settings) -> Redis:
    """Create a Redis client whose read timeout exceeds the blocking queue wait."""
    return Redis.from_url(
        settings.redis_url,
        socket_timeout=settings.redis_worker_socket_timeout_seconds,
    )


def cleanup_stale_work_dirs(temp_dir: Path) -> tuple[int, int]:
    """Remove orphaned per-job directories left by a hard worker crash."""
    removed = 0
    failed = 0
    if not temp_dir.exists():
        return removed, failed
    for child in temp_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            uuid.UUID(child.name)
        except ValueError:
            continue
        try:
            shutil.rmtree(child)
            removed += 1
        except OSError:
            # A single damaged directory must not create a permanent restart
            # loop. The failure count is logged without exposing its path.
            failed += 1
    return removed, failed


def _telegram_file_id(message: dict[str, Any]) -> str | None:
    if message.get("photo"):
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            value = photos[-1]
            if isinstance(value, dict) and value.get("file_id"):
                return str(value["file_id"])
    for field in ("document", "video", "audio", "animation"):
        value = message.get(field)
        if isinstance(value, dict) and value.get("file_id"):
            return str(value["file_id"])
    return None


async def process_job(
    job: StoredDelivery,
    queue: DeliveryQueue,
    telegram: TelegramBotAPIClient,
    settings: Settings,
    *,
    download_client: httpx.AsyncClient,
) -> None:
    stage = "precheck"
    job.attempts += 1
    job.next_attempt_at = None
    job.error_code = None
    job.error_message = None
    work_dir = settings.delivery_temp_dir / job.id
    file_path = work_dir / job.request.filename
    try:
        if (
            job.request.expected_size_bytes is not None
            and job.request.expected_size_bytes > settings.delivery_max_file_bytes
        ):
            raise DownloadError(
                "expected file size exceeds the configured 2000 MB limit",
                retryable=False,
                code="file_too_large",
            )

        stage = "downloading"
        job.state = DeliveryState.DOWNLOADING
        await queue.save(job)
        logger.info(
            "delivery_event stage=downloading error_class=none release=%s "
            "correlation_id=%s attempt=%s",
            settings.app_release,
            job.id,
            job.attempts,
        )
        try:
            async with asyncio.timeout(settings.source_total_timeout_seconds):
                download = await download_to_path(
                    str(job.request.source_url),
                    file_path,
                    settings,
                    client=download_client,
                )
        except TimeoutError as exc:
            raise DownloadError(
                "source download exceeded the total time limit",
                retryable=True,
                code="download_timeout",
            ) from exc
        job.downloaded_size_bytes = download.size_bytes
        if (
            job.request.expected_size_bytes is not None
            and download.size_bytes != job.request.expected_size_bytes
        ):
            raise DownloadError(
                "downloaded size does not match expected_size_bytes",
                retryable=False,
                code="source_size_mismatch",
            )
        stage = "sending"
        job.state = DeliveryState.SENDING
        await queue.save(job)
        logger.info(
            "delivery_event stage=sending error_class=none release=%s "
            "correlation_id=%s bytes=%s attempt=%s",
            settings.app_release,
            job.id,
            job.downloaded_size_bytes,
            job.attempts,
        )
        message = await telegram.send_file(
            job.request,
            file_path,
            job.request.mime_type or download.content_type,
        )
        stage = "finalize"
        job.state = DeliveryState.SENT
        job.telegram_message_id = (
            int(message["message_id"]) if message.get("message_id") is not None else None
        )
        job.telegram_file_id = _telegram_file_id(message)
        await queue.save(job)
        logger.info(
            "delivery_event stage=sent error_class=none release=%s "
            "correlation_id=%s bytes=%s attempts=%s",
            settings.app_release,
            job.id,
            job.downloaded_size_bytes,
            job.attempts,
        )
    except (DownloadError, TelegramAPIError) as exc:
        job.error_code = exc.code
        job.error_message = str(exc)[:500]
        if exc.retryable and job.attempts < settings.delivery_max_attempts:
            retry_after = getattr(exc, "retry_after", None)
            delay = retry_after or min(
                settings.delivery_retry_base_seconds * (2 ** (job.attempts - 1)), 300
            )
            await queue.schedule_retry(job, delay)
            logger.warning(
                "delivery_event stage=%s state=retry_scheduled error_class=%s "
                "release=%s correlation_id=%s delay=%s attempt=%s",
                stage,
                exc.code,
                settings.app_release,
                job.id,
                delay,
                job.attempts,
            )
        else:
            job.state = DeliveryState.FAILED
            await queue.save(job)
            logger.error(
                "delivery_event stage=%s state=failed error_class=%s release=%s "
                "correlation_id=%s attempts=%s",
                stage,
                exc.code,
                settings.app_release,
                job.id,
                job.attempts,
            )
    except Exception as exc:
        logger.error(
            "delivery_event stage=%s state=error error_class=internal_error release=%s "
            "correlation_id=%s exception_type=%s attempt=%s",
            stage,
            settings.app_release,
            job.id,
            type(exc).__name__,
            job.attempts,
        )
        job.error_code = "internal_error"
        job.error_message = "Unexpected delivery worker error"
        if job.attempts < settings.delivery_max_attempts:
            delay = min(settings.delivery_retry_base_seconds * (2 ** (job.attempts - 1)), 300)
            await queue.schedule_retry(job, delay)
        else:
            job.state = DeliveryState.FAILED
            await queue.save(job)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def consume(
    worker_number: int,
    queue: DeliveryQueue,
    telegram: TelegramBotAPIClient,
    settings: Settings,
    download_client: httpx.AsyncClient,
) -> None:
    while True:
        await queue.promote_due_retries()
        job_id = await queue.dequeue()
        if not job_id:
            continue
        job = await queue.get(job_id)
        if job is None or job.state in {DeliveryState.SENT, DeliveryState.FAILED}:
            await queue.acknowledge(job_id)
            continue
        logger.info(
            "delivery_event stage=claimed error_class=none release=%s "
            "correlation_id=%s worker=%s",
            settings.app_release,
            job_id,
            worker_number,
        )
        await process_job(
            job,
            queue,
            telegram,
            settings,
            download_client=download_client,
        )
        await queue.acknowledge(job_id)


async def heartbeat(queue: DeliveryQueue, settings: Settings) -> None:
    interval = max(2, settings.delivery_worker_heartbeat_ttl_seconds // 3)
    while True:
        await queue.heartbeat()
        await asyncio.sleep(interval)


async def worker_main() -> None:
    settings = get_settings()
    errors = settings.runtime_errors()
    if errors:
        raise RuntimeError("; ".join(errors))
    configure_logging(settings.log_level)
    settings.delivery_temp_dir.mkdir(parents=True, exist_ok=True)
    redis = create_worker_redis(settings)
    queue = DeliveryQueue(redis, settings)
    telegram = TelegramBotAPIClient(settings)
    download_client = httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(
            connect=settings.source_connect_timeout_seconds,
            read=settings.source_read_timeout_seconds,
            write=30.0,
            pool=settings.source_connect_timeout_seconds,
        ),
    )
    try:
        await redis.ping()
        stale_dirs, cleanup_failures = await asyncio.to_thread(
            cleanup_stale_work_dirs, settings.delivery_temp_dir
        )
        if stale_dirs:
            logger.warning(
                "delivery_event stage=startup_cleanup error_class=orphaned_temp_files "
                "release=%s correlation_id=startup removed=%s",
                settings.app_release,
                stale_dirs,
            )
        if cleanup_failures:
            logger.error(
                "delivery_event stage=startup_cleanup error_class=cleanup_failed "
                "release=%s correlation_id=startup failed=%s",
                settings.app_release,
                cleanup_failures,
            )
        await telegram.get_me()
        recovered = await queue.recover_processing()
        if recovered:
            logger.warning(
                "delivery_event stage=startup_recovery error_class=unacknowledged_jobs "
                "release=%s correlation_id=startup recovered=%s",
                settings.app_release,
                recovered,
            )
        await asyncio.gather(
            heartbeat(queue, settings),
            *(
                consume(index + 1, queue, telegram, settings, download_client)
                for index in range(settings.delivery_worker_concurrency)
            ),
        )
    finally:
        await download_client.aclose()
        await telegram.close()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(worker_main())
