from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import DeliveryRequest


@pytest.mark.parametrize("filename", ["../secret", "folder/video.mp4", ".", "bad\x00name"])
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
