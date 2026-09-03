from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, field_validator


class MediaKind(StrEnum):
    DOCUMENT = "document"
    VIDEO = "video"
    PHOTO = "photo"
    AUDIO = "audio"
    ANIMATION = "animation"


class DeliveryState(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    SENDING = "sending"
    RETRY_SCHEDULED = "retry_scheduled"
    SENT = "sent"
    FAILED = "failed"


class DeliveryRequest(BaseModel):
    chat_id: int | str
    source_url: HttpUrl
    filename: str = Field(min_length=1, max_length=180)
    media_kind: MediaKind = MediaKind.DOCUMENT
    mime_type: str | None = Field(default=None, max_length=120)
    caption: str | None = Field(default=None, max_length=1024)
    parse_mode: str | None = Field(default=None, pattern="^(HTML|MarkdownV2)$")
    reply_markup: dict[str, Any] | None = None
    disable_notification: bool = False
    protect_content: bool = False
    supports_streaming: bool = True
    fallback_to_document: bool = True
    expected_size_bytes: int | None = Field(default=None, ge=1)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("filename must not be blank")
        if cleaned != Path(cleaned).name or cleaned in {".", ".."}:
            raise ValueError("filename must not contain a path")
        if re.search(r"[\x00-\x1f\x7f]", cleaned):
            raise ValueError("filename must not contain control characters")
        return cleaned

    @field_validator("chat_id")
    @classmethod
    def validate_chat_id(cls, value: int | str) -> int | str:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("chat_id must not be empty")
        return value


class StoredDelivery(BaseModel):
    id: str
    state: DeliveryState
    request: DeliveryRequest
    attempts: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    next_attempt_at: datetime | None = None
    downloaded_size_bytes: int | None = None
    telegram_message_id: int | None = None
    telegram_file_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class DeliveryStatus(BaseModel):
    id: str
    state: DeliveryState
    chat_id: int | str
    filename: str
    media_kind: MediaKind
    attempts: int
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime | None = None
    downloaded_size_bytes: int | None = None
    telegram_message_id: int | None = None
    telegram_file_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def from_stored(cls, job: StoredDelivery) -> DeliveryStatus:
        return cls(
            id=job.id,
            state=job.state,
            chat_id=job.request.chat_id,
            filename=job.request.filename,
            media_kind=job.request.media_kind,
            attempts=job.attempts,
            created_at=job.created_at,
            updated_at=job.updated_at,
            next_attempt_at=job.next_attempt_at,
            downloaded_size_bytes=job.downloaded_size_bytes,
            telegram_message_id=job.telegram_message_id,
            telegram_file_id=job.telegram_file_id,
            error_code=job.error_code,
            error_message=job.error_message,
        )
