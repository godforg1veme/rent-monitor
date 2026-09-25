"""Fail-closed Avito collector for the public rental search page.

The public search route and its HTML format have not been confirmed. Until
they are verified from Avito's ordinary public interface, this collector
stays paused and performs no network requests.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rent_monitor.core.models import CollectionResult, SearchCriteria, SourceHealth

if TYPE_CHECKING:
    from rent_monitor.transport import BoundedHttpClient


SOURCE = "avito"
PAUSE_REASON = "search_route_unverified"


async def collect(
    criteria: SearchCriteria,
    client: "BoundedHttpClient",
) -> CollectionResult:
    """Return a paused status without probing an inferred Avito route."""
    # Keep the shared collector interface while intentionally making no request.
    del criteria, client
    return CollectionResult(
        source=SOURCE,
        listings=[],
        status=SourceHealth.PAUSED,
        failure_code=PAUSE_REASON,
        observed_at=datetime.now(UTC),
    )


class AvitoCollector:
    """Protocol-compatible collector for scheduler registration."""

    source = SOURCE

    async def collect(
        self,
        criteria: SearchCriteria,
        client: "BoundedHttpClient",
    ) -> CollectionResult:
        return await collect(criteria, client)
