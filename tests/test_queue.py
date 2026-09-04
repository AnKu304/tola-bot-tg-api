from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis

from app.config import Settings
from app.models import DeliveryRequest
from app.queue import DeliveryQueue, IdempotencyConflictError


async def test_claim_ack_and_idempotency(settings: Settings) -> None:
    redis = FakeRedis()
    queue = DeliveryQueue(redis, settings)  # type: ignore[arg-type]
    request = DeliveryRequest(
        chat_id=42,
        source_url="https://files.example.test/result.mp4",
        filename="result.mp4",
    )

    first, first_created = await queue.create(request, "generation-1")
    second, second_created = await queue.create(request, "generation-1")
    claimed_id = await queue.dequeue(timeout_seconds=1)

    assert first_created is True
    assert second_created is False
    assert second.id == first.id == claimed_id
    assert await redis.llen(settings.redis_queue_name) == 0
    assert await redis.lrange(settings.redis_processing_queue_name, 0, -1) == [
        first.id.encode()
    ]

    await queue.acknowledge(first.id)
    assert await redis.llen(settings.redis_processing_queue_name) == 0
    await redis.aclose()


async def test_recover_processing_returns_unacknowledged_jobs(settings: Settings) -> None:
    class AtomicRecoveryRedis(FakeRedis):
        lmove_calls = 0

        async def lmove(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            self.lmove_calls += 1
            return await super().lmove(*args, **kwargs)

        async def lpop(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise AssertionError("recovery must not split removal and enqueue")

    redis = AtomicRecoveryRedis()
    queue = DeliveryQueue(redis, settings)  # type: ignore[arg-type]
    await redis.rpush(settings.redis_processing_queue_name, "job-1", "job-2")

    assert await queue.recover_processing() == 2
    assert set(await redis.lrange(settings.redis_queue_name, 0, -1)) == {
        b"job-1",
        b"job-2",
    }
    assert await redis.llen(settings.redis_processing_queue_name) == 0
    assert redis.lmove_calls == 3
    await redis.aclose()


async def test_idempotency_key_rejects_a_different_payload(settings: Settings) -> None:
    redis = FakeRedis()
    queue = DeliveryQueue(redis, settings)  # type: ignore[arg-type]
    first_request = DeliveryRequest(
        chat_id=42,
        source_url="https://files.example.test/result.mp4",
        filename="result.mp4",
    )
    conflicting_request = first_request.model_copy(update={"chat_id": 43})

    first, _ = await queue.create(first_request, "generation-1")
    with pytest.raises(IdempotencyConflictError):
        await queue.create(conflicting_request, "generation-1")

    assert await redis.llen(settings.redis_queue_name) == 1
    assert (await queue.get(first.id)).request == first_request  # type: ignore[union-attr]
    await redis.aclose()


async def test_schedule_retry_atomically_releases_processing_claim(
    settings: Settings,
) -> None:
    redis = FakeRedis()
    queue = DeliveryQueue(redis, settings)  # type: ignore[arg-type]
    request = DeliveryRequest(
        chat_id=42,
        source_url="https://files.example.test/result.mp4",
        filename="result.mp4",
    )
    job, _ = await queue.create(request, "generation-retry")
    assert await queue.dequeue(timeout_seconds=1) == job.id

    await queue.schedule_retry(job, 10)

    assert await redis.llen(settings.redis_processing_queue_name) == 0
    assert await redis.zscore(settings.redis_retry_queue_name, job.id) is not None
    assert await redis.zcard(settings.redis_retry_queue_name) == 1
    assert await queue.recover_processing() == 0
    await redis.aclose()
