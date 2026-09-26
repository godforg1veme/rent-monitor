from __future__ import annotations

import asyncio
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime

from rent_monitor.browser.transport import BrowserPage
from rent_monitor.collectors.avito import AvitoCollector
from rent_monitor.config import AvitoSearchConfig
from rent_monitor.core.models import SearchCriteria, SourceHealth

URL_ONE = (
    "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok/"
    "bez_komissii-ASgBAgICA0SSA8gQ8AeQUp74DgI?s=104"
)
URL_TWO = URL_ONE + "&district=1"
SEARCH_ONE = AvitoSearchConfig("main", URL_ONE, 60)
SEARCH_TWO = AvitoSearchConfig("district", URL_TWO, 120)
CRITERIA = SearchCriteria("Москва", 2, 70_000, True)


def card(source_id: str) -> str:
    return f"""
    <div data-marker="item" data-item-id="{source_id}">
      <a data-marker="item-title"
         href="/moskva/kvartiry/sdam/2-komnatnye/{source_id}">
        2-к. квартира, 48 м²
      </a>
      <span data-marker="item-price-value">65 000</span>
      <div data-marker="item-specific-params">Без комиссии</div>
    </div>
    """


def search_page(*cards: str) -> str:
    return f"""
    <html><body>
      <h1>Аренда квартир на длительный срок в Москве без комиссии</h1>
      <button data-marker="filter-active" aria-pressed="true">Без комиссии</button>
      <button data-marker="filter-active" aria-pressed="true">Сначала новые</button>
      <main data-marker="catalog-serp">{"".join(cards)}</main>
    </body></html>
    """


def page(
    html: str,
    url: str,
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
) -> BrowserPage:
    return BrowserPage(
        status_code=status,
        final_url=url,
        html=html,
        observed_at=datetime(2026, 9, 26, tzinfo=UTC),
        response_headers=headers or {},
    )


class FakeBrowserTransport:
    def __init__(self, responses: Mapping[str, BrowserPage | Exception | list[object]]) -> None:
        self.responses = dict(responses)
        self.requested_urls: list[str] = []
        self.active_fetches = 0
        self.max_concurrent_fetches = 0

    async def fetch(self, url: str) -> BrowserPage:
        self.requested_urls.append(url)
        self.active_fetches += 1
        self.max_concurrent_fetches = max(self.max_concurrent_fetches, self.active_fetches)
        try:
            await asyncio.sleep(0)
            response = self.responses[url]
            if isinstance(response, list):
                response = response.pop(0)
            if isinstance(response, Exception):
                raise response
            assert isinstance(response, BrowserPage)
            return response
        finally:
            self.active_fetches -= 1


class AvitoCollectorTest(unittest.IsolatedAsyncioTestCase):
    async def test_not_due_cycle_preserves_seen_ids_without_navigation(self) -> None:
        now = 100.0
        browser = FakeBrowserTransport({URL_ONE: page(search_page(card("1111111111")), URL_ONE)})
        collector = AvitoCollector((SEARCH_ONE,), monotonic=lambda: now)

        first = await collector.collect(CRITERIA, browser)
        second = await collector.collect(CRITERIA, browser)

        self.assertEqual(first.seen_source_ids, ("1111111111",))
        self.assertEqual(second.seen_source_ids, first.seen_source_ids)
        self.assertEqual(second.listings, ())
        self.assertEqual(browser.requested_urls, [URL_ONE])

    async def test_collects_all_jobs_sequentially_and_unions_ids(self) -> None:
        browser = FakeBrowserTransport(
            {
                URL_ONE: page(search_page(card("1111111111")), URL_ONE),
                URL_TWO: page(search_page(card("2222222222")), URL_TWO),
            }
        )
        collector = AvitoCollector((SEARCH_ONE, SEARCH_TWO))

        result = await collector.collect(CRITERIA, browser)

        self.assertEqual(result.status, SourceHealth.OK)
        self.assertEqual(result.seen_source_ids, ("1111111111", "2222222222"))
        self.assertEqual(browser.max_concurrent_fetches, 1)
        self.assertEqual(browser.requested_urls, [URL_ONE, URL_TWO])

    async def test_captcha_aborts_remaining_jobs_without_permanent_latch(self) -> None:
        captcha = "<html><body>Подтвердите, что вы не робот</body></html>"
        browser = FakeBrowserTransport(
            {
                URL_ONE: [
                    page(captcha, URL_ONE),
                    page(search_page(card("1111111111")), URL_ONE),
                ],
                URL_TWO: page(search_page(card("2222222222")), URL_TWO),
            }
        )
        collector = AvitoCollector((SEARCH_ONE, SEARCH_TWO))

        first = await collector.collect(CRITERIA, browser)
        second = await collector.collect(CRITERIA, browser)

        self.assertEqual(first.status, SourceHealth.PAUSED)
        self.assertEqual(first.failure_code, "captcha")
        self.assertEqual(second.status, SourceHealth.OK)
        self.assertEqual(browser.requested_urls, [URL_ONE, URL_ONE, URL_TWO])
        self.assertFalse(hasattr(collector, "_paused_reason"))

    async def test_classifies_http_and_transport_failures(self) -> None:
        cases = (
            (page("", URL_ONE, status=403), SourceHealth.PAUSED, "access_restricted", None),
            (
                page("", URL_ONE, status=429, headers={"retry-after": "90"}),
                SourceHealth.DEGRADED,
                "rate_limited",
                90.0,
            ),
            (RuntimeError("browser crashed"), SourceHealth.ERROR, "transport_error", None),
            (
                page("<html>changed</html>", URL_ONE),
                SourceHealth.DEGRADED,
                "unrecognized_structure",
                None,
            ),
        )
        for response, health, code, retry_after in cases:
            with self.subTest(code=code):
                browser = FakeBrowserTransport({URL_ONE: response})
                result = await AvitoCollector((SEARCH_ONE,)).collect(CRITERIA, browser)
                self.assertEqual(result.status, health)
                self.assertEqual(result.failure_code, code)
                self.assertEqual(result.retry_after_seconds, retry_after)


if __name__ == "__main__":
    unittest.main()
