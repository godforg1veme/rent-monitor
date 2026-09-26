from __future__ import annotations

import os
import re
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rent_monitor.browser.captcha import CaptchaSessionManager
from rent_monitor.browser.transport import BrowserPage
from rent_monitor.core.models import SearchCriteria, SourceAlert
from rent_monitor.core.scheduler import check_manual_attention_source
from rent_monitor.core.source_state import SourceRunHealth, SourceRunState
from rent_monitor.storage.sqlite import SQLiteRepository
from rent_monitor.telegram.bot import TelegramNotifier


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class CaptchaSessionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name) / "tokens"
        self.clock = Clock()
        self.manager = CaptchaSessionManager(
            self.directory,
            "https://rent-monitor.example.ts.net",
            ttl_seconds=900,
            clock=self.clock,
        )

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_issue_creates_private_token_file_and_expiry_removes_it(self) -> None:
        session = await self.manager.issue("avito")
        token_file = self.directory / session.token

        self.assertEqual(token_file.read_text(encoding="ascii"), "127.0.0.1:5900\n")
        self.assertIn(f"token={session.token}", session.url)
        self.assertRegex(session.token, r"^[A-Za-z0-9_-]+$")
        self.assertNotIn("avito", session.token)
        if os.name != "nt":
            self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)

        self.clock.advance(timedelta(minutes=16))
        await self.manager.remove_expired()

        self.assertFalse(token_file.exists())
        self.assertIsNone(await self.manager.active_session())

    async def test_only_one_session_can_be_active(self) -> None:
        first = await self.manager.issue("avito")
        second = await self.manager.issue("avito")

        self.assertEqual(first.token, second.token)
        self.assertEqual([path.name for path in self.directory.iterdir()], [first.token])

    async def test_startup_removes_stale_token_files(self) -> None:
        self.directory.mkdir(parents=True)
        stale = self.directory / "stale-token"
        stale.write_text("127.0.0.1:5900\n", encoding="ascii")

        await self.manager.issue("avito")

        self.assertFalse(stale.exists())
        self.assertTrue(
            all(re.fullmatch(r"[A-Za-z0-9_-]+", path.name) for path in self.directory.iterdir())
        )

    async def test_manual_check_resumes_only_verified_current_page(self) -> None:
        url = "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok/bez_komissii-test?s=104"

        class Browser:
            def __init__(self, html: str) -> None:
                self.html = html
                self.inspections = 0

            async def current_page(self) -> BrowserPage:
                self.inspections += 1
                return BrowserPage(200, url, self.html, self_now)

        self_now = self.clock.value
        repository = SQLiteRepository(":memory:")
        await repository.initialize()
        await repository.save_source_run_state(SourceRunState.manual_attention("avito", "captcha"))
        criteria = SearchCriteria("Москва", 2, 70_000, True)
        captcha_browser = Browser("<html><body>Подтвердите, что вы не робот</body></html>")

        still_blocked = await check_manual_attention_source(
            "avito", captcha_browser, repository, criteria, now=self.clock
        )
        captcha_browser.html = """
            <html><body>
              <h1>Аренда квартир на длительный срок в Москве без комиссии</h1>
              <button data-marker="filter-active">Без комиссии</button>
              <button data-marker="filter-active">Сначала новые</button>
              <main data-marker="catalog-serp">
                <div data-marker="item" data-item-id="1111111111">
                  <a data-marker="item-title"
                     href="/moskva/kvartiry/sdam/2-komnatnye/1111111111">
                    2-к. квартира
                  </a>
                  <span data-marker="item-price-value">65 000</span>
                  <div data-marker="item-specific-params">Без комиссии</div>
                </div>
              </main>
            </body></html>
        """
        resumed = await check_manual_attention_source(
            "avito", captcha_browser, repository, criteria, now=self.clock
        )
        state = await repository.get_source_run_state("avito")
        await repository.close()

        self.assertFalse(still_blocked)
        self.assertTrue(resumed)
        self.assertEqual(captcha_browser.inspections, 2)
        self.assertEqual(state.health, SourceRunHealth.HEALTHY)

    async def test_captcha_alert_contains_actions_but_not_a_session_url(self) -> None:
        class Bot:
            def __init__(self) -> None:
                self.arguments = None

            async def send_message(self, **kwargs) -> None:
                self.arguments = kwargs

        bot = Bot()
        notifier = TelegramNotifier(bot)
        alert = SourceAlert(
            alert_id=1,
            event_key="avito:event:manual_attention",
            source="avito",
            health="manual_attention",
            failure_code="captcha",
            occurred_at=self.clock.value,
            last_success_at=None,
            next_attempt_at=None,
            outage_seconds=None,
        )

        await notifier.send_source_alert(4242, alert)

        keyboard = bot.arguments["reply_markup"].inline_keyboard[0]
        self.assertEqual(
            [button.callback_data for button in keyboard],
            ["captcha:open:avito", "captcha:check:avito"],
        )
        self.assertNotIn("https://", bot.arguments["text"])


if __name__ == "__main__":
    unittest.main()
