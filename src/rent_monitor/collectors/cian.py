"""Fail-closed Cian collector for the public rental search page.

The ordinary public search page identified during research returned HTTP 403
to a direct page fetch. The collector therefore remains paused and does not
retry or look for alternate routes.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from rent_monitor.core.models import CollectionResult, SearchCriteria, SourceHealth

if TYPE_CHECKING:
    from rent_monitor.transport import BoundedHttpClient


SOURCE = "cian"
PUBLIC_SEARCH_URL = "https://www.cian.ru/snyat-2-komnatnuyu-kvartiru/"
PAUSE_REASON = "public_search_access_blocked"


async def collect(
    criteria: SearchCriteria,
    client: "BoundedHttpClient",
) -> CollectionResult:
    """Return a paused status after the public page returned HTTP 403."""
    # The observed 403 is treated as an access restriction; make no further
    # request and do not try a different route or hidden endpoint.
    del criteria, client
    return CollectionResult(
        source=SOURCE,
        listings=[],
        status=SourceHealth.PAUSED,
        failure_code=PAUSE_REASON,
        observed_at=datetime.now(timezone.utc),
    )


class CianCollector:
    """Protocol-compatible collector for scheduler registration."""

    source = SOURCE

    async def collect(
        self,
        criteria: SearchCriteria,
        client: "BoundedHttpClient",
    ) -> CollectionResult:
        return await collect(criteria, client)
