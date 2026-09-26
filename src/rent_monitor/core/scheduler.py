"""Sequential source scheduling, baseline seeding and outbox delivery."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from rent_monitor.core.filters import matches_listing
from rent_monitor.core.models import CollectionResult, Notification, SearchCriteria, SourceHealth
from rent_monitor.core.source_state import (
    SourceOutcome,
    SourceRunHealth,
    SourceTransition,
    transition_source_state,
)
from rent_monitor.storage.sqlite import SQLiteRepository

logger = logging.getLogger(__name__)


class Collector(Protocol):
    source: str

    async def collect(self, criteria: SearchCriteria, client: object) -> CollectionResult: ...


class Notifier(Protocol):
    async def send_notification(self, chat_id: int, notification: Notification) -> None: ...


@dataclass(frozen=True, slots=True)
class CollectorRuntime:
    collector: Collector
    client: object
    interval_seconds: float
    jitter_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self.jitter_seconds < 0:
            raise ValueError("jitter_seconds must be non-negative")


async def process_collection_result(
    result: CollectionResult,
    criteria: SearchCriteria,
    repository: SQLiteRepository,
) -> None:
    """Persist a source result and queue only newly discovered matches."""
    observed_at = result.observed_at or datetime.now(UTC)
    await repository.record_source_status(
        result.source,
        result.status,
        attempted_at=observed_at,
        successful_at=observed_at if result.status is SourceHealth.OK else None,
        failure_code=result.failure_code,
    )

    if result.status is not SourceHealth.OK or not result.recognized:
        return

    if not await repository.has_source_baseline(result.source):
        await repository.record_baseline_candidates(
            result.source,
            result.seen_source_ids or tuple(item.source_id for item in result.listings),
            observed_at=observed_at,
        )
        for listing in result.listings:
            await repository.upsert_listing(listing, notify=False)
        await repository.set_source_baseline(result.source, complete=True, completed_at=observed_at)
        logger.info("source=%s phase=baseline status=complete", result.source)
        return

    for listing in result.listings:
        if not matches_listing(listing, criteria):
            continue
        if await repository.is_baseline_candidate(result.source, listing.source_id):
            continue
        await repository.upsert_listing(listing, notify=True)


async def deliver_outbox_once(repository: SQLiteRepository, notifier: Notifier) -> int:
    """Attempt a batch of pending notifications for the paired owner chat."""
    chat_id = await repository.get_allowed_chat_id()
    if chat_id is None:
        return 0

    delivered = 0
    for notification in await repository.claim_pending_notifications(limit=20):
        try:
            await notifier.send_notification(chat_id, notification)
        except asyncio.CancelledError:
            await repository.retry_notification(
                notification.notification_id,
                "delivery_interrupted",
            )
            raise
        except Exception:
            logger.warning(
                "telegram phase=delivery status=retry notification_id=%s",
                notification.notification_id,
            )
            await repository.retry_notification(
                notification.notification_id, "telegram_send_failed"
            )
        else:
            await repository.mark_delivered(notification.notification_id)
            delivered += 1
    return delivered


async def run_outbox_worker(
    repository: SQLiteRepository,
    notifier: Notifier,
    stop_event: asyncio.Event,
    *,
    poll_seconds: float = 2.0,
) -> None:
    while not stop_event.is_set():
        await deliver_outbox_once(repository, notifier)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass


async def run_scheduler(
    collectors: Sequence[Collector],
    criteria: SearchCriteria,
    client: object,
    repository: SQLiteRepository,
    stop_event: asyncio.Event,
    *,
    poll_interval_seconds: int = 300,
    state_changed: asyncio.Event | None = None,
) -> None:
    """Compatibility wrapper that now runs every collector independently."""
    runtimes = tuple(
        CollectorRuntime(collector, client, poll_interval_seconds) for collector in collectors
    )
    await run_collectors(
        runtimes,
        criteria,
        repository,
        stop_event,
        state_changed=state_changed,
    )


async def run_collectors(
    runtimes: Sequence[CollectorRuntime],
    criteria: SearchCriteria,
    repository: SQLiteRepository,
    stop_event: asyncio.Event,
    *,
    state_changed: asyncio.Event | None = None,
    on_transition: Callable[[SourceTransition], Awaitable[None]] | None = None,
) -> None:
    """Run every source in its own supervised task."""
    if not runtimes:
        raise ValueError("At least one collector runtime must be configured")
    async with asyncio.TaskGroup() as tasks:
        for runtime in runtimes:
            tasks.create_task(
                run_source_runner(
                    runtime,
                    criteria,
                    repository,
                    stop_event,
                    state_changed=state_changed,
                    on_transition=on_transition,
                ),
                name=f"source-{runtime.collector.source}",
            )


async def run_source_runner(
    runtime: CollectorRuntime,
    criteria: SearchCriteria,
    repository: SQLiteRepository,
    stop_event: asyncio.Event,
    *,
    state_changed: asyncio.Event | None = None,
    on_transition: Callable[[SourceTransition], Awaitable[None]] | None = None,
) -> None:
    """Run one source without allowing it to delay another source."""
    while not stop_event.is_set():
        if await repository.is_paused():
            await _wait_for_wakeup(stop_event, state_changed, timeout=30.0)
            continue

        transition = await run_source_once(
            runtime,
            criteria,
            repository,
            on_transition=on_transition,
        )
        state = (
            transition.current
            if transition is not None
            else await repository.get_source_run_state(runtime.collector.source)
        )
        if state.health is SourceRunHealth.MANUAL_ATTENTION:
            await _wait_for_wakeup(stop_event, state_changed, timeout=None)
            continue
        now = datetime.now(UTC)
        delay = (
            max(0.0, (state.next_attempt_at - now).total_seconds())
            if state.next_attempt_at is not None
            else runtime.interval_seconds
        )
        await _wait_for_wakeup(stop_event, state_changed, timeout=delay)


async def run_source_once(
    runtime: CollectorRuntime,
    criteria: SearchCriteria,
    repository: SQLiteRepository,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    jitter: Callable[[float, float], float] = random.uniform,
    on_transition: Callable[[SourceTransition], Awaitable[None]] | None = None,
) -> SourceTransition | None:
    """Run at most one due collection attempt and persist its runtime state."""
    attempted_at = now()
    previous = await repository.get_source_run_state(runtime.collector.source)
    if previous.health is SourceRunHealth.MANUAL_ATTENTION:
        return None
    if previous.next_attempt_at is not None and previous.next_attempt_at > attempted_at:
        return None

    try:
        result = await runtime.collector.collect(criteria, runtime.client)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "source=%s phase=collect status=error category=%s",
            runtime.collector.source,
            _error_category(exc),
        )
        await repository.record_source_status(
            runtime.collector.source,
            SourceHealth.ERROR,
            attempted_at=attempted_at,
            failure_code="collector_error",
        )
        outcome = SourceOutcome.failure("collector_error")
    else:
        await process_collection_result(result, criteria, repository)
        if result.status is SourceHealth.OK and result.recognized:
            source_ids = result.seen_source_ids or tuple(
                listing.source_id for listing in result.listings
            )
            outcome = SourceOutcome.success(
                card_count=len(source_ids),
                newest_id=source_ids[0] if source_ids else None,
            )
        else:
            outcome = SourceOutcome.failure(
                result.failure_code or "collector_error",
                retry_after_seconds=result.retry_after_seconds,
            )

    transition = transition_source_state(
        previous,
        outcome,
        attempted_at,
        normal_interval_seconds=runtime.interval_seconds,
    )
    if outcome.ok and transition.current.next_attempt_at is not None and runtime.jitter_seconds:
        offset = jitter(-runtime.jitter_seconds, runtime.jitter_seconds)
        transition = SourceTransition(
            transition.previous,
            replace(
                transition.current,
                next_attempt_at=max(
                    attempted_at + timedelta(seconds=1),
                    transition.current.next_attempt_at + timedelta(seconds=offset),
                ),
            ),
        )
    await repository.save_source_run_state(transition.current)
    if transition.changed and on_transition is not None:
        await on_transition(transition)
    return transition


async def _wait_for_wakeup(
    stop_event: asyncio.Event,
    state_changed: asyncio.Event | None,
    *,
    timeout: float | None,
) -> None:
    waiters = [asyncio.create_task(stop_event.wait())]
    if state_changed is not None:
        waiters.append(asyncio.create_task(state_changed.wait()))
    _, pending = await asyncio.wait(
        waiters,
        timeout=timeout,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if state_changed is not None and state_changed.is_set():
        state_changed.clear()


def _error_category(error: Exception) -> str:
    name = error.__class__.__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "http" in name or "connect" in name:
        return "transport"
    return "unexpected"
