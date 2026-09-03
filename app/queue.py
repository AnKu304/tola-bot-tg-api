from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import WatchError

from app.config import Settings
from app.models import DeliveryRequest, DeliveryState, StoredDelivery


class IdempotencyConflictError(Exception):
    """The same idempotency key was already used for another request."""


class DeliveryQueue:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    def _job_key(self, job_id: str) -> str:
        return f"{self.settings.redis_key_prefix}:job:{job_id}"

    def _idempotency_key(self, key: str) -> str:
        return f"{self.settings.redis_key_prefix}:idempotency:{key}"

    @property
    def heartbeat_key(self) -> str:
        return f"{self.settings.redis_key_prefix}:worker-heartbeat"

    async def create(
        self, request: DeliveryRequest, idempotency_key: str | None
    ) -> tuple[StoredDelivery, bool]:
        if not idempotency_key:
            job = StoredDelivery(id=str(uuid.uuid4()), state=DeliveryState.QUEUED, request=request)
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.set(
                    self._job_key(job.id),
                    job.model_dump_json(),
                    ex=self.settings.delivery_job_ttl_seconds,
                )
                pipe.rpush(self.settings.redis_queue_name, job.id)
                await pipe.execute()
            return job, True

        redis_idempotency_key = self._idempotency_key(idempotency_key)
        while True:
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(redis_idempotency_key)
                    existing_id = await pipe.get(redis_idempotency_key)
                    if existing_id:
                        decoded_id = (
                            existing_id.decode()
                            if isinstance(existing_id, bytes)
                            else str(existing_id)
                        )
                        job = await self.get(decoded_id)
                        if job is not None:
                            await pipe.reset()
                            if job.request != request:
                                raise IdempotencyConflictError
                            return job, False
                    job = StoredDelivery(
                        id=str(uuid.uuid4()), state=DeliveryState.QUEUED, request=request
                    )
                    pipe.multi()
                    pipe.set(
                        redis_idempotency_key,
                        job.id,
                        ex=self.settings.delivery_job_ttl_seconds,
                    )
                    pipe.set(
                        self._job_key(job.id),
                        job.model_dump_json(),
                        ex=self.settings.delivery_job_ttl_seconds,
                    )
                    pipe.rpush(self.settings.redis_queue_name, job.id)
                    await pipe.execute()
                    return job, True
                except WatchError:
                    continue

    async def get(self, job_id: str) -> StoredDelivery | None:
        raw = await self.redis.get(self._job_key(job_id))
        return StoredDelivery.model_validate_json(raw) if raw else None

    async def save(self, job: StoredDelivery) -> None:
        job.updated_at = datetime.now(UTC)
        await self.redis.set(
            self._job_key(job.id),
            job.model_dump_json(),
            ex=self.settings.delivery_job_ttl_seconds,
        )

    async def dequeue(self, timeout_seconds: int = 5) -> str | None:
        item = await self.redis.blmove(
            self.settings.redis_queue_name,
            self.settings.redis_processing_queue_name,
            timeout_seconds,
            src="LEFT",
            dest="RIGHT",
        )
        if item is None:
            return None
        return item.decode() if isinstance(item, bytes) else str(item)

    async def acknowledge(self, job_id: str) -> None:
        await self.redis.lrem(self.settings.redis_processing_queue_name, 1, job_id)

    async def recover_processing(self) -> int:
        """Return jobs left unacknowledged by a previous worker process."""
        recovered = 0
        while True:
            job_id = await self.redis.lmove(
                self.settings.redis_processing_queue_name,
                self.settings.redis_queue_name,
                src="LEFT",
                dest="LEFT",
            )
            if job_id is None:
                return recovered
            recovered += 1

    async def schedule_retry(self, job: StoredDelivery, delay_seconds: int) -> None:
        job.state = DeliveryState.RETRY_SCHEDULED
        job.next_attempt_at = datetime.fromtimestamp(time.time() + delay_seconds, tz=UTC)
        job.updated_at = datetime.now(UTC)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(
                self._job_key(job.id),
                job.model_dump_json(),
                ex=self.settings.delivery_job_ttl_seconds,
            )
            pipe.zadd(
                self.settings.redis_retry_queue_name,
                {job.id: job.next_attempt_at.timestamp()},
            )
            # Moving to delayed retry and releasing the processing claim must
            # be one transaction. Otherwise a crash can recover the claim to
            # pending while the same job is also present in the retry set.
            pipe.lrem(self.settings.redis_processing_queue_name, 1, job.id)
            await pipe.execute()

    async def promote_due_retries(self, limit: int = 100) -> int:
        script = """
        local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
        local moved = 0
        for _, id in ipairs(ids) do
          if redis.call('ZREM', KEYS[1], id) == 1 then
            redis.call('RPUSH', KEYS[2], id)
            moved = moved + 1
          end
        end
        return moved
        """
        return int(
            await self.redis.eval(
                script,
                2,
                self.settings.redis_retry_queue_name,
                self.settings.redis_queue_name,
                time.time(),
                limit,
            )
        )

    async def heartbeat(self) -> None:
        await self.redis.set(
            self.heartbeat_key,
            datetime.now(UTC).isoformat(),
            ex=self.settings.delivery_worker_heartbeat_ttl_seconds,
        )

    async def worker_is_alive(self) -> bool:
        return bool(await self.redis.exists(self.heartbeat_key))
