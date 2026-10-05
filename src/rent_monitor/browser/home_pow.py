"""Persistent subprocess transport for the home-route Avito protection flow."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from rent_monitor.browser.transport import BrowserPage


class HomeRouteUnavailable(RuntimeError):
    """The local tunnel or its home network route is temporarily unavailable."""


class HomePowTransport:
    def __init__(
        self,
        state_directory: Path,
        max_response_bytes: int,
        *,
        python_path: str,
        worker_path: str,
        proxy: str,
        timeout_seconds: float = 120,
    ):
        parts = urlsplit(proxy)
        if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port != 18782:
            raise ValueError("Home proxy must be the local SSH forward")
        self.state_directory = state_directory
        self.max_response_bytes = max_response_bytes
        self.python_path, self.worker_path, self.proxy = python_path, worker_path, proxy
        self.timeout_seconds = timeout_seconds
        self._process = None
        self._lock = asyncio.Lock()
        self._last_page = None
        self._search_url = "https://www.avito.ru/"

    async def start(self):
        async with self._lock:
            await self._start()

    async def _start(self):
        if self._process is not None and self._process.returncode is None:
            return
        self.state_directory.mkdir(parents=True, exist_ok=True)
        self._process = await asyncio.create_subprocess_exec(
            self.python_path,
            self.worker_path,
            "--state",
            str(self.state_directory),
            "--proxy",
            self.proxy,
            "--max-bytes",
            str(self.max_response_bytes),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=6 * self.max_response_bytes + 16384,
        )

    async def _close(self):
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()

    async def _request(self, url, referer):
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in {"avito.ru", "www.avito.ru"}
            or parts.port not in {None, 443}
            or parts.username
            or parts.password
        ):
            raise ValueError("Expected public Avito HTTPS URL")
        async with self._lock:
            await self._start()

            async def exchange():
                self._process.stdin.write(
                    (json.dumps({"url": url, "referer": referer}) + "\n").encode()
                )
                await self._process.stdin.drain()
                raw = await self._process.stdout.readline()
                if not raw:
                    raise RuntimeError("Protection worker exited")
                result = json.loads(raw)
                if not result.get("ok"):
                    if result.get("error_type") in {
                        "ProxyError",
                        "ConnectionError",
                        "Timeout",
                        "DNSError",
                        "ConnectTimeout",
                    }:
                        raise HomeRouteUnavailable("Home route temporarily unavailable")
                    raise RuntimeError("Protection worker request failed")
                if len(result["html"].encode("utf-8")) > self.max_response_bytes:
                    raise RuntimeError("Page exceeded configured size limit")
                return BrowserPage(
                    status_code=result["status"],
                    final_url=result["url"],
                    html=result["html"],
                    observed_at=datetime.now(UTC),
                )

            try:
                page = await asyncio.wait_for(exchange(), timeout=self.timeout_seconds)
            except BaseException:
                await self._close()
                raise
            self._last_page = page
            return page

    async def fetch(self, url):
        self._search_url = url
        return await self._request(url, "https://www.avito.ru/")

    async def fetch_listing(self, source_id, url):
        return await self._request(url, self._search_url)

    async def current_page(self):
        return self._last_page

    async def screenshot(self):
        return None

    async def restart(self):
        async with self._lock:
            await self._close()
            await self._start()

    async def aclose(self):
        async with self._lock:
            await self._close()
