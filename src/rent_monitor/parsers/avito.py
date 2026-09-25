"""Fail-closed parser for explicitly structured public Avito HTML."""

import re

from rent_monitor.core.models import Candidate, Listing, SearchPageParse
from rent_monitor.parsers.common import parse_jsonld_detail_page, parse_jsonld_search_page


_ALLOWED_HOSTS = frozenset({"avito.ru", "www.avito.ru"})
_LISTING_ID = re.compile(r"(?:_|/)(\d{7,})(?:\.|/|$)")


def parse_search_page(html: str, base_url: str) -> SearchPageParse:
    """Parse only a Schema.org ItemList with public Avito listing links."""
    return parse_jsonld_search_page(
        html=html,
        base_url=base_url,
        source="avito",
        allowed_hosts=_ALLOWED_HOSTS,
        id_pattern=_LISTING_ID,
    )


def parse_detail_page(html: str, candidate: Candidate) -> Listing:
    """Enrich from matching public Schema.org data, preserving unknowns."""
    return parse_jsonld_detail_page(
        html=html,
        candidate=candidate,
        allowed_hosts=_ALLOWED_HOSTS,
        id_pattern=_LISTING_ID,
    )
