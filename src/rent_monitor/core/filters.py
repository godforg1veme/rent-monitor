"""Strict matching for the user's rental criteria."""

from .models import CommissionStatus, Listing, SearchCriteria


def matches_listing(listing: Listing, criteria: SearchCriteria) -> bool:
    """Return true only when every required value is known and satisfies criteria."""
    if listing.rooms is None or listing.rooms != criteria.rooms:
        return False
    if listing.price_rub is None or listing.price_rub > criteria.max_monthly_price_rub:
        return False
    if criteria.require_no_commission and listing.commission_status is not CommissionStatus.NONE:
        return False
    return True
