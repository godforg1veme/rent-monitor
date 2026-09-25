"""End-to-end smoke test using local HTML fixtures and a fake Telegram sender."""

from __future__ import annotations

import json
import unittest

import httpx

from rent_monitor.collectors.yandex import SEARCH_URL, YandexCollector
from rent_monitor.core.models import SearchCriteria
from rent_monitor.core.scheduler import deliver_outbox_once, process_collection_result
from rent_monitor.storage.sqlite import SQLiteRepository
from rent_monitor.transport import BoundedHttpClient

BASELINE_ID = "100000001"
MATCHING_ID = "100000002"
UNKNOWN_FEE_ID = "100000003"
OVER_BUDGET_ID = "100000004"
WRONG_ROOMS_ID = "100000005"


def offer(
    offer_id: str,
    *,
    price: int = 65_000,
    rooms: int = 2,
    commission: int | None = 0,
) -> dict[str, object]:
    item: dict[str, object] = {
        "offerId": offer_id,
        "url": f"https://realty.yandex.ru/offer/{offer_id}/",
        "price": {"currency": "RUB", "period": "MONTH", "value": price},
        "roomsTotal": rooms,
        "area": 51,
        "address": "Москва, улица Примерная, 12",
        "metro": {"name": "Тестовая", "minutes": 8},
    }
    if commission is not None:
        item["agentFee"] = commission
    return item


def search_page(offers: list[dict[str, object]]) -> str:
    payload = json.dumps({"offers": offers}, ensure_ascii=False)
    return (
        '<!doctype html><html><body><script id="initial_state_script">'
        f"window.INITIAL_STATE = {payload};"
        "</script></body></html>"
    )


class FixtureTransport:
    """Serve predefined public-page HTML through HTTPX without network access."""

    def __init__(self) -> None:
        self.search_pages = [
            search_page([offer(BASELINE_ID)]),
            search_page(
                [
                    offer(BASELINE_ID),
                    offer(MATCHING_ID),
                    offer(UNKNOWN_FEE_ID, commission=None),
                    offer(OVER_BUDGET_ID, price=70_001),
                    offer(WRONG_ROOMS_ID, rooms=1),
                ]
            ),
        ]
        self.search_requests = 0
        self.detail_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "realty.yandex.ru":
            raise AssertionError(f"Unexpected fixture host: {request.url.host}")
        if "cookie" in request.headers:
            raise AssertionError("Cookies must not be sent between public-page requests")
        if request.url.path == httpx.URL(SEARCH_URL).path:
            index = min(self.search_requests, len(self.search_pages) - 1)
            self.search_requests += 1
            body = self.search_pages[index]
        elif request.url.path.startswith("/offer/"):
            self.detail_requests += 1
            body = search_page([offer(UNKNOWN_FEE_ID, commission=None)])
        else:
            raise AssertionError(f"Unexpected fixture path: {request.url.path}")
        return httpx.Response(
            200,
            text=body,
            headers={
                "content-type": "text/html; charset=utf-8",
                "set-cookie": "fixture_session=must-not-be-reused; Path=/",
            },
            request=request,
        )


class FakeTelegramSender:
    def __init__(self) -> None:
        self.chat_ids: list[int] = []
        self.notifications = []

    async def send_notification(self, chat_id: int, notification) -> None:
        self.chat_ids.append(chat_id)
        self.notifications.append(notification)


class MonitorPipelineE2ETest(unittest.IsolatedAsyncioTestCase):
    async def test_new_matching_listing_reaches_fake_telegram_once(self) -> None:
        criteria = SearchCriteria(
            city="Москва",
            rooms=2,
            max_monthly_price_rub=70_000,
            require_no_commission=True,
        )
        fixtures = FixtureTransport()
        repository = SQLiteRepository(":memory:")
        await repository.initialize()
        await repository.bind_chat_id(4242)
        collector = YandexCollector()
        notifier = FakeTelegramSender()

        try:
            async with BoundedHttpClient(transport=httpx.MockTransport(fixtures)) as client:
                baseline = await collector.collect(criteria, client)
                await process_collection_result(baseline, criteria, repository)
                self.assertTrue(await repository.has_source_baseline("yandex"))
                self.assertEqual(await deliver_outbox_once(repository, notifier), 0)

                new_results = await collector.collect(criteria, client)
                self.assertEqual(fixtures.search_requests, 2)
                self.assertEqual(fixtures.detail_requests, 1)
                await process_collection_result(new_results, criteria, repository)

                delivered = await deliver_outbox_once(repository, notifier)
                self.assertEqual(delivered, 1)
                self.assertEqual(notifier.chat_ids, [4242])
                self.assertEqual(len(notifier.notifications), 1)
                self.assertEqual(notifier.notifications[0].listing.source_id, MATCHING_ID)
                self.assertEqual(notifier.notifications[0].listing.price_rub, 65_000)

                # Reprocessing the same source page cannot create another message.
                await process_collection_result(new_results, criteria, repository)
                self.assertEqual(await deliver_outbox_once(repository, notifier), 0)
                self.assertEqual(len(notifier.notifications), 1)
        finally:
            await repository.close()


if __name__ == "__main__":
    unittest.main()
