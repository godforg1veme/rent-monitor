"""Загрузка и проверка конфигурации Rent Monitor."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from rent_monitor.core.models import SearchCriteria


class ConfigurationError(ValueError):
    """Raised when the service configuration is invalid or incomplete."""


@dataclass(frozen=True, slots=True)
class AvitoSearchConfig:
    name: str
    url: str
    poll_interval_seconds: int


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    enabled: bool
    poll_interval_seconds: int
    searches: tuple[AvitoSearchConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class CaptchaConfig:
    enabled: bool
    public_base_url: str | None
    token_directory: Path


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    criteria: SearchCriteria
    sources: tuple[SourceConfig, ...]
    poll_interval_seconds: int
    max_response_bytes: int
    database_path: Path
    captcha: CaptchaConfig


def load_config(path: Path) -> RuntimeConfig:
    """Load the non-secret TOML configuration and validate its bounds."""
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except OSError as exc:
        raise ConfigurationError(f"Не удалось прочитать файл конфигурации: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError("Файл конфигурации содержит некорректный TOML") from exc

    return _load_raw(raw, path)


def _load_raw(raw: dict[str, Any], path: Path) -> RuntimeConfig:
    """Validate an already decoded configuration mapping."""

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
    if max_response_bytes < 1024 or max_response_bytes > 8 * 1024 * 1024:
        raise ConfigurationError("Размер ответа должен быть в диапазоне от 1 KiB до 8 MiB")

    expected = {"avito", "cian", "domclick", "yandex"}
    configured = set(source_table)
    if configured != expected:
        raise ConfigurationError("В [sources] должны быть заданы avito, cian, domclick и yandex")

    sources = tuple(
        _load_source(name, source_table[name]) for name in ("avito", "cian", "domclick", "yandex")
    )
    enabled_intervals = [source.poll_interval_seconds for source in sources if source.enabled]
    if not enabled_intervals:
        interval = 300
    else:
        interval = min(enabled_intervals)

    captcha_raw = raw.get("captcha", {})
    if not isinstance(captcha_raw, dict):
        raise ConfigurationError("Секция [captcha] должна быть таблицей")
    captcha_enabled = captcha_raw.get("enabled", False)
    token_directory_value = os.environ.get(
        "RENT_MONITOR_CAPTCHA_TOKEN_DIRECTORY",
        captcha_raw.get("token_directory", "/run/rent-monitor-captcha/tokens"),
    )
    public_base_url = os.environ.get(
        "RENT_MONITOR_CAPTCHA_BASE_URL",
        captcha_raw.get("public_base_url"),
    )
    if not isinstance(captcha_enabled, bool) or not isinstance(token_directory_value, str):
        raise ConfigurationError("Параметры CAPTCHA имеют неверный тип")
    if public_base_url is not None:
        from rent_monitor.browser.access_url import is_private_browser_url

        if not isinstance(public_base_url, str) or not is_private_browser_url(public_base_url):
            raise ConfigurationError("Адрес CAPTCHA должен быть безопасным HTTPS URL")
        public_base_url = public_base_url.rstrip("/")
    captcha = CaptchaConfig(
        enabled=captcha_enabled,
        public_base_url=public_base_url,
        token_directory=Path(token_directory_value),
    )

    return RuntimeConfig(
        criteria=criteria,
        sources=sources,
        poll_interval_seconds=interval,
        max_response_bytes=max_response_bytes,
        database_path=database_path,
        captcha=captcha,
    )


def _load_source(name: str, raw: object) -> SourceConfig:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Секция [sources.{name}] должна быть таблицей")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise ConfigurationError(f"sources.{name}.enabled должен быть логическим значением")

    if name == "avito":
        searches_raw = raw.get("searches", [])
        if not isinstance(searches_raw, list) or not searches_raw:
            raise ConfigurationError("Для Avito должен быть задан хотя бы один поиск")
        searches = tuple(_load_avito_search(item) for item in searches_raw)
        names = [search.name for search in searches]
        if len(names) != len(set(names)):
            raise ConfigurationError("Имена поисков Avito должны быть уникальными")
        return SourceConfig(
            name=name,
            enabled=enabled,
            poll_interval_seconds=min(search.poll_interval_seconds for search in searches),
            searches=searches,
        )

    interval = raw.get("poll_interval_seconds", 300)
    _validate_interval(interval, f"sources.{name}.poll_interval_seconds")
    return SourceConfig(name=name, enabled=enabled, poll_interval_seconds=interval)


def _load_avito_search(raw: object) -> AvitoSearchConfig:
    if not isinstance(raw, dict):
        raise ConfigurationError("Каждый поиск Avito должен быть таблицей")
    name = raw.get("name")
    url = raw.get("url")
    interval = raw.get("poll_interval_seconds", 60)
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", name):
        raise ConfigurationError("Имя поиска Avito имеет неверный формат")
    if not isinstance(url, str) or not _is_safe_avito_url(url):
        raise ConfigurationError("Поиск Avito должен использовать безопасный HTTPS URL Avito")
    _validate_interval(interval, f"Интервал поиска Avito {name}")
    return AvitoSearchConfig(name=name, url=url, poll_interval_seconds=interval)


def _validate_interval(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= 3600:
        raise ConfigurationError(f"{field} должен быть в диапазоне от 60 до 3600 секунд")


def _is_safe_avito_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(hostname)
        and (hostname == "avito.ru" or hostname.endswith(".avito.ru"))
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
    )


def _is_safe_https_url(url: str, *, allowed_ports: tuple[int | None, ...] = (None, 443)) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port in allowed_ports
        )
    except ValueError:
        return False


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
