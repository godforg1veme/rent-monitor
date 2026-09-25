"""Conservative parser for public Domclick search-page structured data.

The collector is currently paused because no public Moscow rental-search route
has been established and the VPS's Domclick root returned HTTP 401. This parser
only accepts schema.org data embedded in an ordinary HTML page; it does not
discover or call private endpoints.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from rent_monitor.core.models import Candidate, SearchPageParse


_BLOCKED_MARKERS = (
    "captcha",
    "smartcaptcha",
    "access denied",
    "доступ ограничен",
    "доступ запрещен",
    "доступ запрещён",
    "авторизуйтесь",
    "войдите в аккаунт",
)
_EMPTY_MARKERS = (
    "ничего не найдено",
    "объявлений не найдено",
    "предложений не найдено",
    "по вашему запросу нет",
)


class _JsonLdScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self._collecting = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        values = {key.lower(): value for key, value in attrs}
        script_type = (values.get("type") or "").lower().split(";", 1)[0].strip()
        if script_type == "application/ld+json":
            self._collecting = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._collecting:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._collecting:
            self.scripts.append("".join(self._parts))
            self._parts = []
            self._collecting = False


def _blocked_reason(html: str) -> str | None:
    lowered = html.casefold()
    if any(marker in lowered for marker in _BLOCKED_MARKERS):
        return "captcha" if "captcha" in lowered or "smartcaptcha" in lowered else "access_restricted"
    return None


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _schema_type(value: Any) -> set[str]:
    raw = value.get("@type") if isinstance(value, dict) else None
    values = raw if isinstance(raw, list) else [raw]
    return {str(item).rsplit("/", 1)[-1].casefold() for item in values if item}


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    parsed = _float(value)
    return int(parsed) if parsed is not None and parsed.is_integer() else None


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = (
            value.strip()
            .replace("\u00a0", " ")
            .replace(" ", "")
            .replace("₽", "")
            .replace(",", ".")
        )
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _address(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    return _first_text(
        value.get("streetAddress"),
        value.get("name"),
        value.get("addressLocality"),
    )


def _candidate(item: dict[str, Any], base_url: str) -> Candidate | None:
    item_url = _first_text(item.get("url"), item.get("@id"))
    if not item_url:
        return None
    url = urljoin(base_url, item_url)
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in {
        "domclick.ru",
        "www.domclick.ru",
    }:
        return None

    item_id = _first_text(item.get("identifier"), item.get("sku"))
    if isinstance(item.get("identifier"), dict):
        item_id = _first_text(item["identifier"].get("value"), item["identifier"].get("name"))
    if not item_id:
        match = re.search(r"(?:^|/)([0-9]{5,})(?:/|$)", parsed_url.path)
        item_id = match.group(1) if match else None
    if not item_id:
        return None

    offer = item.get("offers")
    if isinstance(offer, list):
        offer = offer[0] if offer else None
    if not isinstance(offer, dict):
        offer = {}
    currency = str(offer.get("priceCurrency") or "").upper()
    price = _integer(offer.get("price")) if currency in {"RUB", "RUR"} else None

    area_node = item.get("floorSize")
    area = _float(area_node.get("value")) if isinstance(area_node, dict) else _float(area_node)
    rooms = _integer(item.get("numberOfRooms"))

    # No zero-fee assumption: schema data normally omits the commission.
    fee = item.get("agentFee")
    fee_raw = fee.get("value") if isinstance(fee, dict) else fee
    fee_value = _float(fee_raw)
    if fee_value is None:
        commission_status = "unknown"
    elif fee_value == 0:
        commission_status = "none"
    else:
        commission_status = "positive"
    commission_value = (
        int(fee_value)
        if fee_value is not None and fee_value > 0 and fee_value.is_integer()
        else None
    )
    commission_unit = (
        _first_text(fee.get("unit"), fee.get("currency"))
        if isinstance(fee, dict) and commission_status == "positive"
        else None
    )

    address_node = item.get("address")
    title = _first_text(item.get("name"), item.get("headline"))
    if title and len(title) > 160:
        title = None

    return Candidate(
        source="domclick",
        source_id=str(item_id),
        url=url,
        price_rub=price,
        title=title,
        address=_address(address_node),
        rooms=rooms,
        area_m2=area,
        metro=None,
        metro_minutes=None,
        seller_type="unknown",
        commission_status=commission_status,
        commission_value=commission_value,
        commission_unit=commission_unit,
        published_at=None,
    )


def parse_search_page(html: str, base_url: str) -> SearchPageParse:
    """Parse only schema.org ItemList data embedded in public HTML."""

    blocked = _blocked_reason(html)
    if blocked:
        return SearchPageParse(recognized=False, candidates=[], blocked_reason=blocked)

    scripts = _JsonLdScripts()
    try:
        scripts.feed(html)
    except Exception:
        return SearchPageParse(recognized=False, candidates=[])

    item_lists: list[list[Any]] = []
    for script in scripts.scripts:
        try:
            document = json.loads(script)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(document):
            if "itemlist" in _schema_type(node) and isinstance(node.get("itemListElement"), list):
                item_lists.append(node["itemListElement"])

    if not item_lists:
        if any(marker in html.casefold() for marker in _EMPTY_MARKERS):
            return SearchPageParse(recognized=True, candidates=[])
        return SearchPageParse(recognized=False, candidates=[])

    candidates: dict[str, Candidate] = {}
    for items in item_lists:
        for entry in items:
            item = entry.get("item") if isinstance(entry, dict) else None
            if not isinstance(item, dict):
                item = entry if isinstance(entry, dict) else None
            if not item:
                continue
            parsed = _candidate(item, base_url)
            if parsed:
                candidates.setdefault(parsed.source_id, parsed)

    # An explicit ItemList is a recognized result structure, including an empty list.
    return SearchPageParse(recognized=True, candidates=list(candidates.values()))
