"""Deterministic driver lifecycle and credential isolation tests (no Avito requests)."""

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from rent_monitor.browser.firefox import (
    FirefoxBrowserTransport,
    ProxyCredentials,
    is_rendered_page_ready,
)
from rent_monitor.browser.transport import UnsafeBrowserUrlError


class FakeNetwork:
    def __init__(self):
        self.handlers = {}

    def add_event_handler(self, name, callback):
        self.handlers[name] = callback

    def add_authentication_handler(self, callback):
        self.handlers["auth"] = callback


class FakeDriver:
    def __init__(self, **kwargs):
        self.options = kwargs["options"]
        self.network = FakeNetwork()
        self.current_window_handle = "window"
        self.current_url = "about:blank"
        self.page_source = '<main data-marker="catalog-serp"><div data-marker="item"></div></main>'
        self.commands = []
        self.active = 0
        self.max_active = 0
        self.closed = False

    def set_page_load_timeout(self, seconds):
        self.timeout = seconds

    def get(self, url):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.commands.append(threading.get_ident())
        time.sleep(0.03)
        self.current_url = url
        self.network.handlers["response_started"](
            {
                "context": "window",
                "navigation": "nav",
                "response": {
                    "status": 200,
                    "headers": [
                        {"name": "Set-Cookie", "value": {"type": "string", "value": "secret"}},
                        {"name": "Content-Type", "value": {"type": "string", "value": "text/html"}},
                    ],
                },
            }
        )
        self.active -= 1

    def execute_script(self, script):
        return {"has_results": True, "visible_text": "", "ready_state": "complete"}

    def get_screenshot_as_png(self):
        return b"image"

    def quit(self):
        self.closed = True


class FirefoxTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_security_check_is_waited_but_captcha_is_not(self):
        signals = {
            "has_results": False,
            "ready_state": "complete",
            "visible_text": "Доступ ограничен: проверка безопасности. "
            "Выполняется проверка, подождите...",
        }
        self.assertFalse(is_rendered_page_ready(signals, 439))
        self.assertFalse(is_rendered_page_ready(signals, 200))
        signals["visible_text"] = "Подтвердите, что вы не робот"
        self.assertTrue(is_rendered_page_ready(signals, 439))
        signals["visible_text"] = "Доступ ограничен: проблема с IP"
        self.assertTrue(is_rendered_page_ready(signals, 403))
        signals["has_results"] = True
        self.assertTrue(is_rendered_page_ready(signals, 200))

    async def test_status_serialization_and_manual_navigation(self):
        with tempfile.TemporaryDirectory() as directory:
            drivers = []

            def factory(**kwargs):
                driver = FakeDriver(**kwargs)
                drivers.append(driver)
                return driver

            transport = FirefoxBrowserTransport(Path(directory), driver_factory=factory)
            try:
                pages = await asyncio.gather(
                    transport.fetch("https://www.avito.ru/moskva/kvartiry/"),
                    transport.fetch("https://www.avito.ru/moskva/kvartiry/"),
                )
                self.assertEqual([page.status_code for page in pages], [200, 200])
                self.assertEqual(drivers[0].max_active, 1)
                self.assertEqual(len(set(drivers[0].commands)), 1)
                self.assertNotEqual(drivers[0].commands[0], threading.get_ident())
                self.assertNotIn("set-cookie", pages[0].response_headers)
                transport._observe_response(
                    {
                        "context": "iframe",
                        "navigation": "nav",
                        "response": {"status": 500},
                    }
                )
                self.assertEqual((await transport.current_page()).status_code, 200)
                transport._observe_response(
                    {
                        "context": "window",
                        "navigation": "manual",
                        "response": {"status": 429},
                    }
                )
                self.assertEqual((await transport.current_page()).status_code, 429)
                await transport.restart()
                self.assertTrue(drivers[0].closed)
                self.assertEqual(len(drivers), 2)
                self.assertEqual(await transport.screenshot(), b"image")
            finally:
                await transport.aclose()
            self.assertTrue(drivers[-1].closed)

    async def test_only_proxy_407_receives_credentials_and_retry_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            proxy = ProxyCredentials("127.0.0.1", 1086, "test", "private-password")
            transport = FirefoxBrowserTransport(Path(directory), proxy=proxy)
            try:
                challenge = Mock()
                challenge._params = {"request": {"request": "proxy"}, "response": {"status": 407}}
                for _ in range(3):
                    transport._authenticate_proxy(challenge)
                self.assertEqual(challenge.provide_credentials.call_count, 2)
                challenge.cancel.assert_called_once()
                origin = Mock()
                origin._params = {"request": {"request": "origin"}, "response": {"status": 401}}
                transport._authenticate_proxy(origin)
                origin.cancel.assert_called_once()
                origin.provide_credentials.assert_not_called()
                self.assertNotIn("private-password", repr(proxy))
            finally:
                await transport.aclose()

    async def test_url_validation_precedes_browser_start(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = Mock()
            transport = FirefoxBrowserTransport(Path(directory), driver_factory=factory)
            try:
                for url in ("https://example.com/", "https://user:secret@avito.ru/", "file:///x"):
                    with self.assertRaises(UnsafeBrowserUrlError):
                        await transport.fetch(url)
                factory.assert_not_called()
            finally:
                await transport.aclose()

    async def test_response_size_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = FirefoxBrowserTransport(
                Path(directory), driver_factory=FakeDriver, max_response_bytes=10
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "size limit"):
                    await transport.fetch("https://www.avito.ru/")
            finally:
                await transport.aclose()


if __name__ == "__main__":
    unittest.main()
