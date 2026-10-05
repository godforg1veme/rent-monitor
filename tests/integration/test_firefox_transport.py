"""Installed Firefox fixture test: local HTTP only, never Avito or Telegram."""

import shutil
import tempfile
import unittest
from pathlib import Path

from rent_monitor.browser.firefox import FirefoxBrowserTransport
from tests.integration.test_browser_transport import fixture_server

WINDOWS_FIREFOX = Path("C:/Program Files/Mozilla Firefox/firefox.exe")
FIREFOX_BINARY = str(WINDOWS_FIREFOX) if WINDOWS_FIREFOX.is_file() else shutil.which("firefox")


@unittest.skipUnless(FIREFOX_BINARY, "Installed Firefox is not available")
class InstalledFirefoxTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_status_and_cookie_survive_restart(self):
        async with fixture_server() as (url, state):
            with tempfile.TemporaryDirectory() as directory:
                transport = FirefoxBrowserTransport(
                    Path(directory),
                    headless=True,
                    binary_path=FIREFOX_BINARY,
                    allowed_test_origins=frozenset({url.rsplit("/", 1)[0]}),
                )
                try:
                    first = await transport.fetch(url)
                    await transport.restart()
                    second = await transport.fetch(url)
                finally:
                    await transport.aclose()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn("cookie=False", first.html)
        self.assertIn("cookie=True", second.html)
        self.assertEqual(state.max_active, 1)


if __name__ == "__main__":
    unittest.main()
