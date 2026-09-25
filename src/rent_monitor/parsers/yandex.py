"""Parser for the public Yandex Realty Moscow rental-search HTML page.

Only the JSON state already embedded in the ordinary search-page HTML is read.
The parser never follows private endpoints and deliberately skips seller,
contact, phone, profile, channel, and description fields.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from rent_monitor.core.models import Candidate, Listing, SearchPageParse


_BLOCKED_MARKERS = (
    "smartcaptcha",
    "captcha",
    "access denied",
    "доступ ограничен",
    "доступ запрещен",
    "доступ запрещён",
    "подтвердите, что вы не робот",
    "проверьте, что вы человек",
)
_EMPTY_MARKERS = (
    "ничего не найдено",
    "объявлений не найдено",
    "предложений не найдено",
    "по вашему запросу нет объявлений",
)
_SENSITIVE_KEYS = {
    "phone",
    "phones",
    "redirectphone",
    "redirectphones",
    "redirectphonesfailed",
    "redirectid",
    "encryptedphone",
    "encryptedphones",
    "encryptedphonenumbers",
    "allowedcommunicationchannels",
    "whatsapp",
    "telegram",
    "author",
    "agentname",
    "partnername",
    "profile",
    "seller",
    "owner",
    "sellerprofile",
    "ownerprofile",
    "agentprofile",
    "agentcontacts",
    "partner",
    "contact",
    "contacts",
    "description",
    "fulldescription",
}
_MONTH_UNITS = {
    "MONTH",
    "MONTHLY",
    "PER_MONTH",
    "1_MONTH",
    "MONTH_RENT",
    "МЕСЯЦ",
    "МЕСЯЧНО",
}


class _InitialStateScript(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._capture = False
        self._parts: list[str] = []
        self.state_script: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {name.lower(): value for name, value in attrs}
        if attributes.get("id") == "initial_state_script":
            self._capture = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capture:
            self.state_script = "".join(self._parts)
            self._capture = False


def _strip_sensitive_fields(value: Any) -> Any:
    """Remove contact and description branches before examining listing data."""

    if isinstance(value, dict):
        for key in list(value):
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            sensitive = (
                normalized in _SENSITIVE_KEYS
                or "phone" in normalized
                or "contact" in normalized
                or "profile" in normalized
                or "description" in normalized
                or "communicationchannel" in normalized
                or normalized in {"whatsapp", "telegram", "author", "seller", "owner", "partner"}
            )
            if sensitive:
                del value[key]
            else:
                _strip_sensitive_fields(value[key])
    elif isinstance(value, list):
        for item in value:
            _strip_sensitive_fields(item)
    return value


def _blocked_reason(html: str) -> str | None:
    lowered = html.casefold()
    if any(marker in lowered for marker in _BLOCKED_MARKERS):
        return "captcha" if "captcha" in lowered or "smartcaptcha" in lowered else "access_restricted"
    return None


def detect_blocked_page(html: str) -> str | None:
    """Return a closed block category without exposing page content."""

    return _blocked_reason(html)


def _load_initial_state(html: str) -> Any | None:
    parser = _InitialStateScript()
    try:
        parser.feed(html)
    except Exception:
        return None
    if not parser.state_script:
        return None
    assignment = re.search(r"\bwindow\.INITIAL_STATE\s*=\s*", parser.state_script)
    if not assignment:
        return None
    try:
        state, _ = json.JSONDecoder().raw_decode(parser.state_script[assignment.end() :].lstrip())
    except (json.JSONDecodeError, ValueError):
        return None
    return _strip_sensitive_fields(state)


def _walk_offers(value: Any):
    """Yield offer-shaped objects without traversing personal/contact payloads."""

    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            if "offerId" in current and isinstance(current.get("offerId"), (str, int)):
                yield current
            for key, child in current.items():
                if str(key).casefold() not in _SENSITIVE_KEYS:
                    pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)


def _mapping_value(parent: Any, *keys: str) -> Any:
    if not isinstance(parent, dict):
        return None
    for key in keys:
        value = parent.get(key)
        if value is not None:
            return value
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip().replace("\u00a0", " ").replace(" ", "").replace(",", ".")
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _integer(value: Any) -> int | None:
    parsed = _number(value)
    if parsed is None or not parsed.is_integer():
        return None
    return int(parsed)


def _text(value: Any, *, limit: int = 500) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned and len(cleaned) <= limit:
            return cleaned
    return None


def _field_text(node: Any, *keys: str) -> str | None:
    value = _mapping_value(node, *keys)
    if isinstance(value, dict):
        value = _mapping_value(value, "name", "value", "text", "formatted")
    return _text(value)


def _published_at(value: Any) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 100_000_000_000:
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return None


def _fee(offer: dict[str, Any]) -> tuple[str, int | None, str | None]:
    raw = _mapping_value(offer, "agentFee", "commission")
    unit: str | None = None
    value: Any = raw
    if isinstance(raw, dict):
        value = _mapping_value(raw, "value", "amount", "rate")
        unit = _text(_mapping_value(raw, "unit", "currency", "type"), limit=40)
    if isinstance(value, str):
        normalized = value.casefold().replace("ё", "е")
        if "без комис" in normalized or normalized.strip() in {"0", "0%"}:
            return "none", None, None
    numeric = _number(value)
    if numeric is None:
        return "unknown", None, None
    if numeric == 0:
        return "none", None, None
    commission_value = int(numeric) if numeric.is_integer() else None
    return "positive", commission_value, unit.lower() if unit else None


def _area(offer: dict[str, Any]) -> float | None:
    raw = _mapping_value(offer, "area", "floorSize")
    if isinstance(raw, dict):
        raw = _mapping_value(raw, "value", "total", "amount")
    return _number(raw)


def _metro(offer: dict[str, Any]) -> tuple[str | None, int | None]:
    raw = _mapping_value(offer, "metro", "subway", "metroStation")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, str):
        return _text(raw, limit=120), None
    if not isinstance(raw, dict):
        return None, None
    name = _text(_mapping_value(raw, "name", "title"), limit=120)
    minutes = _integer(_mapping_value(raw, "minutes", "timeMinutes", "walkingTime"))
    return name, minutes


def _candidate(offer: dict[str, Any], base_url: str) -> Candidate | None:
    raw_id = offer.get("offerId")
    source_id = str(raw_id).strip()
    if not re.fullmatch(r"[0-9]{5,}", source_id):
        return None

    raw_url = _text(offer.get("url"), limit=300)
    url = urljoin(base_url, raw_url) if raw_url else f"https://realty.yandex.ru/offer/{source_id}"
    parsed_url = urlparse(url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "realty.yandex.ru"
        or not re.fullmatch(r"/offer/[0-9]{5,}/?", parsed_url.path)
    ):
        # The canonical public detail path is also visible in page-generated links.
        url = f"https://realty.yandex.ru/offer/{source_id}"

    raw_price = offer.get("price")
    if not isinstance(raw_price, dict):
        raw_price = {}
    currency = str(_mapping_value(raw_price, "currency") or "").upper()
    period = str(
        _mapping_value(raw_price, "period", "pricingPeriod")
        or _mapping_value(offer, "pricingPeriod", "rentPeriod")
        or ""
    ).upper()
    monthly = period in _MONTH_UNITS or any(
        marker in period for marker in ("MONTH", "МЕСЯЦ")
    )
    raw_price_value = _mapping_value(raw_price, "value", "amount")
    price = _integer(raw_price_value) if currency in {"RUB", "RUR"} and monthly else None

    rooms = _integer(_mapping_value(offer, "roomsTotal", "numberOfRooms"))
    fee_status, fee_value, fee_unit = _fee(offer)
    metro, metro_minutes = _metro(offer)
    address = _field_text(offer, "address", "fullAddress", "addressLine")
    title = _field_text(offer, "title", "name", "headline")
    if title and len(title) > 160:
        title = None

    return Candidate(
        source="yandex",
        source_id=source_id,
        url=url,
        price_rub=price,
        title=title,
        address=address,
        rooms=rooms,
        area_m2=_area(offer),
        metro=metro,
        metro_minutes=metro_minutes,
        seller_type="unknown",
        commission_status=fee_status,
        commission_value=fee_value,
        commission_unit=fee_unit,
        published_at=_published_at(_mapping_value(offer, "creationDate", "publishedAt")),
    )


def _to_listing(candidate: Candidate) -> Listing:
    return Listing(
        source=candidate.source,
        source_id=candidate.source_id,
        url=candidate.url,
        price_rub=candidate.price_rub,
        title=candidate.title,
        address=candidate.address,
        rooms=candidate.rooms,
        area_m2=candidate.area_m2,
        metro=candidate.metro,
        metro_minutes=candidate.metro_minutes,
        seller_type=candidate.seller_type,
        commission_status=candidate.commission_status,
        commission_value=candidate.commission_value,
        commission_unit=candidate.commission_unit,
        published_at=candidate.published_at,
    )


def parse_search_page(html: str, base_url: str) -> SearchPageParse:
    """Return candidates from the normal search page's embedded public state."""

    blocked = _blocked_reason(html)
    if blocked:
        return SearchPageParse(recognized=False, candidates=[], blocked_reason=blocked)

    state = _load_initial_state(html)
    if state is None:
        if any(marker in html.casefold() for marker in _EMPTY_MARKERS):
            return SearchPageParse(recognized=True, candidates=[])
        return SearchPageParse(recognized=False, candidates=[])

    candidates: dict[str, Candidate] = {}
    for offer in _walk_offers(state):
        parsed = _candidate(offer, base_url)
        if parsed:
            candidates.setdefault(parsed.source_id, parsed)

    if candidates:
        return SearchPageParse(recognized=True, candidates=list(candidates.values()))
    if any(marker in html.casefold() for marker in _EMPTY_MARKERS):
        return SearchPageParse(recognized=True, candidates=[])
    # A valid state with no extractable offer IDs is not treated as an empty page.
    return SearchPageParse(recognized=False, candidates=[])


def parse_detail_page(html: str, candidate: Candidate) -> Listing | None:
    """Read matching public listing fields from the ordinary detail-page HTML."""

    if _blocked_reason(html):
        return None
    state = _load_initial_state(html)
    if state is None:
        return None
    for offer in _walk_offers(state):
        if str(offer.get("offerId")) != candidate.source_id:
            continue
        parsed = _candidate(offer, candidate.url)
        if parsed:
            return _to_listing(parsed)
    return None
