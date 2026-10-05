"""Send one rollout verification to the existing bound owner."""

import asyncio
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from rent_monitor.storage.sqlite import SQLiteRepository


async def main():
    repository = SQLiteRepository(Path("/var/lib/rent-monitor/rent-monitor.sqlite3"))
    await repository.initialize()
    try:
        chat_id = await repository.get_allowed_chat_id()
        if chat_id is None:
            raise RuntimeError("No owner binding")
        async with Bot(
            token=Path("/etc/rent-monitor/telegram_bot_token").read_text().strip()
        ) as bot:
            await bot.send_message(
                chat_id,
                "Доступ к окну Avito настроен. Включите Tailscale на телефоне и нажмите "
                "кнопку ниже — бот пришлёт ссылку на 15 минут. "
                "Получить новую ссылку можно командой /avito. "
                "Avito пока ограничивает IP сервера. Открытие окна не снимает этот блок.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Получить ссылку на Avito", callback_data="captcha:open:avito"
                            )
                        ]
                    ]
                ),
            )
            print("telegram_rollout_message_delivered")
    finally:
        await repository.close()


asyncio.run(main())
