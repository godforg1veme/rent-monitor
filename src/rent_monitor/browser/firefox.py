"""Installed Firefox, serialized off the event loop, with proxy-only authentication."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait

from rent_monitor.browser.transport import BrowserPage, PlaywrightBrowserTransport
from rent_monitor.parsers.avito import page_block_reason


def is_rendered_page_ready(signals: dict[str, Any], status: int | None) -> bool:
    """Wait for the website's own automatic check; never interact with CAPTCHA."""
    if signals.get("has_results"):
        return True
    reason = page_block_reason(signals.get("visible_text", ""))
    if reason == "captcha":
        return True
    if reason == "browser_verification" or status == 439:
        return False
    if reason == "access_restricted":
        return True
    return status == 200 and signals.get("ready_state") == "complete"


@dataclass(frozen=True, slots=True)
class ProxyCredentials:
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)

    @classmethod
    def load(cls, path: Path) -> ProxyCredentials:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            host, port = raw["host"], raw["port"]
            username, password = raw["username"], raw["password"]
            if (
                not isinstance(host, str)
                or not host
                or len(host) > 253
                or any(c in host for c in "/@: \r\n\t")
                or isinstance(port, bool)
                or not isinstance(port, int)
                or not 1 <= port <= 65535
                or not isinstance(username, str)
                or not username
                or not isinstance(password, str)
                or not password
            ):
                raise ValueError
            return cls(host, port, username, password)
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("Invalid browser proxy credential file") from None


class FirefoxBrowserTransport:
    """Own one dedicated persistent Firefox profile; never automate CAPTCHA."""

    def __init__(
        self,
        profile_path: Path,
        *,
        headless: bool = False,
        proxy: ProxyCredentials | None = None,
        binary_path: str | None = None,
        driver_path: str | None = None,
        navigation_timeout_seconds: int = 45,
        allowed_test_origins: frozenset[str] = frozenset(),
        driver_factory: Callable[..., Any] = webdriver.Firefox,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if navigation_timeout_seconds < 1 or max_response_bytes < 1:
            raise ValueError("Browser limits must be positive")
        self.profile_path = profile_path
        self.headless = headless
        self.proxy = proxy
        self.binary_path = binary_path
        self.driver_path = driver_path
        self.navigation_timeout_seconds = navigation_timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._driver_factory = driver_factory
        self._validator = PlaywrightBrowserTransport(
            profile_path, allowed_test_origins=allowed_test_origins
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="avito-firefox")
        self._lock = asyncio.Lock()
        self._status_lock = threading.Lock()
        self._driver = None
        self._context_id = None
        self._status = None
        self._headers: dict[str, str] = {}
        self._closed = False
        self._auth_attempts: dict[str, int] = {}

    async def _run(self, callback):
        async with self._lock:
            if self._closed:
                raise RuntimeError("Firefox transport is closed")
            return await asyncio.get_running_loop().run_in_executor(self._executor, callback)

    async def start(self) -> None:
        await self._run(self._start)

    def _start(self) -> None:
        if self._driver is not None:
            return
        self.profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        options = Options()
        if self.binary_path:
            options.binary_location = self.binary_path
        if self.headless:
            options.add_argument("-headless")
        options.add_argument("-no-remote")
        options.add_argument("-profile")
        options.add_argument(str(self.profile_path.resolve()))
        options.page_load_strategy = "eager"
        options.enable_bidi = True
        if self.proxy:
            options.set_preference("network.proxy.type", 1)
            for protocol in ("http", "ssl"):
                options.set_preference(f"network.proxy.{protocol}", self.proxy.host)
                options.set_preference(f"network.proxy.{protocol}_port", self.proxy.port)
            options.set_preference("network.proxy.no_proxies_on", "localhost, 127.0.0.1")
        driver = self._driver_factory(
            options=options, service=Service(executable_path=self.driver_path, log_output=-3)
        )
        try:
            self._context_id = driver.current_window_handle
            driver.set_page_load_timeout(self.navigation_timeout_seconds)
            driver.network.add_event_handler("response_started", self._observe_response)
            if self.proxy:
                driver.network.add_authentication_handler(self._authenticate_proxy)
            self._driver = driver
        except Exception:
            driver.quit()
            raise

    def _authenticate_proxy(self, challenge) -> None:
        # Selenium 4.49's public AuthenticationRequest omits HTTP status. Its raw
        # BiDi response is needed to distinguish proxy 407 from origin 401.
        status = challenge._params.get("response", {}).get("status")
        request_id = challenge._params.get("request", {}).get("request", "")
        attempts = self._auth_attempts.get(request_id, 0)
        if status == 407 and self.proxy is not None and attempts < 2:
            self._auth_attempts[request_id] = attempts + 1
            challenge.provide_credentials(self.proxy.username, self.proxy.password)
        else:
            challenge.cancel()

    def _observe_response(self, event) -> None:
        if event.get("context") != self._context_id or event.get("navigation") is None:
            return
        response = event.get("response", {})
        headers = {}
        for header in response.get("headers", []):
            name = str(header.get("name", "")).lower()
            value = header.get("value", {})
            if name in {"retry-after", "content-type"} and value.get("type") == "string":
                headers[name] = value.get("value", "")
        with self._status_lock:
            self._status = response.get("status")
            self._headers = headers

    def _snapshot(self) -> BrowserPage | None:
        if self._driver is None:
            return None
        html = self._driver.page_source
        if len(html.encode("utf-8")) > self.max_response_bytes:
            raise RuntimeError("Rendered page exceeds configured size limit")
        with self._status_lock:
            return BrowserPage(
                self._status,
                self._driver.current_url,
                html,
                datetime.now(UTC),
                response_headers=dict(self._headers),
            )

    async def fetch(self, url: str) -> BrowserPage:
        self._validator._validate_url(url)
        return await self._run(lambda: self._fetch(url))

    def _fetch(self, url: str) -> BrowserPage:
        self._start()
        with self._status_lock:
            self._status, self._headers = None, {}
        self._auth_attempts.clear()
        self._driver.get(url)
        try:
            WebDriverWait(self._driver, self.navigation_timeout_seconds).until(self._page_ready)
        except TimeoutException:
            # Snapshot unknown markup for fail-closed classification by collector.
            pass
        page = self._snapshot()
        if page is None:
            raise RuntimeError("Firefox page was not initialized")
        if urlsplit(page.final_url).scheme == "about":
            raise RuntimeError("Firefox navigation did not finish")
        return page

    def _page_ready(self, driver) -> bool:
        signals = driver.execute_script("""
            const results = !!document.querySelector(
                '[data-marker="catalog-serp"] [data-marker="item"]');
            return {has_results: results, ready_state: document.readyState,
                visible_text: results ? '' :
                    (document.body ? document.body.innerText.slice(0, 20000) : '')};
        """)
        with self._status_lock:
            status = self._status
        return is_rendered_page_ready(signals, status)

    async def current_page(self) -> BrowserPage | None:
        return await self._run(self._snapshot)

    async def screenshot(self) -> bytes | None:
        return await self._run(
            lambda: self._driver.get_screenshot_as_png() if self._driver is not None else None
        )

    def _quit(self) -> None:
        driver, self._driver = self._driver, None
        if driver is not None:
            driver.quit()
        with self._status_lock:
            self._status, self._headers = None, {}

    async def restart(self) -> None:
        def restart():
            self._quit()
            self._start()

        await self._run(restart)

    async def aclose(self) -> None:
        if self._closed:
            return
        try:
            await self._run(self._quit)
        finally:
            self._closed = True
            self._executor.shutdown(wait=False)
