from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from rent_monitor.browser.transport import PlaywrightBrowserTransport, UnsafeBrowserUrlError


@dataclass(slots=True)
class FixtureState:
    requests: int = 0
    active: int = 0
    max_active: int = 0


@asynccontextmanager
async def fixture_server(*, response_delay: float = 0.0) -> AsyncIterator[tuple[str, FixtureState]]:
    state = FixtureState()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state.active += 1
        state.max_active = max(state.max_active, state.active)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            state.requests += 1
            if response_delay:
                await asyncio.sleep(response_delay)
            has_cookie = b"cookie: rm_test=present" in request.lower()
            body = (
                '<html><head><link rel="icon" href="data:,"></head>'
                f"<body>cookie={has_cookie}</body></html>"
            ).encode()
            headers = [
                b"HTTP/1.1 200 OK",
                b"Content-Type: text/html; charset=utf-8",
                f"Content-Length: {len(body)}".encode(),
                b"Connection: close",
            ]
            if not has_cookie:
                headers.append(b"Set-Cookie: rm_test=present; Max-Age=3600; Path=/; SameSite=Lax")
            writer.write(b"\r\n".join(headers) + b"\r\n\r\n" + body)
            await writer.drain()
        finally:
            state.active -= 1
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/search"
    try:
        yield url, state
    finally:
        server.close()
        await server.wait_closed()


class BrowserTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_context_reuses_cookie_across_restart(self) -> None:
        async with fixture_server() as (url, state):
            origin = url.rsplit("/", 1)[0]
            with tempfile.TemporaryDirectory() as directory:
                transport = PlaywrightBrowserTransport(
                    Path(directory) / "profile",
                    headless=True,
                    allowed_test_origins={origin},
                )
                await transport.start()
                first = await transport.fetch(url)
                await transport.restart()
                second = await transport.fetch(url)
                await transport.aclose()

        self.assertEqual(first.status_code, 200)
        self.assertIn("cookie=False", first.html)
        self.assertIn("cookie=True", second.html)
        self.assertEqual(state.requests, 2)

    async def test_concurrent_fetches_are_serialized(self) -> None:
        async with fixture_server(response_delay=0.10) as (url, state):
            origin = url.rsplit("/", 1)[0]
            with tempfile.TemporaryDirectory() as directory:
                transport = PlaywrightBrowserTransport(
                    Path(directory) / "profile",
                    headless=True,
                    allowed_test_origins={origin},
                )
                await transport.start()
                await asyncio.gather(transport.fetch(url), transport.fetch(url))
                await transport.aclose()

        self.assertEqual(state.max_active, 1)
        self.assertEqual(state.requests, 2)

    async def test_rejects_non_avito_url_without_test_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transport = PlaywrightBrowserTransport(Path(directory) / "profile", headless=True)
            with self.assertRaises(UnsafeBrowserUrlError):
                await transport.fetch("https://example.org/")


if __name__ == "__main__":
    unittest.main()
