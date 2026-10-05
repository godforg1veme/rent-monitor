"""Fail-closed parser for the public Avito long-term rental search page."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from rent_monitor.core.models import (
    Candidate,
    CommissionStatus,
    FieldEvidence,
    SearchPageParse,
    SellerType,
)

_ALLOWED_HOSTS = frozenset({"avito.ru", "www.avito.ru"})
_SEARCH_PATH_PREFIX = "/moskva/kvartiry/sdam/na_dlitelnyy_srok/bez_komissii-"
_MOSCOW_LISTING_PREFIX = "/moskva/kvartiry/"
_LISTING_ID = re.compile(r"(?:_|/)(\d{7,})(?:\.|/|$)")
_FIELD_BY_MARKER = {
    "item-title": "title",
    "item-price": "price_label",
    "item-price-value": "price_value",
    "item-specific-params": "specific_params",
    "item-address": "address",
    "item-location": "location",
    "item-date": "published_label",
    "street_link": "street",
    "house_link": "house",
}
_EMPTY_MARKERS = (
    "ничего не найдено",
    "объявлений не найдено",
    "по вашему запросу ничего не найдено",
)
_CAPTCHA_MARKERS = (
    "для решения капчи",
    "captcha",
    "подтвердите, что вы не робот",
    "подтвердите что вы не робот",
    "проверьте, что вы человек",
    "проверка на робота",
)
_ACCESS_MARKERS = (
    "access denied",
    "доступ запрещён",
    "доступ запрещен",
    "доступ ограничен",
)


@dataclass(slots=True)
class _RawCard:
    source_id: str | None
    href: str | None = None
    values: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    published_machine: str | None = None


@dataclass(frozen=True, slots=True)
class AvitoSearchContext:
    recognized: bool
    city: str | None
    long_term: bool
    no_commission: bool
    newest_first: bool


@dataclass(slots=True)
class _Frame:
    tag: str
    field_name: str | None = None
    starts_card: bool = False
    hides_text: bool = False
    starts_primary: bool = False
    page_field_name: str | None = None


class _AvitoCardReader(HTMLParser):
    """Read only public fields from result cards; ignore descriptions and profiles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[_RawCard] = []
        self.current_card: _RawCard | None = None
        self.frames: list[_Frame] = []
        self.active_fields: list[str] = []
        self.hidden_depth = 0
        self.outside_text: list[str] = []
        self.outside_text_length = 0
        self.invalid_structure = False
        self.primary_depth = 0
        self.primary_seen = False
        self.page_values: dict[str, list[str]] = defaultdict(list)
        self.active_page_fields: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value for key, value in attrs}
        marker = attr_map.get("data-marker")
        starts_primary = marker == "catalog-serp" and self.hidden_depth == 0
        if starts_primary:
            if self.primary_depth:
                self.invalid_structure = True
            self.primary_depth += 1
            self.primary_seen = True
        starts_card = marker == "item" and self.hidden_depth == 0 and self.primary_depth > 0
        if starts_card:
            if self.current_card is not None:
                self.invalid_structure = True
            else:
                item_id = attr_map.get("data-item-id")
                self.current_card = _RawCard(item_id.strip() if item_id else None)

        field_name = _FIELD_BY_MARKER.get(marker or "") if self.current_card else None
        if (
            self.current_card is not None
            and field_name is None
            and tag.lower() == "p"
            and "location" in self.active_fields
            and self.hidden_depth == 0
        ):
            line_count = sum(key.startswith("location_line_") for key in self.current_card.values)
            field_name = f"location_line_{line_count}"
            self.current_card.values[field_name] = []
        if self.current_card is not None and field_name and self.hidden_depth == 0:
            if field_name == "title":
                href = attr_map.get("href")
                if href and self.current_card.href is None:
                    self.current_card.href = href.strip()
            self.active_fields.append(field_name)
            if field_name == "published_label" and attr_map.get("datetime"):
                self.current_card.published_machine = attr_map["datetime"].strip()

        page_field_name = None
        if self.current_card is None and self.hidden_depth == 0:
            if tag.lower() == "h1":
                page_field_name = "heading"
            elif marker == "filter-active" and attr_map.get("aria-pressed") != "false":
                page_field_name = "selected_filter"
            elif marker == "sort/title":
                page_field_name = "sort"
            if page_field_name:
                self.active_page_fields.append(page_field_name)

        hides_text = tag.lower() in {"script", "style", "noscript"}
        self.frames.append(
            _Frame(
                tag=tag.lower(),
                field_name=field_name if field_name and self.hidden_depth == 0 else None,
                starts_card=starts_card and self.current_card is not None,
                hides_text=hides_text,
                starts_primary=starts_primary,
                page_field_name=page_field_name,
            )
        )
        if hides_text:
            self.hidden_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        frame_index = next(
            (
                index
                for index in range(len(self.frames) - 1, -1, -1)
                if self.frames[index].tag == normalized_tag
            ),
            None,
        )
        if frame_index is None:
            return

        closing_frames = self.frames[frame_index:]
        del self.frames[frame_index:]
        for frame in reversed(closing_frames):
            if frame.field_name:
                for index in range(len(self.active_fields) - 1, -1, -1):
                    if self.active_fields[index] == frame.field_name:
                        del self.active_fields[index]
                        break
            if frame.page_field_name:
                for index in range(len(self.active_page_fields) - 1, -1, -1):
                    if self.active_page_fields[index] == frame.page_field_name:
                        del self.active_page_fields[index]
                        break
            if frame.starts_card:
                if self.current_card is None:
                    self.invalid_structure = True
                else:
                    self.cards.append(self.current_card)
                    self.current_card = None
            if frame.hides_text:
                self.hidden_depth = max(0, self.hidden_depth - 1)
            if frame.starts_primary:
                self.primary_depth = max(0, self.primary_depth - 1)

    def handle_data(self, data: str) -> None:
        if self.hidden_depth or not data:
            return
        if self.current_card is not None:
            for field_name in dict.fromkeys(self.active_fields):
                self.current_card.values[field_name].append(data)
        else:
            for field_name in dict.fromkeys(self.active_page_fields):
                self.page_values[field_name].append(data)
            if self.outside_text_length < 20_000:
                self.outside_text.append(data)
                self.outside_text_length += len(data)

    def close(self) -> None:
        super().close()
        if self.current_card is not None:
            self.invalid_structure = True


def _clean(parts: list[str]) -> str | None:
    value = " ".join("".join(parts).split())
    return value or None


def _field(card: _RawCard, name: str) -> str | None:
    return _clean(card.values.get(name, []))


def page_block_reason(visible_text: str) -> str | None:
    """Distinguish a pending automatic browser check from a manual challenge."""
    text = visible_text.casefold().replace("ё", "е")
    if any(marker.replace("ё", "е") in text for marker in _CAPTCHA_MARKERS):
        return "captcha"
    if "проверка безопасности" in text and "выполняется проверка" in text:
        return "browser_verification"
    if any(marker.replace("ё", "е") in text for marker in _ACCESS_MARKERS):
        return "access_restricted"
    return None


def _integer(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    if not digits:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    return value if value > 0 else None


def _rooms(title: str | None) -> int | None:
    if not title:
        return None
    match = re.search(
        r"(?<!\d)(\d{1,2})\s*[-‐‑‒–—]?\s*(?:к\.(?=\s|$)|комнат\w*)",
        title,
        flags=re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def _area(title: str | None) -> float | None:
    if not title:
        return None
    match = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:м²|м2|кв\.?\s*м)", title, re.IGNORECASE)
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


def _commission(text: str | None) -> tuple[CommissionStatus, int | None, str | None]:
    if not text:
        return CommissionStatus.UNKNOWN, None, None
    normalized = text.casefold().replace("ё", "е")
    match = re.search(
        r"комисси\w*\s*(?:агент\w*\s*)?[:—–-]?\s*(\d+(?:[,.]\d+)?)\s*(%|₽|руб\w*)?",
        normalized,
    )
    if "без комисси" in normalized:
        if match:
            try:
                if float(match.group(1).replace(",", ".")) > 0:
                    return CommissionStatus.UNKNOWN, None, None
            except ValueError:
                return CommissionStatus.UNKNOWN, None, None
        return CommissionStatus.NONE, 0, None

    if not match:
        return CommissionStatus.UNKNOWN, None, None
    try:
        amount = float(match.group(1).replace(",", "."))
    except ValueError:
        return CommissionStatus.UNKNOWN, None, None
    if amount < 0:
        return CommissionStatus.UNKNOWN, None, None
    if amount == 0:
        return CommissionStatus.NONE, 0, match.group(2)
    unit = "%" if match.group(2) == "%" else "RUB" if match.group(2) else None
    return CommissionStatus.POSITIVE, int(amount) if amount.is_integer() else None, unit


def _published_at(
    machine_value: str | None,
    visible_value: str | None,
    observed_at: datetime | None,
) -> datetime | None:
    if machine_value:
        try:
            value = datetime.fromisoformat(machine_value.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if observed_at is None or not visible_value:
        return None
    normalized = visible_value.casefold().replace("ё", "е")
    match = re.fullmatch(r"\s*(\d+)\s+(минут\w*|час\w*)\s+назад\s*", normalized)
    if not match:
        return None
    amount = int(match.group(1))
    delta = (
        timedelta(minutes=amount) if match.group(2).startswith("минут") else timedelta(hours=amount)
    )
    return observed_at - delta


def _metro(location: str | None) -> tuple[str | None, int | None]:
    if not location:
        return None, None
    match = re.search(
        r"^(.+?)[,\s]+(?:до\s*)?(\d+)(?:\s*[-–—]\s*(\d+))?\s*мин\.?",
        location.strip(),
        re.IGNORECASE,
    )
    if not match:
        return location.strip() or None, None
    name = match.group(1).strip(" ,") or None
    minutes = int(match.group(3) or match.group(2))
    return name, minutes


def _listing_url(raw_href: str | None, base_url: str, source_id: str) -> tuple[str | None, bool]:
    """Return a canonical Moscow listing URL and whether it belongs to Moscow."""
    if not raw_href:
        return None, False
    try:
        url = urljoin(base_url, raw_href)
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None, False
    if (
        parts.scheme != "https"
        or host not in _ALLOWED_HOSTS
        or parts.username
        or parts.password
        or port not in (None, 443)
    ):
        return None, False
    match = _LISTING_ID.search(parts.path)
    if not match or match.group(1) != source_id:
        return None, False
    if not re.match(r"^/[^/]+/kvartiry/", parts.path):
        return None, False
    if not parts.path.startswith(_MOSCOW_LISTING_PREFIX):
        return None, True
    canonical = urlunsplit(("https", host, parts.path, "", ""))
    return canonical, True


def _candidate(
    card: _RawCard,
    base_url: str,
    *,
    verified_no_commission: bool,
    observed_at: datetime | None,
) -> tuple[Candidate | None, bool]:
    source_id = card.source_id or ""
    if not re.fullmatch(r"\d{7,}", source_id):
        return None, False
    url, valid_public_listing = _listing_url(card.href, base_url, source_id)
    if not valid_public_listing:
        return None, False
    if url is None:
        # The Moscow search can surface nearby towns when a radius is selected;
        # do not report them as Moscow-city listings.
        return None, True

    title = _field(card, "title")
    price = _integer(_field(card, "price_value"))
    commission_text = _field(card, "specific_params")
    status, commission_value, commission_unit = _commission(commission_text)
    commission_evidence = (
        FieldEvidence.EXPLICIT_CARD
        if status is not CommissionStatus.UNKNOWN
        else FieldEvidence.UNKNOWN
    )
    has_unresolved_commission_claim = bool(
        commission_text and "комисси" in commission_text.casefold().replace("ё", "е")
    )
    if (
        status is CommissionStatus.UNKNOWN
        and verified_no_commission
        and not has_unresolved_commission_claim
    ):
        status = CommissionStatus.NONE
        commission_value = 0
        commission_evidence = FieldEvidence.VERIFIED_FILTER
    street, house = _field(card, "street"), _field(card, "house")
    address = (
        ", ".join(value.strip(" ,") for value in (street, house) if value)
        if street
        else _field(card, "address")
    )
    location_lines = [
        value
        for key in card.values
        if key.startswith("location_line_")
        if (value := _field(card, key)) is not None
    ]
    metro, metro_minutes = _metro(
        location_lines[-1] if len(location_lines) >= 2 else _field(card, "location")
    )
    return (
        Candidate(
            source="avito",
            source_id=source_id,
            url=url,
            price_rub=price,
            title=title,
            address=address,
            rooms=_rooms(title),
            area_m2=_area(title),
            metro=metro,
            metro_minutes=metro_minutes,
            seller_type=SellerType.UNKNOWN,
            commission_status=status,
            commission_value=commission_value,
            commission_unit=commission_unit,
            published_at=_published_at(
                card.published_machine,
                _field(card, "published_label"),
                observed_at,
            ),
            price_evidence=(
                FieldEvidence.EXPLICIT_CARD if price is not None else FieldEvidence.UNKNOWN
            ),
            rooms_evidence=(
                FieldEvidence.EXPLICIT_CARD if _rooms(title) is not None else FieldEvidence.UNKNOWN
            ),
            commission_evidence=commission_evidence,
        ),
        True,
    )


def parse_search_page(
    html: str,
    base_url: str,
    *,
    expected_city: str = "Москва",
    observed_at: datetime | None = None,
) -> SearchPageParse:
    """Parse explicitly marked listing cards from Avito's public search HTML."""
    reader = _AvitoCardReader()
    try:
        reader.feed(html)
        reader.close()
    except Exception:
        return SearchPageParse(recognized=False, candidates=[])
    outside_text = " ".join(" ".join(reader.outside_text).split())
    blocked_reason = page_block_reason(outside_text)
    if blocked_reason and not reader.cards:
        return SearchPageParse(recognized=False, candidates=[], blocked_reason=blocked_reason)
    try:
        base_parts = urlsplit(base_url)
    except ValueError:
        return SearchPageParse(
            recognized=False,
            candidates=[],
            blocked_reason="unexpected_search_route",
        )
    if (
        base_parts.scheme != "https"
        or (base_parts.hostname or "").lower() not in _ALLOWED_HOSTS
        or not base_parts.path.startswith(_SEARCH_PATH_PREFIX)
    ):
        return SearchPageParse(
            recognized=False,
            candidates=[],
            blocked_reason="unexpected_search_route",
        )

    if reader.invalid_structure:
        return SearchPageParse(recognized=False, candidates=[])

    heading = _clean(reader.page_values.get("heading", []))
    selected_filters = [
        value.casefold().replace("ё", "е")
        for value in reader.page_values.get("selected_filter", [])
        if value.strip()
    ]
    normalized_heading = (heading or "").casefold().replace("ё", "е")
    normalized_city = expected_city.casefold().replace("ё", "е")
    city_marker = "москв" if normalized_city == "москва" else normalized_city
    # The current desktop layout names the active no-commission search in its
    # visible H1, without filter-active buttons. Require route + heading + SERP,
    # never the configured URL alone, before supplying missing card evidence.
    visible_search_scope = (
        city_marker in normalized_heading
        and "длительн" in normalized_heading
        and "без комисси" in normalized_heading
        and reader.primary_seen
    )
    date_sort = "по дате" in " ".join(reader.page_values.get("sort", [])).casefold()
    context = AvitoSearchContext(
        recognized=reader.primary_seen and city_marker in normalized_heading,
        city=expected_city if city_marker in normalized_heading else None,
        long_term="длительн" in normalized_heading,
        no_commission=(base_parts.path.startswith(_SEARCH_PATH_PREFIX) and visible_search_scope),
        newest_first=(
            any("сначала новые" in value for value in selected_filters)
            or (date_sort and parse_qs(base_parts.query).get("s") == ["104"])
        ),
    )
    if not reader.cards:
        lowered = outside_text.casefold()
        if any(marker in lowered for marker in _EMPTY_MARKERS):
            return SearchPageParse(recognized=True, candidates=[], context=context)
        return SearchPageParse(recognized=False, candidates=[], context=context)

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for card in reader.cards:
        candidate, valid = _candidate(
            card,
            base_url,
            verified_no_commission=context.no_commission,
            observed_at=observed_at,
        )
        if not valid:
            return SearchPageParse(recognized=False, candidates=[])
        if candidate is None or candidate.source_id in seen:
            continue
        seen.add(candidate.source_id)
        candidates.append(candidate)
    return SearchPageParse(recognized=True, candidates=candidates, context=context)
