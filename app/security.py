from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from app.config import Settings


def host_matches(host: str, patterns: tuple[str, ...]) -> bool:
    normalized = host.lower().rstrip(".")
    for pattern in patterns:
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if normalized.endswith(suffix) and normalized != suffix.lstrip("."):
                return True
        elif normalized == pattern:
            return True
    return False


def validate_source_url(url: str, settings: Settings) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in ({"https", "http"} if settings.source_allow_http else {"https"}):
        raise ValueError("source URL scheme is not allowed")
    if parsed.username or parsed.password:
        raise ValueError("source URL credentials are not allowed")
    if parsed.fragment:
        raise ValueError("source URL fragments are not allowed")
    if not parsed.hostname:
        raise ValueError("source URL host is missing")
    if not host_matches(parsed.hostname, settings.allowed_source_host_patterns):
        raise ValueError("source URL host is not allowed")


async def require_internal_token(request: Request) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.api_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Delivery API authentication is not configured",
        )
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid delivery API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
