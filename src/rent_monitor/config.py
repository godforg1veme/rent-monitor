"""Загрузка и проверка конфигурации Rent Monitor."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from rent_monitor.core.models import SearchCriteria


class ConfigurationError(ValueError):
    """Raised when the service configuration is invalid or incomplete."""


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    criteria: SearchCriteria
    sources: tuple[SourceConfig, ...]
    poll_interval_seconds: int
    max_response_bytes: int
    database_path: Path


def load_config(path: Path) -> RuntimeConfig:
    """Load the non-secret TOML configuration and validate its bounds."""
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except OSError as exc:
        raise ConfigurationError(f"Не удалось прочитать файл конфигурации: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError("Файл конфигурации содержит некорректный TOML") from exc

    search = raw.get("search", {})
    limits = raw.get("limits", {})
    source_table = raw.get("sources", {})
    if not isinstance(search, dict) or not isinstance(limits, dict):
        raise ConfigurationError("Секции [search] и [limits] должны быть таблицами")
    if not isinstance(source_table, dict):
        raise ConfigurationError("Секция [sources] должна быть таблицей")

    try:
        criteria = SearchCriteria(
            city=str(search["city"]).strip(),
            rooms=int(search["rooms"]),
            max_monthly_price_rub=int(search["max_monthly_price_rub"]),
            require_no_commission=bool(search["require_no_commission"]),
        )
        interval = int(search.get("poll_interval_seconds", 300))
        max_response_bytes = int(limits.get("max_response_bytes", 8 * 1024 * 1024))
        database = raw.get("database", {})
        if not isinstance(database, dict):
            raise TypeError("database must be a table")
        database_path = Path(
            os.environ.get(
                "RENT_MONITOR_DATABASE",
                str(database.get("path", "data/rent-monitor.sqlite3")),
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError("Не хватает обязательных значений поиска или лимитов") from exc

    if not criteria.city or criteria.rooms < 1:
        raise ConfigurationError("Город должен быть указан, число комнат должно быть больше нуля")
    if criteria.max_monthly_price_rub < 1:
        raise ConfigurationError("Максимальная месячная цена должна быть положительной")
    if interval < 60:
        raise ConfigurationError("Интервал опроса не может быть меньше 60 секунд")
    if max_response_bytes < 1024 or max_response_bytes > 8 * 1024 * 1024:
        raise ConfigurationError("Размер ответа должен быть в диапазоне от 1 KiB до 8 MiB")

    sources = tuple(
        SourceConfig(name=str(name), enabled=bool(enabled))
        for name, enabled in source_table.items()
    )
    expected = {"avito", "cian", "domclick", "yandex"}
    configured = {source.name for source in sources}
    if configured != expected:
        raise ConfigurationError("В [sources] должны быть заданы avito, cian, domclick и yandex")

    return RuntimeConfig(
        criteria=criteria,
        sources=sources,
        poll_interval_seconds=interval,
        max_response_bytes=max_response_bytes,
        database_path=database_path,
    )


def read_telegram_token() -> str:
    """Read the Telegram token only from systemd's credential directory."""
    credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not credentials_dir:
        raise ConfigurationError("systemd credential directory is not available")
    token_path = Path(credentials_dir) / "telegram_bot_token"
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError("Telegram bot credential is missing") from exc
    if not token or "\n" in token:
        raise ConfigurationError("Telegram bot credential is empty or malformed")
    return token

