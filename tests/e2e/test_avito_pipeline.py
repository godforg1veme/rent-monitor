"""End-to-end Avito HTML fixture → filter → SQLite → Telegram pipeline tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rent_monitor.browser.transport import BrowserPage
from rent_monitor.collectors.avito import AvitoCollector
from rent_monitor.config import AvitoSearchConfig
from rent_monitor.core.models import (
    CommissionStatus,
    FieldEvidence,
    SearchCriteria,
    SourceHealth,
)
from rent_monitor.core.scheduler import (
    CollectorRuntime,
    deliver_outbox_once,
    process_collection_result,
    run_source_once,
)
from rent_monitor.core.source_state import SourceRunHealth, SourceRunState
from rent_monitor.parsers.avito import parse_search_page
from rent_monitor.storage.sqlite import SQLiteRepository

SEARCH_URL = (
    "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok/"
    "bez_komissii-ASgBAgICA0SSA8gQ8AeQUp74DgI?s=104"
)
SEARCH = AvitoSearchConfig("main", SEARCH_URL, 60)


def card(
    source_id: str,
    *,
    price: str = "65\u00a0000",
    details: str = "Залог 65 000 ₽ · Без комиссии · ЖКУ включены",
    title: str = "2-к. квартира, 48 м², 3/9 эт.",
    path: str | None = None,
    description: str = "Не сохранять описание карточки или контакты продавца.",
    published: str | None = None,
) -> str:
    href = path or f"/moskva/kvartiry/sdam/2-komnatnye/{source_id}"
    published_html = (
        f'<time data-marker="item-date" datetime="{published}">{published}</time>'
        if published
        else ""
    )
    return f"""
    <div data-marker="item" data-item-id="{source_id}">
      <a data-marker="item-title" href="{href}">{title}</a>
      <div data-marker="item-price">
        <span data-marker="item-price-value">{price}</span> ₽ в месяц
      </div>
      <div data-marker="item-specific-params">{details}</div>
      <a data-marker="item-address" href="#address">ул. Тестовая</a>
      <div data-marker="item-location">Новые Черёмушки ,  6–10 мин.</div>
      {published_html}
      <div data-marker="item-description">{description}</div>
    </div>
    """


def search_page(
    *cards: str,
    heading: str = "Аренда квартир на длительный срок в Москве без комиссии",
    selected_filters: tuple[str, ...] = ("Без комиссии", "Сначала новые"),
    recommendations: str = "",
) -> str:
    filters = "".join(
        f'<button data-marker="filter-active" aria-pressed="true">{value}</button>'
        for value in selected_filters
    )
    return f"""
    <!doctype html><html lang="ru"><head><title>Avito</title>
    <script>const captchaScript = 'captcha';</script></head><body>
    <h1>{heading}</h1>
    <nav>{filters}</nav>
    <main data-marker="catalog-serp">{"".join(cards)}</main>
    <aside data-marker="recommendations">{recommendations}</aside>
    </body></html>
    """


class FixtureBrowser:
    def __init__(self, pages: list[str | tuple[int, str]]) -> None:
        self.pages = pages
        self.requests = 0

    async def fetch(self, url: str) -> BrowserPage:
        self.requests += 1
        if url != SEARCH_URL:
            raise AssertionError(f"Unexpected fixture URL: {url}")

        fixture = self.pages[min(self.requests - 1, len(self.pages) - 1)]
        status, body = fixture if isinstance(fixture, tuple) else (200, fixture)
        return BrowserPage(
            status_code=status,
            final_url=url,
            html=body,
            observed_at=datetime.now(UTC),
            response_headers={"content-type": "text/html; charset=utf-8"},
        )


class FakeTelegramSender:
    def __init__(self) -> None:
        self.chat_ids: list[int] = []
        self.notifications = []

    async def send_notification(self, chat_id: int, notification) -> None:
        self.chat_ids.append(chat_id)
        self.notifications.append(notification)


class AvitoPipelineE2ETest(unittest.IsolatedAsyncioTestCase):
    async def test_single_403_schedules_recovery_instead_of_permanent_pause(self) -> None:
        repository = SQLiteRepository(":memory:")
        await repository.initialize()
        browser = FixtureBrowser([(403, "")])
        collector = AvitoCollector((SEARCH,))

        await run_source_once(
            CollectorRuntime(collector, browser, 60, jitter_seconds=0),
            SearchCriteria("Москва", 2, 70_000, True),
            repository,
            now=lambda: datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
        )
        state = await repository.get_source_run_state("avito")
        await repository.close()

        self.assertEqual(state.health, SourceRunHealth.COOLDOWN)
        self.assertIsNotNone(state.next_attempt_at)
        self.assertEqual(browser.requests, 1)

    async def test_captcha_state_stops_navigation_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            repository = SQLiteRepository(path)
            await repository.initialize()
            await repository.save_source_run_state(
                SourceRunState.manual_attention("avito", "captcha")
            )
            await repository.close()

            repository = SQLiteRepository(path)
            await repository.initialize()
            browser = FixtureBrowser([search_page(card("1111111111"))])
            transition = await run_source_once(
                CollectorRuntime(AvitoCollector((SEARCH,)), browser, 60),
                SearchCriteria("Москва", 2, 70_000, True),
                repository,
            )
            await repository.close()

        self.assertIsNone(transition)
        self.assertEqual(browser.requests, 0)

    async def test_existing_avito_baseline_survives_browser_collector_upgrade(self) -> None:
        criteria = SearchCriteria("Москва", 2, 70_000, True)
        source_id = "1111111111"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.sqlite3"
            repository = SQLiteRepository(path)
            await repository.initialize()
            await repository.record_baseline_candidates("avito", (source_id,))
            await repository.set_source_baseline("avito", complete=True)
            await repository.bind_chat_id(4242)
            await repository.close()

            repository = SQLiteRepository(path)
            await repository.initialize()
            collector = AvitoCollector((SEARCH,))
            result = await collector.collect(
                criteria,
                FixtureBrowser([search_page(card(source_id))]),
            )
            await process_collection_result(result, criteria, repository)
            delivered = await deliver_outbox_once(repository, FakeTelegramSender())
            await repository.close()

        self.assertEqual(result.source, "avito")
        self.assertEqual(delivered, 0)

    async def test_legacy_database_adds_evidence_columns_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE listings (
                    source TEXT NOT NULL, source_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    url TEXT NOT NULL, price_rub INTEGER, title TEXT, address TEXT,
                    address_norm TEXT, rooms INTEGER, area_m2 REAL, metro TEXT,
                    metro_minutes INTEGER, seller_type TEXT NOT NULL,
                    commission_status TEXT NOT NULL, commission_value INTEGER,
                    commission_unit TEXT, published_at TEXT, first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL, PRIMARY KEY (source, source_id)
                );
                """
            )
            connection.close()

            repository = SQLiteRepository(path)
            await repository.initialize()
            await repository.close()

            connection = sqlite3.connect(path)
            columns = {
                row[1]: row[4]
                for row in connection.execute("PRAGMA table_info(listings)").fetchall()
            }
            connection.close()

        self.assertEqual(columns["price_evidence"], "'unknown'")
        self.assertEqual(columns["rooms_evidence"], "'unknown'")
        self.assertEqual(columns["commission_evidence"], "'unknown'")

    async def test_new_matching_listing_reaches_telegram_once(self) -> None:
        criteria = SearchCriteria(
            city="Москва",
            rooms=2,
            max_monthly_price_rub=70_000,
            require_no_commission=True,
        )
        baseline_id = "1111111111"
        matching_id = "2222222222"
        over_price_id = "3333333333"
        commission_id = "4444444444"
        unknown_fee_id = "5555555555"
        baseline = card(baseline_id, price="60\u00a0000")
        fixtures = FixtureBrowser(
            [
                search_page(baseline),
                search_page(
                    baseline,
                    card(matching_id, price="70\u00a0000"),
                    card(over_price_id, price="70\u00a0001"),
                    card(commission_id, details="Залог 60 000 ₽ · Комиссия 50%"),
                    card(unknown_fee_id, details="Залог 60 000 ₽ · ЖКУ включены"),
                ),
                search_page(
                    baseline,
                    card(matching_id, price="70\u00a0000"),
                    card(over_price_id, price="70\u00a0001"),
                    card(commission_id, details="Залог 60 000 ₽ · Комиссия 50%"),
                    card(unknown_fee_id, details="Залог 60 000 ₽ · ЖКУ включены"),
                ),
            ]
        )
        repository = SQLiteRepository(":memory:")
        await repository.initialize()
        await repository.bind_chat_id(4242)
        clock = iter((0.0, 60.0, 120.0))
        collector = AvitoCollector((SEARCH,), monotonic=clock.__next__)
        notifier = FakeTelegramSender()

        try:
            first = await collector.collect(criteria, fixtures)
            self.assertEqual(first.status, SourceHealth.OK)
            await process_collection_result(first, criteria, repository)
            self.assertEqual(await deliver_outbox_once(repository, notifier), 0)

            second = await collector.collect(criteria, fixtures)
            await process_collection_result(second, criteria, repository)
            self.assertEqual(await deliver_outbox_once(repository, notifier), 2)
            self.assertEqual(notifier.chat_ids, [4242, 4242])
            self.assertEqual(len(notifier.notifications), 2)
            notification = notifier.notifications[0]
            self.assertEqual(notification.listing.source_id, matching_id)
            self.assertEqual(notification.listing.price_rub, 70_000)
            self.assertEqual(notification.listing.commission_status, "none")
            self.assertEqual(
                notification.listing.commission_evidence,
                FieldEvidence.EXPLICIT_CARD,
            )
            self.assertNotIn("описание карточки", str(notification.listing))

            duplicate = await collector.collect(criteria, fixtures)
            await process_collection_result(duplicate, criteria, repository)
            self.assertEqual(await deliver_outbox_once(repository, notifier), 0)
            self.assertEqual(len(notifier.notifications), 2)
            self.assertEqual(fixtures.requests, 3)
        finally:
            await repository.close()

    async def test_visible_captcha_can_recover_on_next_attempt(self) -> None:
        criteria = SearchCriteria("Москва", 2, 70_000, True)
        blocked_page = "<html><body><h1>Подтвердите, что вы не робот</h1></body></html>"
        fixtures = FixtureBrowser([blocked_page, search_page(card("9999999999"))])
        collector = AvitoCollector((SEARCH,))

        first = await collector.collect(criteria, fixtures)
        second = await collector.collect(criteria, fixtures)

        self.assertEqual(first.status, SourceHealth.PAUSED)
        self.assertEqual(first.failure_code, "captcha")
        self.assertEqual(second.status, SourceHealth.OK)
        self.assertEqual(fixtures.requests, 2)

    async def test_forbidden_response_pauses_and_honors_no_cookie_policy(self) -> None:
        criteria = SearchCriteria("Москва", 2, 70_000, True)
        fixtures = FixtureBrowser([(403, "")])
        collector = AvitoCollector((SEARCH,))

        result = await collector.collect(criteria, fixtures)

        self.assertEqual(result.status, SourceHealth.PAUSED)
        self.assertEqual(result.failure_code, "access_restricted")
        self.assertEqual(fixtures.requests, 1)


class AvitoParserTest(unittest.TestCase):
    def test_verified_no_commission_filter_supplies_missing_card_value(self) -> None:
        parsed = parse_search_page(
            search_page(
                card("5555555555", details="Залог 60 000 ₽ · ЖКУ включены"),
                selected_filters=("Без комиссии", "Сначала новые"),
            ),
            SEARCH_URL,
        )

        candidate = parsed.candidates[0]
        self.assertEqual(candidate.commission_status, CommissionStatus.NONE)
        self.assertEqual(candidate.commission_evidence, FieldEvidence.VERIFIED_FILTER)
        self.assertTrue(parsed.context.no_commission)
        self.assertTrue(parsed.context.newest_first)

    def test_url_without_visible_filter_does_not_supply_commission(self) -> None:
        parsed = parse_search_page(
            search_page(
                card("5555555555", details="Залог 60 000 ₽"),
                heading="Квартиры",
                selected_filters=(),
            ),
            SEARCH_URL,
        )

        self.assertEqual(parsed.candidates[0].commission_status, CommissionStatus.UNKNOWN)
        self.assertEqual(parsed.candidates[0].commission_evidence, FieldEvidence.UNKNOWN)

    def test_explicit_positive_commission_overrides_filter(self) -> None:
        parsed = parse_search_page(
            search_page(
                card("5555555555", details="Комиссия 50%"),
                selected_filters=("Без комиссии", "Сначала новые"),
            ),
            SEARCH_URL,
        )

        self.assertEqual(parsed.candidates[0].commission_status, CommissionStatus.POSITIVE)
        self.assertEqual(parsed.candidates[0].commission_evidence, FieldEvidence.EXPLICIT_CARD)

    def test_machine_timestamp_and_relative_timestamp_are_normalized(self) -> None:
        observed_at = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
        exact = parse_search_page(
            search_page(card("6666666666", published="2026-09-26T11:58:00+00:00")),
            SEARCH_URL,
            observed_at=observed_at,
        )
        relative = parse_search_page(
            search_page(card("7777777777", published="5 минут назад")),
            SEARCH_URL,
            observed_at=observed_at,
        )

        self.assertEqual(
            exact.candidates[0].published_at,
            datetime(2026, 9, 26, 11, 58, tzinfo=UTC),
        )
        self.assertEqual(relative.candidates[0].published_at, observed_at - timedelta(minutes=5))

    def test_recommendation_cards_outside_primary_results_are_ignored(self) -> None:
        parsed = parse_search_page(
            search_page(
                card("6666666666"),
                recommendations=card("7777777777"),
            ),
            SEARCH_URL,
        )

        self.assertEqual([item.source_id for item in parsed.candidates], ["6666666666"])

    def test_visible_card_fields_parse_without_reading_description_or_script(self) -> None:
        listing_id = "6666666666"
        parsed = parse_search_page(
            search_page(card(listing_id)),
            SEARCH_URL,
        )

        self.assertTrue(parsed.recognized)
        self.assertEqual(len(parsed.candidates), 1)
        candidate = parsed.candidates[0]
        self.assertEqual(candidate.source_id, listing_id)
        self.assertEqual(candidate.rooms, 2)
        self.assertEqual(candidate.price_rub, 65_000)
        self.assertEqual(candidate.commission_status, "none")
        self.assertEqual(candidate.address, "ул. Тестовая")
        self.assertEqual(candidate.metro, "Новые Черёмушки")
        self.assertEqual(candidate.metro_minutes, 10)
        self.assertNotIn("Не сохранять описание", repr(candidate))

    def test_non_moscow_cards_are_ignored(self) -> None:
        parsed = parse_search_page(
            search_page(
                card(
                    "7777777777",
                    path="/kotelniki/kvartiry/sdam/2-komnatnye/7777777777",
                )
            ),
            SEARCH_URL,
        )

        self.assertTrue(parsed.recognized)
        self.assertEqual(parsed.candidates, [])

    def test_unknown_markup_is_not_treated_as_an_empty_search(self) -> None:
        parsed = parse_search_page("<html><body>Some new page</body></html>", SEARCH_URL)
        self.assertFalse(parsed.recognized)
        self.assertEqual(parsed.candidates, [])

    def test_conflicting_no_commission_and_positive_fee_is_rejected(self) -> None:
        parsed = parse_search_page(
            search_page(
                card(
                    "8888888888",
                    details="Без комиссии · Комиссия агента 50%",
                )
            ),
            SEARCH_URL,
        )

        self.assertTrue(parsed.recognized)
        self.assertEqual(parsed.candidates[0].commission_status, "unknown")

    def test_malformed_listing_port_fails_closed(self) -> None:
        parsed = parse_search_page(
            search_page(
                card(
                    "9999999999",
                    path="https://www.avito.ru:invalid/moskva/kvartiry/sdam/2-komnatnye/9999999999",
                )
            ),
            SEARCH_URL,
        )

        self.assertFalse(parsed.recognized)
        self.assertEqual(parsed.candidates, [])


if __name__ == "__main__":
    unittest.main()
