from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from rent_monitor.core.models import CollectionResult, SearchCriteria, SourceHealth
from rent_monitor.core.scheduler import (
    CollectorRuntime,
    run_collectors,
    run_source_once,
)
from rent_monitor.core.source_state import SourceRunHealth, SourceRunState
from rent_monitor.storage.sqlite import SQLiteRepository

CRITERIA = SearchCriteria("Москва", 2, 70_000, True)


class RecordingCollector:
    def __init__(
        self,
        source: str,
        *,
        delay: float = 0.0,
        result: CollectionResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.source = source
        self.delay = delay
        self.result = result or CollectionResult(source=source)
        self.error = error
        self.calls = 0

    async def collect(self, criteria: SearchCriteria, client: object) -> CollectionResult:
        del criteria, client
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


class SourceRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repository = SQLiteRepository(":memory:")
        await self.repository.initialize()

    async def asyncTearDown(self) -> None:
        await self.repository.close()

    async def test_manual_attention_makes_no_network_call(self) -> None:
        await self.repository.save_source_run_state(
            SourceRunState.manual_attention("avito", "captcha")
        )
        collector = RecordingCollector("avito")
        runtime = CollectorRuntime(collector, object(), interval_seconds=60, jitter_seconds=0)

        transition = await run_source_once(runtime, CRITERIA, self.repository)

        self.assertIsNone(transition)
        self.assertEqual(collector.calls, 0)

    async def test_future_attempt_makes_no_network_call(self) -> None:
        now = datetime.now(UTC)
        await self.repository.save_source_run_state(
            SourceRunState(
                source="avito",
                health=SourceRunHealth.COOLDOWN,
                next_attempt_at=now + timedelta(hours=1),
            )
        )
        collector = RecordingCollector("avito")
        runtime = CollectorRuntime(collector, object(), interval_seconds=60, jitter_seconds=0)

        transition = await run_source_once(
            runtime,
            CRITERIA,
            self.repository,
            now=lambda: now,
        )

        self.assertIsNone(transition)
        self.assertEqual(collector.calls, 0)

    async def test_exception_becomes_persisted_degraded_state(self) -> None:
        collector = RecordingCollector("avito", error=RuntimeError("browser closed"))
        runtime = CollectorRuntime(collector, object(), interval_seconds=60, jitter_seconds=0)

        transition = await run_source_once(runtime, CRITERIA, self.repository)

        self.assertIsNotNone(transition)
        state = await self.repository.get_source_run_state("avito")
        self.assertEqual(state.health, SourceRunHealth.DEGRADED)
        self.assertEqual(state.failure_code, "collector_error")

    async def test_slow_source_does_not_delay_fast_source(self) -> None:
        slow = RecordingCollector("yandex", delay=1.00)
        fast = RecordingCollector("avito")
        stop_event = asyncio.Event()

        async def stop_later() -> None:
            await asyncio.sleep(0.60)
            stop_event.set()

        stopper = asyncio.create_task(stop_later())
        try:
            await run_collectors(
                (
                    CollectorRuntime(slow, object(), interval_seconds=0.05, jitter_seconds=0),
                    CollectorRuntime(fast, object(), interval_seconds=0.02, jitter_seconds=0),
                ),
                CRITERIA,
                self.repository,
                stop_event,
            )
        finally:
            await stopper

        self.assertEqual(slow.calls, 1)
        self.assertGreaterEqual(fast.calls, 2)

    async def test_result_status_drives_runtime_failure(self) -> None:
        collector = RecordingCollector(
            "avito",
            result=CollectionResult(
                source="avito",
                status=SourceHealth.PAUSED,
                failure_code="access_restricted",
            ),
        )
        runtime = CollectorRuntime(collector, object(), interval_seconds=60, jitter_seconds=0)

        await run_source_once(runtime, CRITERIA, self.repository)

        state = await self.repository.get_source_run_state("avito")
        self.assertEqual(state.health, SourceRunHealth.COOLDOWN)
        self.assertEqual(state.consecutive_failures, 1)


if __name__ == "__main__":
    unittest.main()
