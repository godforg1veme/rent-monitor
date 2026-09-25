"""HTTP collector for the public Yandex Realty Moscow rental-search page."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

from rent_monitor.core.models import (
    Candidate,
    CollectionResult,
    Listing,
    SearchCriteria,
    SearchPageParse,
    SourceHealth,
)
from rent_monitor.parsers.yandex import (
    detect_blocked_page,
    parse_detail_page,
    parse_search_page,
)

if TYPE_CHECKING:
    from rent_monitor.transport import BoundedHttpClient


# This canonical path is linked by the public consumer site and applies the
# two-room plus no-commission filters. The monthly price ceiling stays in the
# shared filter because no public maximum-price path was confirmed.
SEARCH_URL = "https://realty.yandex.ru/moskva/snyat/kvartira/dvuhkomnatnaya/bez-komissii/"
_MIN_DETAIL_INTERVAL_SECONDS = 60.0
_MAX_PENDING_DETAILS = 50
_MAX_ATTEMPTED_DETAILS = 512


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


def _listing(candidate: Candidate) -> Listing:
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


def _merge_listing(base: Listing, detail: Listing) -> Listing:
    """Prefer non-null public detail fields, while preserving search-page data."""

    return Listing(
        source=base.source,
        source_id=base.source_id,
        url=base.url,
        price_rub=detail.price_rub if detail.price_rub is not None else base.price_rub,
        title=detail.title or base.title,
        address=detail.address or base.address,
        rooms=detail.rooms if detail.rooms is not None else base.rooms,
        area_m2=detail.area_m2 if detail.area_m2 is not None else base.area_m2,
        metro=detail.metro or base.metro,
        metro_minutes=detail.metro_minutes
        if detail.metro_minutes is not None
        else base.metro_minutes,
        seller_type=detail.seller_type if detail.seller_type != "unknown" else base.seller_type,
        commission_status=(
            detail.commission_status
            if detail.commission_status != "unknown"
            else base.commission_status
        ),
        commission_value=(
            detail.commission_value
            if detail.commission_value is not None
            else base.commission_value
        ),
        commission_unit=detail.commission_unit or base.commission_unit,
        published_at=detail.published_at or base.published_at,
    )


class YandexCollector:
    source = "yandex"

    def __init__(self) -> None:
        self._pending_details: deque[Candidate] = deque()
        self._queued_ids: set[str] = set()
        self._attempted_ids: set[str] = set()
        self._attempted_order: deque[str] = deque()
        self._last_detail_request_at: datetime | None = None
        self._paused_reason: str | None = None

    def resume_after_manual_fix(self) -> None:
        """Clear a source pause only after the public route/parser was reviewed."""

        self._paused_reason = None

    async def collect(
        self, criteria: SearchCriteria, client: BoundedHttpClient
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

        parsed: SearchPageParse = parse_search_page(html, SEARCH_URL)
        if parsed.blocked_reason:
            reason = "captcha" if parsed.blocked_reason == "captcha" else "access_restricted"
            self._paused_reason = reason
            return self._result([], SourceHealth.PAUSED, self._paused_reason, observed_at)
        if not parsed.recognized:
            return self._result([], SourceHealth.DEGRADED, "unrecognized_structure", observed_at)

        candidates = list(parsed.candidates)
        listings = {candidate.source_id: _listing(candidate) for candidate in candidates}
        self._enqueue_detail_candidates(candidates, criteria)

        detail_outcome = await self._enrich_one(listings, client)
        if detail_outcome == "paused":
            self._paused_reason = "access_restricted"
            return self._result([], SourceHealth.PAUSED, self._paused_reason, observed_at)
        if detail_outcome == "captcha":
            self._paused_reason = "captcha"
            return self._result([], SourceHealth.PAUSED, self._paused_reason, observed_at)
        if isinstance(detail_outcome, tuple) and detail_outcome[0] == "rate_limited":
            return self._result(
                list(listings.values()),
                SourceHealth.OK,
                None,
                observed_at,
                seen_source_ids=tuple(candidate.source_id for candidate in candidates),
                retry_after_seconds=detail_outcome[1],
            )

        return self._result(
            list(listings.values()),
            SourceHealth.OK,
            None,
            observed_at,
            seen_source_ids=tuple(candidate.source_id for candidate in candidates),
        )

    def _enqueue_detail_candidates(
        self, candidates: list[Candidate], criteria: SearchCriteria
    ) -> None:
        for candidate in candidates:
            if (
                candidate.source_id in self._queued_ids
                or candidate.source_id in self._attempted_ids
            ):
                continue
            if candidate.rooms != criteria.rooms:
                continue
            if candidate.price_rub is None or candidate.price_rub > criteria.max_monthly_price_rub:
                continue
            if candidate.commission_status != "unknown":
                continue
            if len(self._pending_details) >= _MAX_PENDING_DETAILS:
                return
            self._pending_details.append(candidate)
            self._queued_ids.add(candidate.source_id)

    async def _enrich_one(self, listings: dict[str, Listing], client: BoundedHttpClient):
        if not self._pending_details:
            return None
        now = datetime.now(UTC)
        if self._last_detail_request_at is not None:
            elapsed = (now - self._last_detail_request_at).total_seconds()
            if elapsed < _MIN_DETAIL_INTERVAL_SECONDS:
                return None

        candidate = self._pending_details.popleft()
        self._queued_ids.discard(candidate.source_id)
        self._remember_attempt(candidate.source_id)
        self._last_detail_request_at = now
        try:
            status_code, html, headers = await client.get_text(candidate.url)
        except Exception:
            return None
        if status_code in {401, 403}:
            return "paused"
        if status_code == 429:
            # The search page itself was recognized, so retain its baseline cohort
            # while asking the scheduler to back off subsequent source requests.
            return "rate_limited", _retry_after(headers) or 60.0
        if status_code != 200:
            return None
        blocked = detect_blocked_page(html)
        if blocked:
            return "captcha" if blocked == "captcha" else "paused"

        enriched = parse_detail_page(html, candidate)
        if enriched is not None and candidate.source_id in listings:
            listings[candidate.source_id] = _merge_listing(listings[candidate.source_id], enriched)
        return None

    def _remember_attempt(self, source_id: str) -> None:
        self._attempted_ids.add(source_id)
        self._attempted_order.append(source_id)
        while len(self._attempted_order) > _MAX_ATTEMPTED_DETAILS:
            expired = self._attempted_order.popleft()
            self._attempted_ids.discard(expired)

    def _result(
        self,
        listings: list[Listing],
        status: SourceHealth,
        failure_code: str | None,
        observed_at: datetime,
        *,
        seen_source_ids: tuple[str, ...] = (),
        retry_after_seconds: float | None = None,
    ) -> CollectionResult:
        return CollectionResult(
            source=self.source,
            listings=listings,
            status=status,
            failure_code=failure_code,
            observed_at=observed_at,
            seen_source_ids=seen_source_ids,
            retry_after_seconds=retry_after_seconds,
        )
