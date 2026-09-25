"""Загрузка и проверка конфигурации Rent Monitor."""

from __future__ import annotations

import os
import re
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
        city = search["city"]
        rooms = search["rooms"]
        max_price = search["max_monthly_price_rub"]
        no_commission = search["require_no_commission"]
        interval = search.get("poll_interval_seconds", 300)
        max_response_bytes = limits.get("max_response_bytes", 8 * 1024 * 1024)
        database = raw.get("database", {})
        if not isinstance(database, dict):
            raise TypeError("database must be a table")
        database_value = database.get("path", "data/rent-monitor.sqlite3")
        if (
            not isinstance(city, str)
            or isinstance(rooms, bool)
            or not isinstance(rooms, int)
            or isinstance(max_price, bool)
            or not isinstance(max_price, int)
            or not isinstance(no_commission, bool)
            or isinstance(interval, bool)
            or not isinstance(interval, int)
            or isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not isinstance(database_value, str)
        ):
            raise TypeError("configuration values have the wrong type")
        criteria = SearchCriteria(
            city=city.strip(),
            rooms=rooms,
            max_monthly_price_rub=max_price,
            require_no_commission=no_commission,
        )
        database_path = Path(os.environ.get("RENT_MONITOR_DATABASE", database_value))
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

    expected = {"avito", "cian", "domclick", "yandex"}
    if any(
        not isinstance(name, str) or not isinstance(enabled, bool)
        for name, enabled in source_table.items()
    ):
        raise ConfigurationError("Каждый источник должен иметь логическое значение true или false")
    sources = tuple(
        SourceConfig(name=name, enabled=enabled) for name, enabled in source_table.items()
    )
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
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        raise ConfigurationError("Telegram bot credential is empty or malformed")
    return token
