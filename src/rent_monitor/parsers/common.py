"""Small, fail-closed helpers for explicitly public Schema.org HTML data."""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

from rent_monitor.core.models import (
    Candidate,
    CommissionStatus,
    Listing,
    SearchPageParse,
    SellerType,
)


class PublicPageBlocked(RuntimeError):
    """Raised when a public detail page contains a CAPTCHA/access challenge."""


class _JsonLdReader(HTMLParser):
    """Collect only application/ld+json script payloads from an HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.documents: list[str] = []
        self._active = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attr_map = {key.lower(): value for key, value in attrs}
        content_type = (attr_map.get("type") or "").split(";", 1)[0].strip().lower()
        if content_type == "application/ld+json":
            self._active = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._active:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._active:
            self.documents.append("".join(self._parts))
            self._active = False
            self._parts = []


def _jsonld_documents(html: str) -> tuple[list[Any], bool]:
    reader = _JsonLdReader()
    reader.feed(html)
    parsed: list[Any] = []
    malformed = False
    for document in reader.documents:
        try:
            parsed.append(json.loads(document))
        except (json.JSONDecodeError, TypeError):
            malformed = True
    return parsed, malformed


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _types(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {value.rsplit("/", 1)[-1].lower() for value in values if isinstance(value, str)}


def _is_block_page(html: str) -> bool:
    lowered = html.casefold()
    return any(
        marker in lowered
        for marker in (
            "captcha",
            "access denied",
            "forbidden",
            "доступ запрещён",
            "доступ запрещен",
            "доступ ограничен",
            "подтвердите, что вы не робот",
            "подтвердите что вы не робот",
            "обнаружена подозрительная активность",
        )
    )


def _public_listing_url(raw: Any, base_url: str, allowed_hosts: frozenset[str]) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    url = urljoin(base_url, raw.strip())
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in allowed_hosts or parts.username or parts.password:
        return None
    return url


def _source_id(url: str, id_pattern: re.Pattern[str]) -> str | None:
    match = id_pattern.search(urlsplit(url).path)
    return match.group(1) if match else None


def _as_number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            result = Decimal(str(value))
        except InvalidOperation:
            return None
        return result if result.is_finite() else None
    if isinstance(value, str):
        normalized = value.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
        try:
            result = Decimal(normalized)
        except InvalidOperation:
            return None
        return result if result.is_finite() else None
    return None


def _integer(value: Any) -> int | None:
    number = _as_number(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _monthly_price(node: dict[str, Any]) -> int | None:
    """Accept a price only when currency and monthly billing are explicit."""
    offers = node.get("offers")
    if isinstance(offers, list):
        offer = next((item for item in offers if isinstance(item, dict)), None)
    else:
        offer = offers if isinstance(offers, dict) else None
    if offer is None:
        return None

    currency = str(offer.get("priceCurrency") or "").upper()
    if currency not in {"RUB", "RUR"}:
        return None

    price_spec = offer.get("priceSpecification")
    spec = price_spec if isinstance(price_spec, dict) else {}
    period = " ".join(
        str(value)
        for value in (
            offer.get("billingPeriod"),
            offer.get("pricePeriod"),
            spec.get("unitText"),
            spec.get("billingDuration"),
        )
        if value is not None
    ).casefold()
    if not any(word in period for word in ("month", "monthly", "месяц", "мес.")):
        return None

    amount = _integer(spec.get("price") if spec.get("price") is not None else offer.get("price"))
    return amount if amount is not None and amount > 0 else None


def _address(node: dict[str, Any]) -> str | None:
    value = node.get("address")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, dict):
        return None
    parts = [
        value.get("addressLocality"),
        value.get("streetAddress"),
    ]
    normalized = [part.strip() for part in parts if isinstance(part, str) and part.strip()]
    return ", ".join(dict.fromkeys(normalized)) or None


def _area_m2(node: dict[str, Any]) -> float | None:
    raw = node.get("floorSize")
    if not isinstance(raw, dict):
        return None
    unit = str(raw.get("unitText") or raw.get("unitCode") or "").casefold().replace(" ", "")
    if unit not in {"m²", "m2", "м²", "м2", "sqm", "mtk"}:
        return None
    number = _as_number(raw.get("value"))
    if number is None or number <= 0:
        return None
    return float(number)


def _enum_seller(value: Any) -> SellerType:
    if not isinstance(value, str):
        return SellerType.UNKNOWN
    token = value.strip().casefold()
    if token in {"private", "owner", "individual", "собственник", "частное лицо"}:
        return SellerType.PRIVATE
    if token in {"agency", "agent", "realtor", "агентство", "агент"}:
        return SellerType.AGENCY
    return SellerType.UNKNOWN


def _commission(node: dict[str, Any]) -> tuple[CommissionStatus, int | None, str | None]:
    """Read only an explicit commission field; absence always stays unknown."""
    raw: Any = None
    for key in ("rentalCommission", "commission", "agentFee"):
        if key in node:
            raw = node[key]
            break
    if raw is None:
        properties = node.get("additionalProperty")
        if isinstance(properties, list):
            for prop in properties:
                if not isinstance(prop, dict):
                    continue
                name = str(prop.get("name") or "").casefold()
                if name in {"commission", "rental commission", "комиссия", "комиссия агента"}:
                    raw = prop.get("value")
                    break
    if raw is None:
        return CommissionStatus.UNKNOWN, None, None

    unit: str | None = None
    if isinstance(raw, dict):
        unit_raw = raw.get("unitText") or raw.get("unitCode")
        unit = str(unit_raw) if unit_raw is not None else None
        raw = raw.get("value") if raw.get("value") is not None else raw.get("amount")
    if isinstance(raw, str):
        normalized = raw.strip().casefold()
        if normalized in {"без комиссии", "нет", "0", "0%", "0 %", "none", "no"}:
            return CommissionStatus.NONE, 0, unit
    amount = _as_number(raw)
    if amount is None or amount < 0:
        return CommissionStatus.UNKNOWN, None, unit
    if amount == 0:
        return CommissionStatus.NONE, 0, unit
    return CommissionStatus.POSITIVE, int(amount) if amount == amount.to_integral_value() else None, unit


def _published_at(node: dict[str, Any]) -> datetime | None:
    raw = node.get("datePosted") or node.get("datePublished")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _candidate(
    item: dict[str, Any],
    source: str,
    base_url: str,
    allowed_hosts: frozenset[str],
    id_pattern: re.Pattern[str],
) -> Candidate | None:
    raw_url = item.get("url") or item.get("@id")
    url = _public_listing_url(raw_url, base_url, allowed_hosts)
    if url is None:
        return None
    source_id = _source_id(url, id_pattern)
    if source_id is None:
        return None
    commission_status, commission_value, commission_unit = _commission(item)
    title = item.get("name") or item.get("headline")
    metro = item.get("nearestMetro") or item.get("metroStation")
    if isinstance(metro, dict):
        metro = metro.get("name")
    metro_minutes = _integer(item.get("metroMinutes") or item.get("metro_minutes"))
    return Candidate(
        source=source,
        source_id=source_id,
        url=url,
        price_rub=_monthly_price(item),
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        address=_address(item),
        rooms=_integer(item.get("numberOfRooms")),
        area_m2=_area_m2(item),
        metro=metro.strip() if isinstance(metro, str) and metro.strip() else None,
        metro_minutes=metro_minutes,
        seller_type=_enum_seller(item.get("sellerType")),
        commission_status=commission_status,
        commission_value=commission_value,
        commission_unit=commission_unit,
        published_at=_published_at(item),
    )


def parse_jsonld_search_page(
    html: str,
    base_url: str,
    source: str,
    allowed_hosts: frozenset[str],
    id_pattern: re.Pattern[str],
) -> SearchPageParse:
    """Parse a public search page only when it declares an explicit ItemList."""
    if _is_block_page(html):
        return SearchPageParse(
            recognized=False,
            candidates=[],
            blocked_reason="captcha_or_access_denied",
        )
    if (urlsplit(base_url).hostname or "").lower() not in allowed_hosts:
        return SearchPageParse(
            recognized=False,
            candidates=[],
            blocked_reason="unexpected_source_host",
        )

    documents, malformed = _jsonld_documents(html)
    item_lists = [
        node
        for document in documents
        for node in _walk(document)
        if "itemlist" in _types(node) and isinstance(node.get("itemListElement"), list)
    ]
    if not item_lists:
        reason = "malformed_public_structured_data" if malformed else "unrecognized_public_markup"
        return SearchPageParse(recognized=False, candidates=[], blocked_reason=reason)

    candidates: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    unrecognized_entry = False
    for item_list in item_lists:
        for entry in item_list["itemListElement"]:
            item: Any = entry
            if isinstance(entry, dict) and "item" in entry:
                item = entry["item"]
            if not isinstance(item, dict):
                unrecognized_entry = True
                continue
            candidate = _candidate(item, source, base_url, allowed_hosts, id_pattern)
            if candidate is None:
                unrecognized_entry = True
                continue
            if (candidate.source, candidate.source_id) in seen:
                continue
            seen.add((candidate.source, candidate.source_id))
            candidates.append(candidate)
    if unrecognized_entry:
        return SearchPageParse(
            recognized=False,
            candidates=[],
            blocked_reason="unrecognized_listing_items",
        )
    return SearchPageParse(recognized=True, candidates=candidates)


def _listing_from_candidate(candidate: Candidate, **updates: Any) -> Listing:
    return Listing(
        source=candidate.source,
        source_id=candidate.source_id,
        url=candidate.url,
        price_rub=updates.get("price_rub", candidate.price_rub),
        title=updates.get("title", candidate.title),
        address=updates.get("address", candidate.address),
        rooms=updates.get("rooms", candidate.rooms),
        area_m2=updates.get("area_m2", candidate.area_m2),
        metro=updates.get("metro", candidate.metro),
        metro_minutes=updates.get("metro_minutes", candidate.metro_minutes),
        seller_type=updates.get("seller_type", candidate.seller_type),
        commission_status=updates.get("commission_status", candidate.commission_status),
        commission_value=updates.get("commission_value", candidate.commission_value),
        commission_unit=updates.get("commission_unit", candidate.commission_unit),
        published_at=updates.get("published_at", candidate.published_at),
    )


def parse_jsonld_detail_page(
    html: str,
    candidate: Candidate,
    allowed_hosts: frozenset[str],
    id_pattern: re.Pattern[str],
) -> Listing:
    """Enrich a candidate from matching explicit listing JSON-LD only."""
    if _is_block_page(html):
        raise PublicPageBlocked("captcha_or_access_denied")
    documents, _ = _jsonld_documents(html)
    matching: list[dict[str, Any]] = []
    for document in documents:
        for node in _walk(document):
            raw_url = node.get("url") or node.get("@id")
            url = _public_listing_url(raw_url, candidate.url, allowed_hosts)
            if url and _source_id(url, id_pattern) == candidate.source_id:
                matching.append(node)
    if not matching:
        return _listing_from_candidate(candidate)

    item = matching[0]
    commission_status, commission_value, commission_unit = _commission(item)
    title = item.get("name") or item.get("headline")
    metro = item.get("nearestMetro") or item.get("metroStation")
    if isinstance(metro, dict):
        metro = metro.get("name")
    monthly_price = _monthly_price(item)
    parsed_rooms = _integer(item.get("numberOfRooms"))
    parsed_area = _area_m2(item)
    parsed_metro_minutes = _integer(item.get("metroMinutes") or item.get("metro_minutes"))
    parsed_seller_type = _enum_seller(item.get("sellerType"))
    return _listing_from_candidate(
        candidate,
        price_rub=monthly_price if monthly_price is not None else candidate.price_rub,
        title=title.strip() if isinstance(title, str) and title.strip() else candidate.title,
        address=_address(item) or candidate.address,
        rooms=parsed_rooms if parsed_rooms is not None else candidate.rooms,
        area_m2=parsed_area if parsed_area is not None else candidate.area_m2,
        metro=metro.strip() if isinstance(metro, str) and metro.strip() else candidate.metro,
        metro_minutes=parsed_metro_minutes
        if parsed_metro_minutes is not None
        else candidate.metro_minutes,
        seller_type=parsed_seller_type
        if parsed_seller_type is not SellerType.UNKNOWN
        else candidate.seller_type,
        commission_status=commission_status
        if commission_status is not CommissionStatus.UNKNOWN
        else candidate.commission_status,
        commission_value=commission_value if commission_status is not CommissionStatus.UNKNOWN else candidate.commission_value,
        commission_unit=commission_unit if commission_status is not CommissionStatus.UNKNOWN else candidate.commission_unit,
        published_at=_published_at(item) or candidate.published_at,
    )
