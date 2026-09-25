"""Low-frequency HTTP collector for Avito's public long-term rental search."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

from rent_monitor.core.models import (
    CollectionResult,
    Listing,
    SearchCriteria,
    SourceHealth,
)
from rent_monitor.parsers.avito import parse_search_page

if TYPE_CHECKING:
    from rent_monitor.transport import BoundedHttpClient


SOURCE = "avito"
# Observed in Avito's public interface: Moscow, two rooms, long-term rent,
# no commission, sorted by date. The shared filter applies the 70,000 ₽ ceiling.
SEARCH_URL = (
    "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok/"
    "bez_komissii-ASgBAgICA0SSA8gQ8AeQUp74DgI?s=104"
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


class AvitoCollector:
    """Collect only visible fields from the ordinary public Avito result page."""

    source = SOURCE

    def __init__(self) -> None:
        self._paused_reason: str | None = None

    def resume_after_manual_fix(self) -> None:
        """Clear a source pause only after the public route/parser was reviewed."""
        self._paused_reason = None

    async def collect(
        self,
        criteria: SearchCriteria,
        client: BoundedHttpClient,
    ) -> CollectionResult:
        observed_at = datetime.now(UTC)
        if self._paused_reason:
            return self._result([], SourceHealth.PAUSED, self._paused_reason, observed_at)
        if criteria.city.strip().casefold() != "москва" or criteria.rooms != 2:
            return self._result([], SourceHealth.ERROR, "unsupported_search_criteria", observed_at)

        try:
            status_code, html, headers = await client.get_text(SEARCH_URL)
        except Exception:
            return self._result([], SourceHealth.ERROR, "transport_error", observed_at)

        if status_code in {401, 403}:
            self._paused_reason = "access_restricted"
            return self._result([], SourceHealth.PAUSED, self._paused_reason, observed_at)
        if status_code == 429:
            return self._result(
                [],
                SourceHealth.DEGRADED,
                "rate_limited",
                observed_at,
                retry_after_seconds=_retry_after(headers),
            )
        if status_code != 200:
            return self._result([], SourceHealth.ERROR, "http_error", observed_at)

        parsed = parse_search_page(html, SEARCH_URL)
        if parsed.blocked_reason in {"captcha", "access_restricted"}:
            self._paused_reason = parsed.blocked_reason
            return self._result([], SourceHealth.PAUSED, self._paused_reason, observed_at)
        if not parsed.recognized:
            return self._result([], SourceHealth.DEGRADED, "unrecognized_structure", observed_at)

        listings = tuple(candidate.to_listing() for candidate in parsed.candidates)
        return self._result(
            listings,
            SourceHealth.OK,
            None,
            observed_at,
            seen_source_ids=tuple(candidate.source_id for candidate in parsed.candidates),
        )

    def _result(
        self,
        listings: Sequence[Listing],
        status: SourceHealth,
        failure_code: str | None,
        observed_at: datetime,
        *,
        seen_source_ids: tuple[str, ...] = (),
        retry_after_seconds: float | None = None,
    ) -> CollectionResult:
        return CollectionResult(
            source=SOURCE,
            listings=tuple(listings),
            status=status,
            failure_code=failure_code,
            observed_at=observed_at,
            seen_source_ids=seen_source_ids,
            retry_after_seconds=retry_after_seconds,
        )
