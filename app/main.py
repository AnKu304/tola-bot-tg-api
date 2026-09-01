from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from redis.asyncio import Redis

from app import __version__
from app.config import Settings, get_settings
from app.logging_config import configure_logging
from app.models import DeliveryRequest, DeliveryStatus
from app.queue import DeliveryQueue
from app.security import require_internal_token, validate_source_url
from app.telegram_client import TelegramBotAPIClient

logger = logging.getLogger(__name__)


def create_app(
    *,
    settings: Settings | None = None,
    queue: DeliveryQueue | None = None,
) -> FastAPI:
    configured_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        errors = configured_settings.runtime_errors()
        if errors:
            raise RuntimeError("; ".join(errors))
        configure_logging(configured_settings.log_level)
        redis: Redis | None = None
        if queue is None:
            redis = Redis.from_url(configured_settings.redis_url)
            app.state.queue = DeliveryQueue(redis, configured_settings)
        else:
            app.state.queue = queue
        app.state.settings = configured_settings
        try:
            yield
        finally:
            if redis is not None:
                await redis.aclose()

    app = FastAPI(
        title="Tola Bot Telegram Delivery API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = configured_settings
    if queue is not None:
        app.state.queue = queue

    def get_queue(request: Request) -> DeliveryQueue:
        return request.app.state.queue

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/health/ready", tags=["health"])
    async def ready(request: Request) -> dict[str, str]:
        delivery_queue: DeliveryQueue = request.app.state.queue
        checks: dict[str, str] = {}
        try:
            await delivery_queue.redis.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "failed"
        checks["worker"] = "ok" if await delivery_queue.worker_is_alive() else "failed"
        telegram = TelegramBotAPIClient(configured_settings)
        try:
            await telegram.get_me()
            checks["telegram_bot_api"] = "ok"
        except Exception:
            checks["telegram_bot_api"] = "failed"
        finally:
            await telegram.close()
        if any(value != "ok" for value in checks.values()):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=checks)
        return {"status": "ok", **checks}

    @app.post(
        "/v1/deliveries",
        response_model=DeliveryStatus,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_internal_token)],
        tags=["deliveries"],
    )
    async def create_delivery(
        body: DeliveryRequest,
        delivery_queue: DeliveryQueue = Depends(get_queue),  # noqa: B008
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            min_length=1,
            max_length=160,
        ),
    ) -> DeliveryStatus:
        if (
            body.expected_size_bytes is not None
            and body.expected_size_bytes > configured_settings.delivery_max_file_bytes
        ):
            raise HTTPException(status_code=413, detail="File exceeds the 2000 MB limit")
        try:
            validate_source_url(str(body.source_url), configured_settings)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        job, _created = await delivery_queue.create(body, idempotency_key)
        return DeliveryStatus.from_stored(job)

    @app.get(
        "/v1/deliveries/{job_id}",
        response_model=DeliveryStatus,
        dependencies=[Depends(require_internal_token)],
        tags=["deliveries"],
    )
    async def get_delivery(
        job_id: str,
        delivery_queue: DeliveryQueue = Depends(get_queue),  # noqa: B008
    ) -> DeliveryStatus:
        job = await delivery_queue.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Delivery not found")
        return DeliveryStatus.from_stored(job)

    return app


app = create_app()
