from __future__ import annotations

import pytest

from examples.tolaai_client import (
    DeliveryRoute,
    choose_delivery_route,
)

MIB = 1024 * 1024


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        pytest.param(49 * MIB, DeliveryRoute.DIRECT, id="49-MiB-direct"),
        pytest.param(50 * MIB, DeliveryRoute.QUEUE, id="50-MiB-safe-boundary"),
        pytest.param(51 * MIB, DeliveryRoute.QUEUE, id="51-MiB-queued"),
        pytest.param(None, DeliveryRoute.QUEUE, id="unknown-size-queued"),
    ],
)
def test_delivery_route_uses_queue_at_cloud_limit(
    size_bytes: int, expected: DeliveryRoute
) -> None:
    assert choose_delivery_route(size_bytes) is expected


@pytest.mark.parametrize("size_bytes", [0, -1])
def test_delivery_route_rejects_non_positive_size(size_bytes: int) -> None:
    with pytest.raises(ValueError, match="positive expected_size_bytes"):
        choose_delivery_route(size_bytes)
