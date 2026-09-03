from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

import httpx

MediaKind = Literal["document", "video", "photo", "audio", "animation"]
CLOUD_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024


class DeliveryRoute(StrEnum):
    DIRECT = "direct"
    QUEUE = "queue"


def choose_delivery_route(expected_size_bytes: int | None) -> DeliveryRoute:
    """Choose the TolaAI delivery path before reading an S3 object into memory.

    Files at the cloud limit are queued conservatively. After the mandatory
    cloud-to-local cutover, ``DIRECT`` still means the configured local Bot API,
    never api.telegram.org.
    """
    if expected_size_bytes is None:
        return DeliveryRoute.QUEUE
    if expected_size_bytes <= 0:
        raise ValueError("routing requires positive expected_size_bytes")
    if expected_size_bytes >= CLOUD_UPLOAD_LIMIT_BYTES:
        return DeliveryRoute.QUEUE
    return DeliveryRoute.DIRECT


class TolaTelegramDeliveryClient:
    """Small integration client which may be copied into the TolaAI backend."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout_seconds

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def enqueue(
        self,
        *,
        chat_id: int | str,
        source_url: str,
        filename: str,
        idempotency_key: str,
        media_kind: MediaKind = "document",
        mime_type: str | None = None,
        expected_size_bytes: int | None = None,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {**self._headers, "Idempotency-Key": idempotency_key}
        payload = {
            "chat_id": chat_id,
            "source_url": source_url,
            "filename": filename,
            "media_kind": media_kind,
            "mime_type": mime_type,
            "expected_size_bytes": expected_size_bytes,
            "caption": caption,
            "reply_markup": reply_markup,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/v1/deliveries",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def get(self, delivery_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/v1/deliveries/{delivery_id}",
                headers=self._headers,
            )
            response.raise_for_status()
            return response.json()
