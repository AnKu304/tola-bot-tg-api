from __future__ import annotations

from fakeredis.aioredis import FakeRedis

from app.config import Settings
from app.models import DeliveryRequest
from app.queue import DeliveryQueue


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
    redis = FakeRedis()
    queue = DeliveryQueue(redis, settings)  # type: ignore[arg-type]
    await redis.rpush(settings.redis_processing_queue_name, "job-1", "job-2")

    assert await queue.recover_processing() == 2
    assert set(await redis.lrange(settings.redis_queue_name, 0, -1)) == {
        b"job-1",
        b"job-2",
    }
    assert await redis.llen(settings.redis_processing_queue_name) == 0
    await redis.aclose()
