"""Fail-closed Domclick collector.

The public root returned HTTP 401 during the design investigation, and no
ordinary public Moscow rental-search route has been established. Until one is
confirmed, this collector does not issue requests. It will not probe alternate
or private routes, authenticate, or retry around access restrictions.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from rent_monitor.core.models import (
    CollectionResult,
    Listing,
    SearchCriteria,
    SourceHealth,
)
from rent_monitor.parsers.domclick import parse_search_page

if TYPE_CHECKING:
    from rent_monitor.transport import BoundedHttpClient


class DomclickCollector:
    source = "domclick"

    def __init__(self, public_search_url: str | None = None) -> None:
        # No route is configured by default: the earlier 401 must not trigger
        # speculative path probing. A route may be supplied only after it has
        # been confirmed from Domclick's ordinary public consumer interface.
        parsed = urlparse(public_search_url or "")
        self.public_search_url = (
            public_search_url
            if parsed.scheme == "https" and parsed.hostname in {"domclick.ru", "www.domclick.ru"}
            else None
        )
        self._paused_reason: str | None = None

    def resume_after_manual_fix(self) -> None:
        """Clear a source pause only after the public route/parser was reviewed."""

        self._paused_reason = None

    async def collect(
        self, criteria: SearchCriteria, client: BoundedHttpClient
    ) -> CollectionResult:
        observed_at = datetime.now(UTC)
        if self._paused_reason:
            return self._result(SourceHealth.PAUSED, self._paused_reason, observed_at)
        if not self.public_search_url:
            return self._result(SourceHealth.PAUSED, "access_restricted", observed_at)
        if criteria.city.strip().casefold() != "москва" or criteria.rooms != 2:
            return self._result(SourceHealth.ERROR, "unsupported_search_criteria", observed_at)

        try:
            status_code, html, headers = await client.get_text(self.public_search_url)
        except Exception:
            return self._result(SourceHealth.ERROR, "transport_error", observed_at)

        if status_code in {401, 403}:
            self._paused_reason = "access_restricted"
            return self._result(SourceHealth.PAUSED, self._paused_reason, observed_at)
        if status_code == 429:
            return self._result(
                SourceHealth.DEGRADED,
                "rate_limited",
                observed_at,
                retry_after_seconds=_retry_after(headers),
            )
        if status_code != 200:
            return self._result(SourceHealth.ERROR, "http_error", observed_at)

        parsed = parse_search_page(html, self.public_search_url)
        if parsed.blocked_reason:
            self._paused_reason = parsed.blocked_reason
            return self._result(SourceHealth.PAUSED, self._paused_reason, observed_at)
        if not parsed.recognized:
            return self._result(SourceHealth.DEGRADED, "unrecognized_structure", observed_at)

        listings = [
            Listing(
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
            for candidate in parsed.candidates
        ]
        return CollectionResult(
            source=self.source,
            listings=listings,
            status=SourceHealth.OK,
            failure_code=None,
            observed_at=observed_at,
            seen_source_ids=tuple(candidate.source_id for candidate in parsed.candidates),
        )

    def _result(
        self,
        status: SourceHealth,
        failure_code: str | None,
        observed_at: datetime,
        *,
        retry_after_seconds: float | None = None,
    ) -> CollectionResult:
        return CollectionResult(
            source=self.source,
            listings=[],
            status=status,
            failure_code=failure_code,
            observed_at=observed_at,
            seen_source_ids=(),
            retry_after_seconds=retry_after_seconds,
        )


def _retry_after(headers: Mapping[str, str]) -> float | None:
    normalized = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    raw = normalized.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
