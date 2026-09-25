"""Bounded HTTP transport for public search and listing pages."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from urllib.parse import urljoin, urlsplit

import httpx


ALLOWED_HOSTS = frozenset(
    {
        "avito.ru",
        "cian.ru",
        "domclick.ru",
        "yandex.ru",
        "ya.ru",
    }
)


class ResponseTooLargeError(RuntimeError):
    """The decompressed response exceeded the configured byte limit."""


class UnsafePageUrlError(ValueError):
    """A page URL uses an unapproved scheme or host."""


class RedirectLimitError(RuntimeError):
    """The server exceeded the redirect limit."""


class BoundedHttpClient:
    """HTTPX wrapper that serializes requests and caps decompressed responses."""

    def __init__(self, max_response_bytes: int = 8 * 1024 * 1024) -> None:
        if not 1024 <= max_response_bytes <= 8 * 1024 * 1024:
            raise ValueError("max_response_bytes must be between 1 KiB and 8 MiB")
        self.max_response_bytes = max_response_bytes
        self._semaphore = asyncio.Semaphore(1)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=25, write=10, pool=10),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={
                "User-Agent": "RentMonitor/0.1 (+polite public-page polling)",
                "Accept-Language": "ru,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=False,
            max_redirects=5,
            trust_env=False,
        )

    async def __aenter__(self) -> BoundedHttpClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_text(self, url: str) -> tuple[int, str, Mapping[str, str]]:
        """Fetch one public HTTPS page and return status, decoded text and headers.

        Redirects are followed only when they remain on one of the approved source
        domains. A new request acquires the global semaphore, including redirects.
        """
        current_url = url
        for redirect_number in range(6):
            self._validate_url(current_url)
            async with self._semaphore:
                async with self._client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if location:
                            if redirect_number == 5:
                                raise RedirectLimitError("Too many redirects")
                            current_url = urljoin(str(response.url), location)
                            self._validate_url(current_url)
                            continue

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self.max_response_bytes:
                            raise ResponseTooLargeError("Response exceeded configured size limit")
                        body.extend(chunk)
                    encoding = response.encoding or "utf-8"
                    text = bytes(body).decode(encoding, errors="replace")
                    return response.status_code, text, dict(response.headers)
        raise RedirectLimitError("Too many redirects")

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not hostname:
            raise UnsafePageUrlError("Only HTTPS listing pages are allowed")
        if not any(hostname == domain or hostname.endswith(f".{domain}") for domain in ALLOWED_HOSTS):
            raise UnsafePageUrlError("Page host is outside the configured source domains")
