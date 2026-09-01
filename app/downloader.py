from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import aiofiles
import httpx

from app.config import Settings
from app.security import validate_source_url


@dataclass(frozen=True)
class DownloadResult:
    size_bytes: int
    content_type: str | None


class DownloadError(Exception):
    def __init__(self, message: str, *, retryable: bool, code: str = "download_failed") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


async def download_to_path(
    source_url: str,
    destination: Path,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> DownloadResult:
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=settings.source_connect_timeout_seconds,
                read=settings.source_read_timeout_seconds,
                write=30.0,
                pool=settings.source_connect_timeout_seconds,
            ),
        )
    assert client is not None

    current_url = source_url
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        for redirect_number in range(settings.source_max_redirects + 1):
            validate_source_url(current_url, settings)
            try:
                async with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadError(
                                "source returned a redirect without Location",
                                retryable=False,
                            )
                        if redirect_number >= settings.source_max_redirects:
                            raise DownloadError("too many source redirects", retryable=False)
                        current_url = urljoin(current_url, location)
                        continue

                    if response.status_code >= 500 or response.status_code in {408, 429}:
                        raise DownloadError(
                            f"source returned HTTP {response.status_code}", retryable=True
                        )
                    if response.status_code >= 400:
                        raise DownloadError(
                            f"source returned HTTP {response.status_code}", retryable=False
                        )

                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            announced_size = int(content_length)
                        except ValueError as exc:
                            raise DownloadError(
                                "source returned an invalid Content-Length",
                                retryable=False,
                                code="invalid_content_length",
                            ) from exc
                        if announced_size < 0:
                            raise DownloadError(
                                "source returned an invalid Content-Length",
                                retryable=False,
                                code="invalid_content_length",
                            )
                        if announced_size > settings.delivery_max_file_bytes:
                            raise DownloadError(
                                "source file exceeds the configured 2000 MB limit",
                                retryable=False,
                                code="file_too_large",
                            )

                    size = 0
                    async with aiofiles.open(destination, "wb") as output:
                        async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                            size += len(chunk)
                            if size > settings.delivery_max_file_bytes:
                                raise DownloadError(
                                    "source file exceeds the configured 2000 MB limit",
                                    retryable=False,
                                    code="file_too_large",
                                )
                            await output.write(chunk)
                    if size == 0:
                        raise DownloadError("source file is empty", retryable=False)
                    return DownloadResult(
                        size_bytes=size,
                        content_type=response.headers.get("content-type", "").split(";", 1)[0]
                        or None,
                    )
            except DownloadError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise DownloadError("source download failed temporarily", retryable=True) from exc
        raise DownloadError("too many source redirects", retryable=False)
    except Exception:
        destination.unlink(missing_ok=True)  # noqa: ASYNC240 - one local metadata operation
        raise
    finally:
        if own_client:
            await client.aclose()
