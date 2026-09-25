"""Normalized, source-independent models used by collectors and storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, StrEnum


class CommissionStatus(str, Enum):
    NONE = "none"
    POSITIVE = "positive"
    UNKNOWN = "unknown"


class SellerType(str, Enum):
    PRIVATE = "private"
    AGENCY = "agency"
    UNKNOWN = "unknown"


class SourceHealth(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    PAUSED = "paused"
    ERROR = "error"


def _enum_value(enum_type: type[Enum], value: Enum | str, field: str) -> Enum:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ValueError(f"{field} must be one of: {allowed}") from exc


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    city: str
    rooms: int
    max_monthly_price_rub: int
    require_no_commission: bool

    def __post_init__(self) -> None:
        if not self.city.strip():
            raise ValueError("city must not be empty")
        if isinstance(self.rooms, bool) or not isinstance(self.rooms, int) or self.rooms < 1:
            raise ValueError("rooms must be a positive integer")
        if (
            isinstance(self.max_monthly_price_rub, bool)
            or not isinstance(self.max_monthly_price_rub, int)
            or self.max_monthly_price_rub < 0
        ):
            raise ValueError("max_monthly_price_rub must be non-negative")


@dataclass(frozen=True, slots=True)
class Listing:
    source: str
    source_id: str
    url: str
    price_rub: int | None
    title: str | None
    address: str | None
    rooms: int | None
    area_m2: float | None
    metro: str | None
    metro_minutes: int | None
    seller_type: SellerType | str
    commission_status: CommissionStatus | str
    commission_value: int | None
    commission_unit: str | None
    published_at: datetime | None

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.source_id.strip() or not self.url.strip():
            raise ValueError("source, source_id, and url must not be empty")
        object.__setattr__(self, "seller_type", _enum_value(SellerType, self.seller_type, "seller_type"))
        object.__setattr__(
            self,
            "commission_status",
            _enum_value(CommissionStatus, self.commission_status, "commission_status"),
        )
        if self.price_rub is not None and (
            isinstance(self.price_rub, bool)
            or not isinstance(self.price_rub, int)
            or self.price_rub < 0
        ):
            raise ValueError("price_rub must be a non-negative integer or None")
        if self.rooms is not None and (
            isinstance(self.rooms, bool) or not isinstance(self.rooms, int) or self.rooms < 0
        ):
            raise ValueError("rooms must be a non-negative integer or None")
        if self.area_m2 is not None and self.area_m2 < 0:
            raise ValueError("area_m2 must be non-negative or None")
        if self.metro_minutes is not None and self.metro_minutes < 0:
            raise ValueError("metro_minutes must be non-negative or None")
        if self.commission_value is not None and (
            isinstance(self.commission_value, bool)
            or not isinstance(self.commission_value, int)
            or self.commission_value < 0
        ):
            raise ValueError("commission_value must be non-negative or None")


@dataclass(frozen=True, slots=True)
class Candidate:
    """Public search-result candidate; absent fields remain unknown until parsed."""

    source: str
    source_id: str
    url: str
    price_rub: int | None = None
    title: str | None = None
    address: str | None = None
    rooms: int | None = None
    area_m2: float | None = None
    metro: str | None = None
    metro_minutes: int | None = None
    seller_type: SellerType | str = SellerType.UNKNOWN
    commission_status: CommissionStatus | str = CommissionStatus.UNKNOWN
    commission_value: int | None = None
    commission_unit: str | None = None
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.source_id.strip() or not self.url.strip():
            raise ValueError("source, source_id, and url must not be empty")
        object.__setattr__(self, "seller_type", _enum_value(SellerType, self.seller_type, "seller_type"))
        object.__setattr__(
            self,
            "commission_status",
            _enum_value(CommissionStatus, self.commission_status, "commission_status"),
        )

    def to_listing(self) -> Listing:
        return Listing(
            source=self.source,
            source_id=self.source_id,
            url=self.url,
            price_rub=self.price_rub,
            title=self.title,
            address=self.address,
            rooms=self.rooms,
            area_m2=self.area_m2,
            metro=self.metro,
            metro_minutes=self.metro_minutes,
            seller_type=self.seller_type,
            commission_status=self.commission_status,
            commission_value=self.commission_value,
            commission_unit=self.commission_unit,
            published_at=self.published_at,
        )


@dataclass(frozen=True, slots=True)
class SearchPageParse:
    """Structured result of inspecting a public search page."""

    recognized: bool
    candidates: list[Candidate]
    blocked_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SourceStatusRecord:
    source: str
    health: SourceHealth | str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("source must not be empty")
        object.__setattr__(self, "health", _enum_value(SourceHealth, self.health, "health"))

    @property
    def status(self) -> SourceHealth:
        """Compatibility alias for consumers that display a `status` field."""
        return self.health  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class CollectionResult:
    source: str
    listings: tuple[Listing, ...] = ()
    status: SourceHealth | str = SourceHealth.OK
    recognized: bool = True
    failure_code: str | None = None
    observed_at: datetime | None = None
    seen_source_ids: tuple[str, ...] = ()
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("source must not be empty")
        object.__setattr__(self, "status", _enum_value(SourceHealth, self.status, "status"))
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative or None")


@dataclass(frozen=True, slots=True)
class Notification:
    notification_id: int
    group_id: str
    listing: Listing
    alternatives: tuple[Listing, ...] = ()
    attempts: int = 0
