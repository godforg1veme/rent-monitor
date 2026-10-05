"""Send one explicitly labelled existing-listing test to the bound owner."""

import asyncio
import sqlite3
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from rent_monitor.config import load_config
from rent_monitor.core.filters import matches_listing
from rent_monitor.storage.sqlite import _listing_from_row
from rent_monitor.telegram.bot import _safe_url, format_notification


async def main():
    config = load_config(Path("/opt/rent-monitor/current/config/search.toml"))
    with sqlite3.connect("file:/var/lib/rent-monitor/rent-monitor.sqlite3?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        state = db.execute("SELECT health FROM source_runtime WHERE source='avito'").fetchone()
        assert state and state[0] == "healthy"
        baseline = db.execute(
            "SELECT complete FROM source_baselines WHERE source='avito'"
        ).fetchone()
        assert baseline and baseline[0] == 1
        owner = db.execute("SELECT allowed_chat_id FROM owner_binding WHERE singleton=1").fetchone()
        assert owner
        listing = next(
            item
            for row in db.execute("SELECT * FROM listings WHERE source='avito'")
            if matches_listing(item := _listing_from_row(row), config.criteria) and _safe_url(item)
        )
    bot = Bot(Path("/etc/rent-monitor/telegram_bot_token").read_text().strip())
    try:
        await bot.send_message(
            owner[0],
            "Проверка доставки Avito: это существующее объявление, не новое.\n"
            "Монитор работает автономно на немецком VPS, опрос примерно раз в минуту.\n\n"
            + format_notification(listing),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Открыть на Avito", url=_safe_url(listing))]
                ]
            ),
        )
        print("Telegram matching-listing test delivered; production outbox unchanged")
    finally:
        await bot.session.close()


asyncio.run(main())
