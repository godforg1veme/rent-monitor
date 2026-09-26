"""Command-line entry point and async application lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from rent_monitor.browser.transport import PlaywrightBrowserTransport
from rent_monitor.collectors import build_collectors
from rent_monitor.config import ConfigurationError, load_config, read_telegram_token
from rent_monitor.core.scheduler import CollectorRuntime, run_collectors, run_outbox_worker
from rent_monitor.storage.sqlite import SQLiteRepository
from rent_monitor.telegram.bot import TelegramNotifier, create_dispatcher
from rent_monitor.transport import BoundedHttpClient

logger = logging.getLogger(__name__)
DEFAULT_CONFIG = Path("config/search.toml")


async def serve(config_path: Path) -> None:
    """Run the source scheduler and the private Telegram bot until shutdown."""
    config = load_config(config_path)
    collectors = build_collectors(config)
    if not collectors:
        raise ConfigurationError("В конфигурации не включён ни один источник")

    token = read_telegram_token()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    repository = SQLiteRepository(config.database_path)
    await repository.initialize()
    state_changed = asyncio.Event()
    stop_event = asyncio.Event()
    dispatcher = create_dispatcher(repository, state_changed)
    browser: PlaywrightBrowserTransport | None = None

    try:
        if any(collector.source == "avito" for collector in collectors):
            profile_path = config.database_path.parent / "avito-browser-profile"
            browser = PlaywrightBrowserTransport(profile_path, headless=False)
            await browser.start()
        async with BoundedHttpClient(config.max_response_bytes) as client:
            source_configs = {source.name: source for source in config.sources}
            runtimes = tuple(
                CollectorRuntime(
                    collector=collector,
                    client=browser if collector.source == "avito" else client,
                    interval_seconds=source_configs[collector.source].poll_interval_seconds,
                )
                for collector in collectors
            )
            try:
                async with asyncio.TaskGroup() as tasks:
                    scheduler_task = tasks.create_task(
                        run_collectors(
                            runtimes,
                            config.criteria,
                            repository,
                            stop_event,
                            state_changed=state_changed,
                        ),
                        name="source-scheduler",
                    )
                    outbox_task = tasks.create_task(
                        run_outbox_worker(repository, TelegramNotifier(bot), stop_event),
                        name="telegram-outbox",
                    )
                    polling_task = tasks.create_task(
                        dispatcher.start_polling(
                            bot,
                            allowed_updates=dispatcher.resolve_used_update_types(),
                            close_bot_session=False,
                            handle_signals=True,
                            tasks_concurrency_limit=8,
                        ),
                        name="telegram-polling",
                    )
                    try:
                        await polling_task
                    finally:
                        stop_event.set()
                        scheduler_task.cancel()
                        outbox_task.cancel()
            except* Exception:
                logger.error("application phase=run status=error")
                raise
    finally:
        if browser is not None:
            await browser.aclose()
        await bot.session.close()
        await repository.close()


async def create_pairing_code(config_path: Path, ttl_seconds: int) -> str:
    """Create a short-lived one-use code without starting a Telegram client."""
    config = load_config(config_path)
    repository = SQLiteRepository(config.database_path)
    await repository.initialize()
    try:
        return await repository.issue_pairing_code(ttl_seconds=ttl_seconds)
    finally:
        await repository.close()


async def clear_source_pause(config_path: Path, source: str) -> bool:
    """Clear one stored pause after the source route/parser has been reviewed."""
    config = load_config(config_path)
    if source not in {item.name for item in config.sources}:
        raise ConfigurationError(f"Источник {source} отсутствует в конфигурации")
    repository = SQLiteRepository(config.database_path)
    await repository.initialize()
    try:
        return await repository.clear_source_pause(source)
    finally:
        await repository.close()


def cli() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="rent-monitor",
        description="Поиск новых объявлений аренды по заданным фильтрам.",
    )
    parser.add_argument("--version", action="version", version="rent-monitor 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)

    serve_parser = commands.add_parser("serve", help="Запустить сборщики и Telegram-бота")
    serve_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)

    pairing_parser = commands.add_parser("pair-code", help="Выпустить одноразовый код привязки")
    pairing_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    pairing_parser.add_argument("--ttl-seconds", type=int, default=600)

    resume_parser = commands.add_parser(
        "resume-source",
        help="Снять сохранённую паузу после ручной проверки источника",
    )
    resume_parser.add_argument("source", choices=("avito", "cian", "domclick", "yandex"))
    resume_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        if args.command == "serve":
            asyncio.run(serve(args.config))
        elif args.command == "pair-code":
            code = asyncio.run(create_pairing_code(args.config, args.ttl_seconds))
            print(
                f"Код привязки действует {args.ttl_seconds} секунд. Отправьте боту: /start {code}"
            )
        elif args.command == "resume-source":
            cleared = asyncio.run(clear_source_pause(args.config, args.source))
            if not cleared:
                print(f"Для {args.source} нет сохранённой паузы.", file=sys.stderr)
                return 1
            print(f"Сохранённая пауза для {args.source} снята. Перезапустите сервис.")
        return 0
    except (ConfigurationError, OSError, RuntimeError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(cli())
