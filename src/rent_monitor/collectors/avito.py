"""Browser-backed collector for configured public Avito searches."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

from rent_monitor.config import AvitoSearchConfig
from rent_monitor.core.filters import matches_listing
from rent_monitor.core.models import CollectionResult, Listing, SearchCriteria, SourceHealth
from rent_monitor.parsers.avito import AvitoSearchContext, parse_search_page
from rent_monitor.parsers.avito_detail import parse_detail

if TYPE_CHECKING:
    from rent_monitor.browser.transport import BrowserTransport


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
        self.repository = None
        self.detail_cache_directory = None

    async def collect(
        self,
        criteria: SearchCriteria,
        client: BrowserTransport,
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
            except Exception as exc:
                from rent_monitor.browser.home_pow import HomeRouteUnavailable

                if isinstance(exc, HomeRouteUnavailable):
                    return self._result(
                        (), SourceHealth.DEGRADED, "home_route_unavailable", observed_at
                    )
                return self._result((), SourceHealth.ERROR, "transport_error", observed_at)

            status_code = page.status_code
            parsed = parse_search_page(
                page.html,
                page.final_url,
                expected_city=criteria.city,
                observed_at=page.observed_at,
            )
            if parsed.blocked_reason == "captcha":
                return self._result((), SourceHealth.PAUSED, "captcha", page.observed_at)
            if parsed.blocked_reason == "browser_verification" or status_code == 439:
                return self._result((), SourceHealth.PAUSED, "human_verification", page.observed_at)
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

        detail_failure = False
        if self.repository is not None and os.environ.get("RENT_MONITOR_COLLECT_DETAILS") == "1":
            baseline = await self.repository.has_source_baseline(SOURCE)
            enriched = []
            detail_requests = 0
            for listing in listings:
                existing = await self.repository.get_listing(SOURCE, listing.source_id)
                if existing is not None and existing.details:
                    enriched.append(replace(listing, details=existing.details))
                    continue
                if not baseline or not matches_listing(listing, criteria):
                    enriched.append(listing)
                    continue
                if await self.repository.is_baseline_candidate(SOURCE, listing.source_id):
                    enriched.append(listing)
                    continue
                cache_path = self.detail_cache_directory / f"{listing.source_id}.json"
                details = None
                if cache_path.exists():
                    try:
                        details = json.loads(cache_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        pass
                if details is None:
                    if detail_requests >= 5:
                        continue  # Remains absent from DB, so the next cycle retries.
                    detail_requests += 1
                    try:
                        await asyncio.sleep(15)
                        if hasattr(client, "fetch_listing"):
                            detail_page = await client.fetch_listing(listing.source_id, listing.url)
                        else:
                            detail_page = await client.fetch(listing.url)
                        details = parse_detail(
                            detail_page.html, listing.source_id, detail_page.final_url
                        )
                    except Exception as exc:
                        from rent_monitor.browser.home_pow import HomeRouteUnavailable

                        if isinstance(exc, HomeRouteUnavailable):
                            return self._result(
                                (),
                                SourceHealth.DEGRADED,
                                "home_route_unavailable",
                                observed_at,
                                seen_source_ids=self._all_seen_ids(),
                            )
                        details = None
                    if details is None:
                        detail_failure = True
                        logging.getLogger(__name__).warning(
                            "source=avito phase=detail status=unavailable id=%s", listing.source_id
                        )
                        break  # Stop paid detail requests until the next cycle.
                    self.detail_cache_directory.mkdir(parents=True, exist_ok=True)
                    temporary = cache_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(details, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(cache_path)
                label = details.get("seller_type_label", "").casefold()
                from rent_monitor.core.models import SellerType

                seller_type = (
                    SellerType.AGENCY
                    if "агент" in label
                    else SellerType.PRIVATE
                    if "частн" in label
                    else listing.seller_type
                )
                enriched.append(replace(listing, details=details, seller_type=seller_type))
            listings = enriched

        if detail_failure:
            return self._result(
                (),
                SourceHealth.DEGRADED,
                "detail_unavailable",
                observed_at,
                seen_source_ids=self._all_seen_ids(),
            )

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
