from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import DeliveryRequest, DeliveryState, StoredDelivery


class StubQueue:
    def __init__(self) -> None:
        self.jobs: dict[str, StoredDelivery] = {}

    async def create(
        self, request: DeliveryRequest, idempotency_key: str | None
    ) -> tuple[StoredDelivery, bool]:
        job_id = idempotency_key or "generated-id"
        if job_id in self.jobs:
            return self.jobs[job_id], False
        job = StoredDelivery(id=job_id, state=DeliveryState.QUEUED, request=request)
        self.jobs[job_id] = job
        return job, True

    async def get(self, job_id: str) -> StoredDelivery | None:
        return self.jobs.get(job_id)


def payload(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "chat_id": 42,
        "source_url": "https://files.example.test/result.mp4?signature=secret",
        "filename": "result.mp4",
        "media_kind": "video",
        "expected_size_bytes": 900,
    }
    value.update(overrides)
    return value


def test_delivery_api_requires_token_and_is_idempotent(settings: Settings) -> None:
    queue = StubQueue()
    app = create_app(settings=settings, queue=queue)  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert client.post("/v1/deliveries", json=payload()).status_code == 401
        headers = {
            "Authorization": f"Bearer {settings.api_token}",
            "Idempotency-Key": "generation-1",
        }
        first = client.post("/v1/deliveries", headers=headers, json=payload())
        second = client.post("/v1/deliveries", headers=headers, json=payload())

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"] == "generation-1"
    assert "source_url" not in first.json()


def test_delivery_api_rejects_announced_oversize(settings: Settings) -> None:
    app = create_app(settings=settings, queue=StubQueue())  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.post(
            "/v1/deliveries",
            headers={"Authorization": f"Bearer {settings.api_token}"},
            json=payload(expected_size_bytes=1025),
        )
    assert response.status_code == 413
