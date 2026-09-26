"""Browser-backed collector for configured public Avito searches."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

from rent_monitor.config import AvitoSearchConfig
from rent_monitor.core.models import CollectionResult, Listing, SearchCriteria, SourceHealth
from rent_monitor.parsers.avito import AvitoSearchContext, parse_search_page

if TYPE_CHECKING:
    from rent_monitor.browser.transport import PlaywrightBrowserTransport


SOURCE = "avito"


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
    """Render due search jobs sequentially and combine their visible cards."""

    source = SOURCE

    def __init__(
        self,
        searches: tuple[AvitoSearchConfig, ...],
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not searches:
            raise ValueError("At least one Avito search must be configured")
        self.searches = searches
        self.interval_seconds = min(search.poll_interval_seconds for search in searches)
        self._monotonic = monotonic
        self._next_due: dict[str, float] = {}
        self._seen_by_job: dict[str, tuple[str, ...]] = {}

    async def collect(
        self,
        criteria: SearchCriteria,
        client: PlaywrightBrowserTransport,
    ) -> CollectionResult:
        observed_at = datetime.now(UTC)
        now = self._monotonic()
        due = [search for search in self.searches if self._next_due.get(search.name, 0) <= now]
        if not due:
            return self._result(
                (),
                SourceHealth.OK,
                None,
                observed_at,
                seen_source_ids=self._all_seen_ids(),
            )

        listings: list[Listing] = []
        listed_ids: set[str] = set()
        for search in due:
            try:
                page = await client.fetch(search.url)
            except Exception:
                return self._result((), SourceHealth.ERROR, "transport_error", observed_at)

            status_code = page.status_code
            if status_code in {401, 403}:
                return self._result((), SourceHealth.PAUSED, "access_restricted", page.observed_at)
            if status_code == 429:
                return self._result(
                    (),
                    SourceHealth.DEGRADED,
                    "rate_limited",
                    page.observed_at,
                    retry_after_seconds=_retry_after(page.response_headers),
                )
            if status_code != 200:
                return self._result((), SourceHealth.ERROR, "http_error", page.observed_at)

            parsed = parse_search_page(
                page.html,
                page.final_url,
                expected_city=criteria.city,
                observed_at=page.observed_at,
            )
            if parsed.blocked_reason in {"captcha", "access_restricted"}:
                return self._result(
                    (),
                    SourceHealth.PAUSED,
                    parsed.blocked_reason,
                    page.observed_at,
                )
            context = parsed.context
            if (
                not parsed.recognized
                or not isinstance(context, AvitoSearchContext)
                or not context.recognized
                or (criteria.require_no_commission and not context.no_commission)
            ):
                return self._result(
                    (),
                    SourceHealth.DEGRADED,
                    "unrecognized_structure",
                    page.observed_at,
                )

            job_ids = tuple(candidate.source_id for candidate in parsed.candidates)
            self._seen_by_job[search.name] = job_ids
            self._next_due[search.name] = now + search.poll_interval_seconds
            for candidate in parsed.candidates:
                if candidate.source_id in listed_ids:
                    continue
                listed_ids.add(candidate.source_id)
                listings.append(candidate.to_listing())
            observed_at = page.observed_at

        return self._result(
            listings,
            SourceHealth.OK,
            None,
            observed_at,
            seen_source_ids=self._all_seen_ids(),
        )

    def _all_seen_ids(self) -> tuple[str, ...]:
        seen: set[str] = set()
        ordered: list[str] = []
        for search in self.searches:
            for source_id in self._seen_by_job.get(search.name, ()):
                if source_id not in seen:
                    seen.add(source_id)
                    ordered.append(source_id)
        return tuple(ordered)

    @staticmethod
    def _result(
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
