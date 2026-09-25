"""End-to-end Avito HTML fixture → filter → SQLite → Telegram pipeline tests."""

from __future__ import annotations

import unittest
from urllib.parse import urlsplit

import httpx

from rent_monitor.collectors.avito import SEARCH_URL, AvitoCollector
from rent_monitor.core.models import SearchCriteria, SourceHealth
from rent_monitor.core.scheduler import deliver_outbox_once, process_collection_result
from rent_monitor.parsers.avito import parse_search_page
from rent_monitor.storage.sqlite import SQLiteRepository
from rent_monitor.transport import BoundedHttpClient


def card(
    source_id: str,
    *,
    price: str = "65\u00a0000",
    details: str = "Залог 65 000 ₽ · Без комиссии · ЖКУ включены",
    title: str = "2-к. квартира, 48 м², 3/9 эт.",
    path: str | None = None,
    description: str = "Не сохранять описание карточки или контакты продавца.",
) -> str:
    href = path or f"/moskva/kvartiry/sdam/2-komnatnye/{source_id}"
    return f"""
    <div data-marker="item" data-item-id="{source_id}">
      <a data-marker="item-title" href="{href}">{title}</a>
      <div data-marker="item-price">
        <span data-marker="item-price-value">{price}</span> ₽ в месяц
      </div>
      <div data-marker="item-specific-params">{details}</div>
      <a data-marker="item-address" href="#address">ул. Тестовая</a>
      <div data-marker="item-location">Новые Черёмушки ,  6–10 мин.</div>
      <div data-marker="item-description">{description}</div>
    </div>
    """


def search_page(*cards: str) -> str:
    return f"""
    <!doctype html><html lang="ru"><head><title>Avito</title>
    <script>const captchaScript = 'captcha';</script></head><body>
    <h1>Аренда квартир на длительный срок в Москве без комиссии</h1>
    <main data-marker="catalog-serp">{"".join(cards)}</main>
    </body></html>
    """


class FixtureTransport:
    def __init__(self, pages: list[str | tuple[int, str]]) -> None:
        self.pages = pages
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if request.url.host != "www.avito.ru" and request.url.host != "avito.ru":
            raise AssertionError(f"Unexpected fixture host: {request.url.host}")
        if request.url.path != urlsplit(SEARCH_URL).path:
            raise AssertionError(f"Unexpected fixture path: {request.url.path}")
        if request.url.query != b"s=104":
            raise AssertionError(f"Unexpected search parameters: {request.url.query}")
        if "cookie" in request.headers:
            raise AssertionError("Public-page requests must not send cookies")

        fixture = self.pages[min(self.requests - 1, len(self.pages) - 1)]
        status, body = fixture if isinstance(fixture, tuple) else (200, fixture)
        return httpx.Response(
            status,
            text=body,
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )


class FakeTelegramSender:
    def __init__(self) -> None:
        self.chat_ids: list[int] = []
        self.notifications = []

    async def send_notification(self, chat_id: int, notification) -> None:
        self.chat_ids.append(chat_id)
        self.notifications.append(notification)


class AvitoPipelineE2ETest(unittest.IsolatedAsyncioTestCase):
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
        fixtures = FixtureTransport(
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
        collector = AvitoCollector()
        notifier = FakeTelegramSender()

        try:
            async with BoundedHttpClient(transport=httpx.MockTransport(fixtures)) as client:
                first = await collector.collect(criteria, client)
                self.assertEqual(first.status, SourceHealth.OK)
                await process_collection_result(first, criteria, repository)
                self.assertEqual(await deliver_outbox_once(repository, notifier), 0)

                second = await collector.collect(criteria, client)
                await process_collection_result(second, criteria, repository)
                self.assertEqual(await deliver_outbox_once(repository, notifier), 1)
                self.assertEqual(notifier.chat_ids, [4242])
                self.assertEqual(len(notifier.notifications), 1)
                notification = notifier.notifications[0]
                self.assertEqual(notification.listing.source_id, matching_id)
                self.assertEqual(notification.listing.price_rub, 70_000)
                self.assertEqual(notification.listing.commission_status, "none")
                self.assertNotIn("описание карточки", str(notification.listing))

                duplicate = await collector.collect(criteria, client)
                await process_collection_result(duplicate, criteria, repository)
                self.assertEqual(await deliver_outbox_once(repository, notifier), 0)
                self.assertEqual(len(notifier.notifications), 1)
                self.assertEqual(fixtures.requests, 3)
        finally:
            await repository.close()

    async def test_visible_captcha_pauses_without_retrying_source(self) -> None:
        criteria = SearchCriteria("Москва", 2, 70_000, True)
        blocked_page = "<html><body><h1>Подтвердите, что вы не робот</h1></body></html>"
        fixtures = FixtureTransport([blocked_page, search_page(card("9999999999"))])
        collector = AvitoCollector()

        async with BoundedHttpClient(transport=httpx.MockTransport(fixtures)) as client:
            first = await collector.collect(criteria, client)
            second = await collector.collect(criteria, client)

        self.assertEqual(first.status, SourceHealth.PAUSED)
        self.assertEqual(first.failure_code, "captcha")
        self.assertEqual(second.status, SourceHealth.PAUSED)
        self.assertEqual(fixtures.requests, 1)

    async def test_forbidden_response_pauses_and_honors_no_cookie_policy(self) -> None:
        criteria = SearchCriteria("Москва", 2, 70_000, True)
        fixtures = FixtureTransport([(403, "")])
        collector = AvitoCollector()

        async with BoundedHttpClient(transport=httpx.MockTransport(fixtures)) as client:
            result = await collector.collect(criteria, client)

        self.assertEqual(result.status, SourceHealth.PAUSED)
        self.assertEqual(result.failure_code, "access_restricted")
        self.assertEqual(fixtures.requests, 1)


class AvitoParserTest(unittest.TestCase):
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
