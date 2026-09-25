"""Sequential source scheduling, baseline seeding and outbox delivery."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from rent_monitor.core.filters import matches_listing
from rent_monitor.core.models import CollectionResult, Notification, SearchCriteria, SourceHealth
from rent_monitor.storage.sqlite import SQLiteRepository

logger = logging.getLogger(__name__)


class Collector(Protocol):
    source: str

    async def collect(self, criteria: SearchCriteria, client: object) -> CollectionResult: ...


class Notifier(Protocol):
    async def send_notification(self, chat_id: int, notification: Notification) -> None: ...


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
    """Run collectors sequentially in staggered slots with per-source backoff."""
    if not collectors:
        raise ValueError("At least one collector must be configured")

    backoff_until: dict[str, float] = {}
    previous_backoff_seconds: dict[str, float] = {}
    last_attempt_at: dict[str, float] = {}
    next_cycle = time.monotonic()
    while not stop_event.is_set():
        cycle_started = time.monotonic()
        for index, collector in enumerate(collectors):
            if stop_event.is_set():
                break
            if await repository.is_paused():
                break

            slot = cycle_started + poll_interval_seconds * index / len(collectors)
            source_statuses = await repository.get_source_statuses()
            source_status = next(
                (status for status in source_statuses if status.source == collector.source), None
            )
            if source_status is not None and source_status.health is SourceHealth.PAUSED:
                continue
            if backoff_until.get(collector.source, 0.0) > time.monotonic():
                continue

            minimum_interval = (
                last_attempt_at.get(collector.source, float("-inf")) + poll_interval_seconds
            )
            deadline = max(slot, minimum_interval)
            while time.monotonic() < deadline and not stop_event.is_set():
                await _wait_until(deadline, stop_event, state_changed)
                if await repository.is_paused():
                    break
            if stop_event.is_set() or await repository.is_paused():
                break

            last_attempt_at[collector.source] = time.monotonic()
            try:
                result = await collector.collect(criteria, client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "source=%s phase=collect status=error category=%s",
                    collector.source,
                    _error_category(exc),
                )
                await repository.record_source_status(
                    collector.source,
                    SourceHealth.ERROR,
                    failure_code="collector_error",
                )
                continue

            await process_collection_result(result, criteria, repository)
            if result.retry_after_seconds is not None:
                delay = min(result.retry_after_seconds, 6 * 60 * 60)
                previous_backoff_seconds[collector.source] = delay
                backoff_until[collector.source] = time.monotonic() + delay
            elif result.failure_code == "rate_limited":
                delay = min(
                    max(
                        poll_interval_seconds,
                        previous_backoff_seconds.get(collector.source, 0) * 2,
                    ),
                    6 * 60 * 60,
                )
                previous_backoff_seconds[collector.source] = delay
                backoff_until[collector.source] = time.monotonic() + delay
            elif result.status is SourceHealth.OK:
                previous_backoff_seconds.pop(collector.source, None)
                backoff_until.pop(collector.source, None)

        next_cycle = max(cycle_started + poll_interval_seconds, time.monotonic())
        await _wait_until(next_cycle, stop_event, state_changed)


async def _wait_until(
    deadline: float,
    stop_event: asyncio.Event,
    state_changed: asyncio.Event | None,
) -> None:
    while not stop_event.is_set():
        delay = deadline - time.monotonic()
        if delay <= 0:
            return
        wait_events = [asyncio.create_task(stop_event.wait())]
        if state_changed is not None:
            wait_events.append(asyncio.create_task(state_changed.wait()))
        done, pending = await asyncio.wait(
            wait_events,
            timeout=min(delay, 30.0),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if state_changed is not None and any(task is wait_events[-1] for task in done):
            state_changed.clear()
            return


def _error_category(error: Exception) -> str:
    name = error.__class__.__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "http" in name or "connect" in name:
        return "transport"
    return "unexpected"
