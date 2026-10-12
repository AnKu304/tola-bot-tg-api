from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import DeliveryRequest, MediaKind


@pytest.mark.parametrize(
    "filename",
    [" ", "\t", "../secret", "folder/video.mp4", ".", "bad\x00name"],
)
def test_filename_cannot_escape_delivery_directory(filename: str) -> None:
    with pytest.raises(ValidationError):
        DeliveryRequest(
            chat_id=1,
            source_url="https://files.example.test/result.mp4",
            filename=filename,
        )


def test_caption_respects_telegram_limit() -> None:
    with pytest.raises(ValidationError):
        DeliveryRequest(
            chat_id=1,
            source_url="https://files.example.test/result.mp4",
            filename="result.mp4",
            caption="x" * 1025,
        )


def test_video_metadata_is_optional_for_existing_requests() -> None:
    request = DeliveryRequest(
        chat_id=1, source_url="https://files.example.test/result.mp4",
        filename="result.mp4", media_kind=MediaKind.VIDEO,
    )
    assert (request.width, request.height, request.duration) == (None, None, None)


@pytest.mark.parametrize("metadata", [
    {"width": 816},
    {"height": 1104},
    {"width": True, "height": 1104},
    {"width": 0, "height": 1104},
    {"duration": 0},
    {"duration": 86401},
])
def test_invalid_video_metadata_is_rejected(metadata: dict) -> None:
    with pytest.raises(ValidationError):
        DeliveryRequest(
            chat_id=1, source_url="https://files.example.test/result.mp4",
            filename="result.mp4", media_kind=MediaKind.VIDEO, **metadata,
        )


def test_non_video_cannot_claim_video_metadata() -> None:
    with pytest.raises(ValidationError):
        DeliveryRequest(
            chat_id=1, source_url="https://files.example.test/result.jpg",
            filename="result.jpg", media_kind=MediaKind.PHOTO, duration=15,
        )
