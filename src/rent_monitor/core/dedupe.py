"""Conservative listing identity and cross-source duplicate matching."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence

from .models import Listing

_ADDRESS_ABBREVIATIONS = {
    "б-р": "бульвар",
    "бул": "бульвар",
    "вл": "владение",
    "д": "дом",
    "корп": "корпус",
    "к": "корпус",
    "лит": "литера",
    "мкр": "микрорайон",
    "наб": "набережная",
    "пер": "переулок",
    "пр": "проезд",
    "просп": "проспект",
    "пр-т": "проспект",
    "стр": "строение",
    "ул": "улица",
    "ш": "шоссе",
}
_TOKEN_RE = re.compile(r"[\w/.-]+", re.UNICODE)


def listing_identity(listing: Listing) -> tuple[str, str]:
    """Return the exact in-source identity required by the unique database key."""
    return listing.source, listing.source_id


def normalize_address(address: str) -> str:
    """Normalize typography and common abbreviations while preserving address numbers."""
    normalized = unicodedata.normalize("NFKC", address).casefold().replace("ё", "е")
    # Keep slash and hyphen between digits (for example 10/2 or 12-14), since
    # flattening these can make distinct house numbers appear identical.
    normalized = re.sub(r"(?<=\d)\s*([/-])\s*(?=\d)", r"\1", normalized)
    tokens = _TOKEN_RE.findall(normalized)
    canonical: list[str] = []
    for token in tokens:
        token = token.strip(".-/")
        if not token:
            continue
        canonical.append(_ADDRESS_ABBREVIATIONS.get(token, token))
    return " ".join(canonical)


def cross_source_match(left: Listing, right: Listing) -> bool:
    """Apply only the approved exact address/rooms/price and ±1 m² rule."""
    if left.source == right.source:
        return False
    if left.address is None or right.address is None:
        return False
    if (
        left.rooms is None
        or right.rooms is None
        or left.price_rub is None
        or right.price_rub is None
    ):
        return False
    if left.area_m2 is None or right.area_m2 is None:
        return False
    if not normalize_address(left.address) or normalize_address(left.address) != normalize_address(
        right.address
    ):
        return False
    return (
        left.rooms == right.rooms
        and left.price_rub == right.price_rub
        and abs(left.area_m2 - right.area_m2) <= 1.0
    )


def group_key_for_listing(listing: Listing) -> str:
    """Produce a stable group seed from a member identity, not a loose address key."""
    source, source_id = listing_identity(listing)
    digest = hashlib.sha256(f"{source}\0{source_id}".encode()).hexdigest()
    return f"listing:{digest}"


def find_duplicate_group(listing: Listing, candidates: Sequence[Listing]) -> str | None:
    """Return the first matching candidate's stable group seed, if any."""
    for candidate in candidates:
        if cross_source_match(listing, candidate):
            return group_key_for_listing(candidate)
    return None
