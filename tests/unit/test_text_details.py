from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from rent_monitor.browser.transport import BrowserPage
from rent_monitor.collectors.avito import AvitoCollector
from rent_monitor.config import AvitoSearchConfig
from rent_monitor.core.models import Candidate, SearchCriteria, SourceHealth
from rent_monitor.core.scheduler import process_collection_result
from rent_monitor.parsers.avito_detail import parse_detail
from rent_monitor.storage.sqlite import _SCHEMA, SQLiteRepository
from rent_monitor.telegram.bot import format_notification, full_listing_text

DETAIL = """<html><body>
<span data-marker="item-view/item-id">№ 1234567890</span>
<div data-marker="item-view/item-description"><p>Полный текст &amp; условия</p>
<p>Можно с детьми.</p><script>secret</script></div>
<div data-marker="item-view/item-params"><h2>Условия</h2><li>Комиссия: 0 %</li>
<li>Залог: 60000 ₽</li></div>
<div data-marker="item-view/item-date">сегодня в 12:00</div>
<div data-marker="item-view/seller-info"><a data-marker="seller-link/link"
href="/user/test/profile">Анна</a><span data-marker="seller-info/name">Анна</span>
<span data-marker="seller-info/label">Частное лицо</span></div></body></html>"""
URL = "https://www.avito.ru/moskva/kvartiry/flat_1234567890"


class DetailTests(unittest.TestCase):
    def test_identity_and_complete_description(self):
        self.assertIsNone(parse_detail(DETAIL, "9999999999", URL))
        details = parse_detail(DETAIL, "1234567890", URL)
        self.assertEqual(details["description"], "Полный текст & условия\nМожно с детьми.")
        self.assertEqual(details["characteristics"]["Комиссия"], "0 %")
        self.assertEqual(details["seller_url"], "https://www.avito.ru/user/test/profile")
        self.assertNotIn("seller_rating", details)

    def test_description_and_seller_are_escaped_and_long_text_preserved(self):
        details = parse_detail(DETAIL, "1234567890", URL)
        details["description"] = "<b>публичный текст</b>" * 1000
        details["seller_name"] = "<script>"
        listing = replace(
            Candidate(
                "avito", "1234567890", URL, title="Квартира", rooms=2, price_rub=65000
            ).to_listing(),
            details=details,
        )
        message = format_notification(listing)
        self.assertLessEqual(len(message), 4096)
        self.assertIn("&lt;script&gt;", message)
        self.assertIn(details["description"], full_listing_text(listing))


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_listing_details_cached_and_notification_not_duplicated(self):
        search_url = (
            "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok/bez_komissii-test?s=104"
        )
        search_html = """<h1>Аренда квартир на длительный срок в Москве без комиссии</h1>
        <main data-marker="catalog-serp"><div data-marker="item" data-item-id="1234567890">
        <a data-marker="item-title" href="/moskva/kvartiry/flat_1234567890">2-к. квартира, 48 м²</a>
        <span data-marker="item-price-value">65000</span>
        <div data-marker="item-specific-params">Без комиссии</div></div></main>"""
        requested = []

        class Browser:
            async def fetch(self, url):
                requested.append(url)
                return BrowserPage(
                    200, url, search_html if url == search_url else DETAIL, datetime.now(UTC)
                )

        with tempfile.TemporaryDirectory() as temp:
            repo = SQLiteRepository(Path(temp) / "test.sqlite3")
            await repo.initialize()
            await repo.set_source_baseline("avito", complete=True)
            now = [0]
            collector = AvitoCollector(
                (AvitoSearchConfig("test", search_url, 60),), monotonic=lambda: now[0]
            )
            collector.repository = repo
            collector.detail_cache_directory = Path(temp) / "cache"
            criteria = SearchCriteria("Москва", 2, 70000, True)
            with (
                patch.dict(os.environ, {"RENT_MONITOR_COLLECT_DETAILS": "1"}),
                patch("rent_monitor.collectors.avito.asyncio.sleep"),
            ):
                result = await collector.collect(criteria, Browser())
                self.assertEqual(result.status, SourceHealth.OK)
                self.assertEqual(result.listings[0].details["seller_name"], "Анна")
                await process_collection_result(result, criteria, repo)
                now[0] = 61
                result = await collector.collect(criteria, Browser())
                await process_collection_result(result, criteria, repo)
            self.assertEqual(requested.count(URL), 1)
            await repo.close()

    async def test_legacy_migration_roundtrip_and_single_outbox(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(_SCHEMA.replace("    details_json TEXT,\n", ""))
            connection.close()
            repo = SQLiteRepository(path)
            await repo.initialize()
            details = parse_detail(DETAIL, "1234567890", URL)
            listing = replace(
                Candidate("avito", "1234567890", URL, rooms=2, price_rub=65000).to_listing(),
                details=details,
            )
            await repo.upsert_listing(listing, notify=True)
            await repo.upsert_listing(listing, notify=True)
            restored = await repo.get_listing("avito", "1234567890")
            self.assertEqual(restored.details, details)
            await repo.close()
            connection = sqlite3.connect(path)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM notification_outbox").fetchone()[0], 1
            )
            connection.close()


if __name__ == "__main__":
    unittest.main()
