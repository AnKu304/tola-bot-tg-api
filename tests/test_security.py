from __future__ import annotations

import pytest

from app.config import Settings
from app.security import host_matches, validate_source_url


def test_host_allowlist_exact_and_wildcard() -> None:
    patterns = ("files.example.test", "*.objects.example.test")
    assert host_matches("files.example.test", patterns)
    assert host_matches("bucket.objects.example.test", patterns)
    assert not host_matches("objects.example.test", patterns)
    assert not host_matches("files.example.test.evil.test", patterns)


@pytest.mark.parametrize(
    "url",
    [
        "http://files.example.test/file.mp4",
        "https://user:password@files.example.test/file.mp4",
        "https://files.example.test/file.mp4#fragment",
        "https://127.0.0.1/file.mp4",
    ],
)
def test_source_url_rejects_unsafe_variants(settings: Settings, url: str) -> None:
    with pytest.raises(ValueError):
        validate_source_url(url, settings)


def test_source_url_accepts_trusted_signed_url(settings: Settings) -> None:
    validate_source_url(
        "https://bucket.objects.example.test/result.mp4?X-Amz-Signature=secret",
        settings,
    )
