from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rent_monitor.core.scheduler import deliver_outbox_once
from rent_monitor.core.source_state import SourceRunHealth, SourceRunState, SourceTransition
from rent_monitor.storage.sqlite import SQLiteRepository


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class SourceAlertTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.clock = Clock(datetime(2026, 9, 26, 12, 0, tzinfo=UTC))
        self.path = Path(self.temporary_directory.name) / "alerts.sqlite3"
        self.repository = SQLiteRepository(self.path, clock=self.clock)
        await self.repository.initialize()

    async def asyncTearDown(self) -> None:
        await self.repository.close()
        self.temporary_directory.cleanup()

    def transition(
        self,
        previous_health: SourceRunHealth,
        current_health: SourceRunHealth,
        *,
        failure_code: str | None = None,
        failures: int = 1,
        outage_started_at: datetime | None = None,
    ) -> SourceTransition:
        previous = SourceRunState(
            source="avito",
            health=previous_health,
            last_success_at=self.clock.value - timedelta(minutes=10),
            outage_started_at=outage_started_at,
        )
        current = replace(
            previous,
            health=current_health,
            failure_code=failure_code,
            consecutive_failures=failures,
            last_attempt_at=self.clock.value,
            transitioned_at=self.clock.value,
            next_attempt_at=self.clock.value + timedelta(minutes=15),
            outage_started_at=outage_started_at or self.clock.value,
        )
        return SourceTransition(previous, current)

    async def test_transition_is_enqueued_once_and_retried(self) -> None:
        transition = self.transition(
            SourceRunHealth.HEALTHY,
            SourceRunHealth.MANUAL_ATTENTION,
            failure_code="captcha",
        )

        first = await self.repository.enqueue_source_transition(transition)
        second = await self.repository.enqueue_source_transition(transition)
        claimed = await self.repository.claim_pending_source_alerts(limit=10)
        await self.repository.retry_source_alert(claimed[0].alert_id, "telegram_send_failed")

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(await self.repository.claim_pending_source_alerts(limit=10), [])
        self.clock.advance(timedelta(seconds=3))
        self.assertEqual(len(await self.repository.claim_pending_source_alerts(limit=10)), 1)

    async def test_recovery_records_exact_outage_and_survives_restart(self) -> None:
        outage_started = self.clock.value - timedelta(minutes=17, seconds=9)
        transition = self.transition(
            SourceRunHealth.BLOCKED,
            SourceRunHealth.HEALTHY,
            outage_started_at=outage_started,
        )
        await self.repository.enqueue_source_transition(transition)
        await self.repository.close()

        self.repository = SQLiteRepository(self.path, clock=self.clock)
        await self.repository.initialize()
        alerts = await self.repository.claim_pending_source_alerts(limit=10)

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].health, SourceRunHealth.HEALTHY.value)
        self.assertEqual(alerts[0].outage_seconds, 17 * 60 + 9)

    async def test_ignores_startup_success_and_first_transient_failure(self) -> None:
        startup = self.transition(SourceRunHealth.STARTING, SourceRunHealth.HEALTHY)
        transient = self.transition(
            SourceRunHealth.HEALTHY,
            SourceRunHealth.DEGRADED,
            failure_code="transport_error",
            failures=1,
        )
        repeated = self.transition(
            SourceRunHealth.DEGRADED,
            SourceRunHealth.DEGRADED,
            failure_code="transport_error",
            failures=2,
        )

        self.assertFalse(await self.repository.enqueue_source_transition(startup))
        self.assertFalse(await self.repository.enqueue_source_transition(transient))
        self.assertTrue(await self.repository.enqueue_source_transition(repeated))

    async def test_delivery_drains_source_alert_queue(self) -> None:
        class Notifier:
            def __init__(self) -> None:
                self.alerts = []

            async def send_notification(self, chat_id, notification) -> None:
                raise AssertionError("listing queue should be empty")

            async def send_source_alert(self, chat_id, alert) -> None:
                self.alerts.append((chat_id, alert))

        await self.repository.bind_chat_id(4242)
        await self.repository.enqueue_source_transition(
            self.transition(
                SourceRunHealth.HEALTHY,
                SourceRunHealth.MANUAL_ATTENTION,
                failure_code="captcha",
            )
        )
        notifier = Notifier()

        delivered = await deliver_outbox_once(self.repository, notifier)

        self.assertEqual(delivered, 1)
        self.assertEqual(notifier.alerts[0][0], 4242)
        self.assertEqual(notifier.alerts[0][1].failure_code, "captcha")


if __name__ == "__main__":
    unittest.main()
