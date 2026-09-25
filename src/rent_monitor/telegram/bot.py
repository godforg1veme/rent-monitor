"""Private Telegram bot for listing delivery and owner controls."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from rent_monitor.core.models import Listing, Notification
from rent_monitor.storage.sqlite import SQLiteRepository
from rent_monitor.transport import ALLOWED_HOSTS

logger = logging.getLogger(__name__)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


class TelegramNotifier:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_notification(self, chat_id: int, notification: Notification) -> None:
        listings = (notification.listing, *notification.alternatives)
        buttons = []
        for item in listings:
            safe_url = _safe_url(item)
            if safe_url is not None:
                buttons.append(
                    [InlineKeyboardButton(text=f"Открыть на {item.source}", url=safe_url)]
                )
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await self.bot.send_message(
            chat_id=chat_id,
            text=format_notification(notification.listing, notification.alternatives),
            reply_markup=keyboard if keyboard.inline_keyboard else None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


def create_dispatcher(
    repository: SQLiteRepository,
    state_changed: asyncio.Event,
) -> Dispatcher:
    router = Router(name="rent-monitor-owner")

    @router.message(CommandStart(), F.chat.type == "private")
    async def start(message: Message, command: CommandObject) -> None:
        chat_id = message.chat.id
        allowed_chat_id = await repository.get_allowed_chat_id()
        if allowed_chat_id == chat_id:
            await message.answer("Чат уже привязан. Команды: /status, /pause, /resume")
            return
        if allowed_chat_id is not None:
            await message.answer("Бот уже привязан к личному чату владельца.")
            return
        if not command.args:
            await message.answer(
                "Для привязки отправьте /start и одноразовый код из консоли сервера."
            )
            return
        if await repository.consume_pairing_code(command.args.strip(), chat_id):
            await message.answer("Личный чат привязан. Новые объявления будут приходить сюда.")
        else:
            await message.answer(
                "Код недействителен или уже истёк. Выпустите новый код на сервере."
            )

    async def require_owner(message: Message) -> bool:
        if message.chat.type != "private":
            return False
        return await repository.get_allowed_chat_id() == message.chat.id

    @router.message(Command("status"), F.chat.type == "private")
    async def status(message: Message) -> None:
        if not await require_owner(message):
            return
        source_statuses = await repository.get_source_statuses()
        known = {record.source: record for record in source_statuses}
        lines = ["<b>Состояние поиска</b>"]
        for source in ("avito", "cian", "domclick", "yandex"):
            record = known.get(source)
            if record is None:
                lines.append(f"{html.escape(_source_label(source))}: ещё не проверялся")
                continue
            last_success = _format_time(record.last_success_at)
            failure = (
                f", причина: {html.escape(record.failure_code)}" if record.failure_code else ""
            )
            lines.append(
                f"{html.escape(_source_label(source))}: {html.escape(record.health.value)}, "
                f"последняя удачная проверка: {last_success}{failure}"
            )
        paused = await repository.is_paused()
        lines.append(f"Опрос: {'приостановлен' if paused else 'включён'}")
        await message.answer("\n".join(lines))

    @router.message(Command("pause"), F.chat.type == "private")
    async def pause(message: Message) -> None:
        if not await require_owner(message):
            return
        await repository.set_paused(True)
        state_changed.set()
        await message.answer("Опрос приостановлен.")

    @router.message(Command("resume"), F.chat.type == "private")
    async def resume(message: Message) -> None:
        if not await require_owner(message):
            return
        await repository.set_paused(False)
        state_changed.set()
        await message.answer("Опрос продолжен.")

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


def format_notification(listing: Listing, alternatives: tuple[Listing, ...] = ()) -> str:
    lines = [f"<b>{listing.rooms or 'Комнаты не указаны'}-комнатная квартира</b>"]
    if listing.price_rub is not None:
        lines.append(f"Аренда: <b>{listing.price_rub:,} ₽/мес.</b>".replace(",", " "))
    if listing.address:
        lines.append(f"Адрес: {html.escape(listing.address)}")
    if listing.metro:
        metro_suffix = (
            f", {listing.metro_minutes} мин." if listing.metro_minutes is not None else ""
        )
        lines.append(f"Метро: {html.escape(listing.metro)}{html.escape(metro_suffix)}")
    lines.append("Комиссия: без комиссии")

    sources = [listing.source, *(item.source for item in alternatives)]
    lines.append(
        "Источник: "
        + ", ".join(html.escape(_source_label(source)) for source in dict.fromkeys(sources))
    )
    return "\n".join(lines)


def _safe_url(listing: Listing) -> str | None:
    parsed = urlsplit(listing.url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not any(
        host == domain or host.endswith(f".{domain}") for domain in ALLOWED_HOSTS
    ):
        logger.warning("telegram phase=format status=skip_link source=%s", listing.source)
        return None
    return listing.url


def _source_label(source: str) -> str:
    return {
        "avito": "Avito",
        "cian": "Циан",
        "domclick": "Домклик",
        "yandex": "Яндекс Недвижимость",
    }.get(source, source)


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "нет данных"
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M МСК")
