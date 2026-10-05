"""Pure source runtime-state transitions and bounded retry policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class SourceRunHealth(StrEnum):
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    COOLDOWN = "cooldown"
    BLOCKED = "blocked"
    MANUAL_ATTENTION = "manual_attention"


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    ok: bool
    failure_code: str | None = None
    retry_after_seconds: float | None = None
    card_count: int | None = None
    newest_id: str | None = None

    @classmethod
    def success(
        cls,
        *,
        card_count: int | None = None,
        newest_id: str | None = None,
    ) -> SourceOutcome:
        return cls(True, card_count=card_count, newest_id=newest_id)

    @classmethod
    def failure(
        cls,
        failure_code: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> SourceOutcome:
        return cls(False, failure_code, retry_after_seconds)


@dataclass(frozen=True, slots=True)
class SourceRunState:
    source: str
    health: SourceRunHealth
    consecutive_failures: int = 0
    failure_code: str | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    next_attempt_at: datetime | None = None
    transitioned_at: datetime | None = None
    outage_started_at: datetime | None = None
    last_card_count: int | None = None
    last_newest_id: str | None = None

    @classmethod
    def initial(cls, source: str) -> SourceRunState:
        return cls(source=source, health=SourceRunHealth.STARTING)

    @classmethod
    def manual_attention(cls, source: str, failure_code: str) -> SourceRunState:
        return cls(
            source=source,
            health=SourceRunHealth.MANUAL_ATTENTION,
            consecutive_failures=1,
            failure_code=failure_code,
        )


@dataclass(frozen=True, slots=True)
class SourceTransition:
    previous: SourceRunState
    current: SourceRunState

    @property
    def changed(self) -> bool:
        return self.previous.health is not self.current.health


def transition_source_state(
    previous: SourceRunState,
    outcome: SourceOutcome,
    now: datetime,
    *,
    normal_interval_seconds: int | float,
) -> SourceTransition:
    """Apply a deterministic retry policy to one collection outcome."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if normal_interval_seconds <= 0:
        raise ValueError("normal_interval_seconds must be positive")
    now = now.astimezone(UTC)

    if outcome.ok:
        current = SourceRunState(
            source=previous.source,
            health=SourceRunHealth.HEALTHY,
            last_attempt_at=now,
            last_success_at=now,
            next_attempt_at=now + timedelta(seconds=normal_interval_seconds),
            transitioned_at=(
                now if previous.health is not SourceRunHealth.HEALTHY else previous.transitioned_at
            ),
            last_card_count=(
                outcome.card_count if outcome.card_count is not None else previous.last_card_count
            ),
            last_newest_id=(
                outcome.newest_id if outcome.newest_id is not None else previous.last_newest_id
            ),
        )
        return SourceTransition(previous, current)

    failure_code = outcome.failure_code or "collector_error"
    failure_count = (
        previous.consecutive_failures + 1 if previous.failure_code == failure_code else 1
    )
    health, delay = _failure_schedule(
        failure_code,
        failure_count,
        outcome.retry_after_seconds,
    )
    current = replace(
        previous,
        health=health,
        consecutive_failures=failure_count,
        failure_code=failure_code,
        last_attempt_at=now,
        next_attempt_at=now + timedelta(seconds=delay) if delay is not None else None,
        transitioned_at=now if previous.health is not health else previous.transitioned_at,
        outage_started_at=previous.outage_started_at or now,
    )
    return SourceTransition(previous, current)


def _failure_schedule(
    failure_code: str,
    failure_count: int,
    retry_after_seconds: float | None,
) -> tuple[SourceRunHealth, float | None]:
    if failure_code == "home_route_unavailable":
        return SourceRunHealth.DEGRADED, 60
    if failure_code == "detail_unavailable":
        return SourceRunHealth.COOLDOWN, min(15 * 60 * failure_count, 60 * 60)
    if failure_code in {"captcha", "human_verification"}:
        return SourceRunHealth.MANUAL_ATTENTION, None

    if failure_code == "access_restricted":
        if failure_count == 1:
            return SourceRunHealth.COOLDOWN, 15 * 60
        if failure_count == 2:
            return SourceRunHealth.COOLDOWN, 60 * 60
        return SourceRunHealth.BLOCKED, 6 * 60 * 60

    if failure_code == "rate_limited":
        if retry_after_seconds is not None:
            return SourceRunHealth.COOLDOWN, min(max(retry_after_seconds, 0.0), 6 * 60 * 60)
        delays = (5 * 60, 15 * 60, 30 * 60, 60 * 60)
        return SourceRunHealth.COOLDOWN, delays[min(failure_count - 1, len(delays) - 1)]

    if failure_code == "unrecognized_structure":
        if failure_count >= 3:
            return SourceRunHealth.BLOCKED, 6 * 60 * 60
        return SourceRunHealth.DEGRADED, 5 * 60

    delays = (30, 2 * 60, 5 * 60, 15 * 60, 30 * 60, 60 * 60)
    return SourceRunHealth.DEGRADED, delays[min(failure_count - 1, len(delays) - 1)]
