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
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from rent_monitor.browser.captcha import CaptchaSessionManager
from rent_monitor.browser.transport import BrowserTransport
from rent_monitor.core.models import Listing, Notification, SearchCriteria, SourceAlert
from rent_monitor.core.scheduler import check_manual_attention_source
from rent_monitor.core.source_state import SourceRunHealth
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
        if notification.listing.details and notification.listing.details.get("description"):
            from rent_monitor.telegram.detail_format import format_messages

            messages = format_messages(notification.listing, notification.listing.details, "new")
            for index, text in enumerate(messages):
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard if index == len(messages) - 1 else None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            return
        await self.bot.send_message(
            chat_id=chat_id,
            text=format_notification(notification.listing, notification.alternatives),
            reply_markup=keyboard if keyboard.inline_keyboard else None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        details = notification.listing.details or {}
        if details.get("description"):
            document = full_listing_text(notification.listing)
            if (
                len(format_notification(notification.listing, notification.alternatives))
                + len(html.escape(details["description"]))
                > 3800
            ):
                await self.bot.send_document(
                    chat_id=chat_id,
                    document=BufferedInputFile(
                        document.encode("utf-8"),
                        filename=f"avito-{notification.listing.source_id}.txt",
                    ),
                    caption="Полный текст и характеристики объявления",
                )

    async def send_source_alert(self, chat_id: int, alert: SourceAlert) -> None:
        keyboard = None
        if (
            alert.source == "avito"
            and alert.health == "manual_attention"
            and alert.failure_code in {"captcha", "human_verification"}
        ):
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Открыть CAPTCHA",
                            callback_data=f"captcha:open:{alert.source}",
                        ),
                        InlineKeyboardButton(
                            text="Проверить",
                            callback_data=f"captcha:check:{alert.source}",
                        ),
                    ]
                ]
            )
        await self.bot.send_message(
            chat_id=chat_id,
            text=format_source_alert(alert),
            reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


def create_dispatcher(
    repository: SQLiteRepository,
    state_changed: asyncio.Event,
    *,
    browser: BrowserTransport | None = None,
    captcha_manager: CaptchaSessionManager | None = None,
    criteria: SearchCriteria | None = None,
    avito_interval_seconds: float = 60.0,
    avito_search_url: str | None = None,
) -> Dispatcher:
    router = Router(name="rent-monitor-owner")

    @router.message(CommandStart(), F.chat.type == "private")
    async def start(message: Message, command: CommandObject) -> None:
        chat_id = message.chat.id
        allowed_chat_id = await repository.get_allowed_chat_id()
        if allowed_chat_id == chat_id:
            await message.answer("Чат уже привязан. Команды: /status, /avito, /pause, /resume")
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

    async def require_owner_callback(callback: CallbackQuery) -> bool:
        message = callback.message
        if message is None or message.chat.type != "private":
            return False
        return await repository.get_allowed_chat_id() == message.chat.id

    async def prepare_avito_window(message: Message) -> None:
        if browser is None:
            return
        page = await browser.current_page()
        if avito_search_url and (page is None or page.final_url == "about:blank"):
            await message.answer("Открываю настроенный поиск Avito один раз для ручной проверки…")
            try:
                page = await browser.fetch(avito_search_url)
            except Exception:
                await message.answer(
                    "Страница не загрузилась. Окно браузера доступно по ссылке ниже."
                )
                return
        if page is not None:
            if page.status_code == 403 or "проблема с ip" in page.html.lower():
                await message.answer(
                    "Avito ограничивает IP сервера. Это не CAPTCHA: подтверждать нечего. "
                    "Автоматическая пауза сохранена; окно ниже показывает ответ Avito."
                )

    @router.message(Command("avito"), F.chat.type == "private")
    async def avito_window(message: Message) -> None:
        if not await require_owner(message):
            return
        if browser is None or captcha_manager is None:
            await message.answer("Удалённый доступ к Avito не настроен.")
            return
        await prepare_avito_window(message)
        session = await captcha_manager.issue("avito")
        await message.answer(
            "Текущее окно Avito. Включите Tailscale на телефоне. Ссылка действует 15 минут. "
            "Открытие окна не снимает паузу при ограничении IP.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть окно Avito", url=session.url)]]
            ),
        )

    @router.callback_query(F.data == "captcha:open:avito")
    async def open_captcha(callback: CallbackQuery) -> None:
        if not await require_owner_callback(callback):
            await callback.answer("Недоступно", show_alert=True)
            return
        if browser is None or captcha_manager is None or callback.message is None:
            await callback.answer("Удалённый доступ не настроен", show_alert=True)
            return
        state = await repository.get_source_run_state("avito")
        if state.health not in {
            SourceRunHealth.MANUAL_ATTENTION,
            SourceRunHealth.COOLDOWN,
            SourceRunHealth.BLOCKED,
            SourceRunHealth.DEGRADED,
        }:
            await callback.answer("CAPTCHA уже не ожидается", show_alert=True)
            return
        await callback.answer("Открываю окно Avito")
        await prepare_avito_window(callback.message)
        session = await captcha_manager.issue("avito")
        screenshot = await browser.screenshot()
        if screenshot:
            await callback.message.answer_photo(
                BufferedInputFile(screenshot, filename="avito-captcha.png"),
                caption="Текущее окно Avito перед подключением.",
            )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Открыть окно Avito", url=session.url)]]
        )
        await callback.message.answer(
            "Ссылка доступна 15 минут и работает только через ваш Tailscale.",
            reply_markup=keyboard,
        )
        await callback.answer()

    @router.callback_query(F.data == "captcha:check:avito")
    async def check_captcha(callback: CallbackQuery) -> None:
        if not await require_owner_callback(callback):
            await callback.answer("Недоступно", show_alert=True)
            return
        if browser is None or criteria is None:
            await callback.answer("Проверка не настроена", show_alert=True)
            return
        resumed = await check_manual_attention_source(
            "avito",
            browser,
            repository,
            criteria,
            normal_interval_seconds=avito_interval_seconds,
        )
        if not resumed:
            await callback.answer("CAPTCHA ещё видна или страница не распознана", show_alert=True)
            return
        if captcha_manager is not None:
            await captcha_manager.expire_all()
        state_changed.set()
        await callback.answer("Avito проверен, опрос возобновлён", show_alert=True)

    @router.message(Command("status"), F.chat.type == "private")
    async def status(message: Message) -> None:
        if not await require_owner(message):
            return
        runtime_states = await repository.list_source_run_states()
        runtime_known = {record.source: record for record in runtime_states}
        source_statuses = await repository.get_source_statuses() if not runtime_states else []
        legacy_known = {record.source: record for record in source_statuses}
        lines = ["<b>Состояние поиска</b>"]
        for source in ("avito", "cian", "domclick", "yandex"):
            runtime = runtime_known.get(source)
            if runtime is not None:
                interval = "нет данных"
                if runtime.last_attempt_at is not None and runtime.next_attempt_at is not None:
                    seconds = max(
                        0,
                        int((runtime.next_attempt_at - runtime.last_attempt_at).total_seconds()),
                    )
                    interval = f"{seconds} сек."
                manual = (
                    ", требуется ручное действие"
                    if runtime.health is SourceRunHealth.MANUAL_ATTENTION
                    else ""
                )
                lines.append(
                    f"{html.escape(_source_label(source))}: "
                    f"{html.escape(runtime.health.value)}, "
                    f"последний успех: {_format_time(runtime.last_success_at)}, "
                    f"следующая попытка: {_format_time(runtime.next_attempt_at)}, "
                    f"интервал: {interval}, ошибок подряд: "
                    f"{runtime.consecutive_failures}, карточек: "
                    f"{runtime.last_card_count if runtime.last_card_count is not None else '—'}, "
                    f"новейший ID: {html.escape(runtime.last_newest_id or '—')}{manual}"
                )
                continue
            record = legacy_known.get(source)
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
    details = listing.details or {}
    if details:
        if listing.title:
            lines[0] = f"<b>{html.escape(listing.title[:200])}</b>"
        extra = [f"ID: {listing.source_id}"]
        for key, label in (
            ("published_label", "Опубликовано"),
            ("seller_name", "Продавец"),
            ("seller_type_label", "Тип продавца"),
            ("seller_rating", "Рейтинг"),
        ):
            if details.get(key):
                extra.append(f"{label}: {html.escape(str(details[key])[:200])}")
        for key, value in details.get("characteristics", {}).items():
            extra.append(f"{html.escape(key[:120])}: {html.escape(str(value)[:200])}")
        for line in extra:
            if len("\n".join(lines)) + len(line) < 3000:
                lines.append(line)
        description = details.get("description", "")
        escaped_description = html.escape(description)
        if len("\n".join(lines)) + len(escaped_description) < 3800:
            lines.append("\n" + escaped_description)
        elif description:
            lines.append("\nПолное описание — в текстовом файле ниже.")
    return "\n".join(lines)


def full_listing_text(listing: Listing) -> str:
    details = listing.details or {}
    lines = [
        listing.title or "Объявление",
        listing.url,
        f"ID: {listing.source_id}",
        f"Цена: {listing.price_rub} ₽",
        f"Адрес: {listing.address or 'не указан'}",
    ]
    for key, label in (
        ("published_label", "Опубликовано"),
        ("seller_info", "Продавец"),
        ("seller_url", "Профиль продавца"),
    ):
        if details.get(key):
            lines.append(f"{label}: {details[key]}")
    lines.extend(f"{key}: {value}" for key, value in details.get("characteristics", {}).items())
    lines.extend(("", details.get("description", "")))
    return "\n".join(lines)


def format_source_alert(alert: SourceAlert) -> str:
    source = html.escape(_source_label(alert.source))
    health_labels = {
        "healthy": "работа восстановлена",
        "degraded": "повторяющиеся ошибки",
        "cooldown": "временная пауза",
        "blocked": "источник заблокирован",
        "manual_attention": "нужно ручное действие",
    }
    lines = [f"<b>{source}: {health_labels.get(alert.health, html.escape(alert.health))}</b>"]
    if alert.failure_code:
        lines.append(f"Причина: <code>{html.escape(alert.failure_code)}</code>")
    lines.append(f"Событие: {_format_time(alert.occurred_at)}")
    if alert.next_attempt_at is not None:
        lines.append(f"Следующая попытка: {_format_time(alert.next_attempt_at)}")
    if alert.outage_seconds is not None:
        lines.append(f"Перерыв: {alert.outage_seconds} сек.")
    if alert.health == "manual_attention":
        lines.append("Автоматический опрос остановлен до ручной проверки.")
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
