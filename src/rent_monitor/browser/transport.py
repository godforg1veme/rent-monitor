"""Persistent Playwright transport for rendered public pages."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)


class UnsafeBrowserUrlError(ValueError):
    """A browser URL is outside the explicitly allowed origins."""


@dataclass(frozen=True, slots=True)
class BrowserPage:
    status_code: int | None
    final_url: str
    html: str
    observed_at: datetime
    screenshot_png: bytes | None = None
    response_headers: Mapping[str, str] = field(default_factory=dict)


class BrowserTransport(Protocol):
    async def start(self) -> None: ...
    async def fetch(self, url: str) -> BrowserPage: ...
    async def current_page(self) -> BrowserPage | None: ...
    async def screenshot(self) -> bytes | None: ...
    async def restart(self) -> None: ...
    async def aclose(self) -> None: ...


class PlaywrightBrowserTransport:
    """Own one serialized persistent Chromium context."""

    def __init__(
        self,
        profile_path: Path,
        *,
        headless: bool = False,
        navigation_timeout_ms: int = 30_000,
        allowed_test_origins: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        if navigation_timeout_ms < 1:
            raise ValueError("navigation_timeout_ms must be positive")
        self.profile_path = profile_path
        self.headless = headless
        self.navigation_timeout_ms = navigation_timeout_ms
        self.allowed_test_origins = frozenset(origin.rstrip("/") for origin in allowed_test_origins)
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._last_status_code: int | None = None
        self._last_response_headers: Mapping[str, str] = {}

    async def __aenter__(self) -> PlaywrightBrowserTransport:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def start(self) -> None:
        async with self._lock:
            await self._start_unlocked()

    async def _start_unlocked(self) -> None:
        if self._context is not None:
            return
        self.profile_path.mkdir(parents=True, exist_ok=True)
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_path),
            headless=self.headless,
            accept_downloads=False,
            viewport={"width": 1440, "height": 1000},
        )
        self._context.set_default_navigation_timeout(self.navigation_timeout_ms)
        self._page = (
            self._context.pages[0] if self._context.pages else await self._context.new_page()
        )
        self._page.on("response", self._observe_navigation_response)

    def _observe_navigation_response(self, response) -> None:
        if (
            self._page is not None
            and response.request.is_navigation_request()
            and response.frame == self._page.main_frame
        ):
            self._last_status_code = response.status

    async def fetch(self, url: str) -> BrowserPage:
        self._validate_url(url)
        async with self._lock:
            await self._start_unlocked()
            if self._page is None:
                raise RuntimeError("browser page was not initialized")
            response = await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
            try:
                await self._page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass
            self._last_status_code = response.status if response is not None else None
            self._last_response_headers = (
                await response.all_headers() if response is not None else {}
            )
            return BrowserPage(
                status_code=self._last_status_code,
                final_url=self._page.url,
                html=await self._page.content(),
                observed_at=datetime.now(UTC),
                response_headers=self._last_response_headers,
            )

    async def current_page(self) -> BrowserPage | None:
        async with self._lock:
            if self._page is None:
                return None
            return BrowserPage(
                status_code=self._last_status_code,
                final_url=self._page.url,
                html=await self._page.content(),
                observed_at=datetime.now(UTC),
                response_headers=self._last_response_headers,
            )

    async def screenshot(self) -> bytes | None:
        async with self._lock:
            if self._page is None:
                return None
            return await self._page.screenshot(type="png")

    async def restart(self) -> None:
        async with self._lock:
            await self._close_context_unlocked()
            await self._start_unlocked()

    async def aclose(self) -> None:
        async with self._lock:
            await self._close_context_unlocked()
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None

    async def _close_context_unlocked(self) -> None:
        context, self._context = self._context, None
        self._page = None
        self._last_status_code = None
        self._last_response_headers = {}
        if context is not None:
            await context.close()

    def _validate_url(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            hostname = (parsed.hostname or "").lower().rstrip(".")
            port = parsed.port
        except ValueError as exc:
            raise UnsafeBrowserUrlError("Malformed browser URL") from exc

        origin = f"{parsed.scheme}://{hostname}"
        if port is not None:
            origin += f":{port}"
        if (
            origin in self.allowed_test_origins
            and parsed.username is None
            and parsed.password is None
        ):
            return
        if (
            parsed.scheme != "https"
            or not hostname
            or not (hostname == "avito.ru" or hostname.endswith(".avito.ru"))
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
        ):
            raise UnsafeBrowserUrlError("Only public HTTPS Avito URLs are allowed")
