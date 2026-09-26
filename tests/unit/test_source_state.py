from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rent_monitor.core.source_state import (
    SourceOutcome,
    SourceRunHealth,
    SourceRunState,
    transition_source_state,
)
from rent_monitor.storage.sqlite import SQLiteRepository

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


class SourceStatePolicyTest(unittest.TestCase):
    def test_success_schedules_normal_interval_and_records_metrics(self) -> None:
        transition = transition_source_state(
            SourceRunState.initial("avito"),
            SourceOutcome.success(card_count=42, newest_id="1234567890"),
            NOW,
            normal_interval_seconds=60,
        )

        self.assertEqual(transition.current.health, SourceRunHealth.HEALTHY)
        self.assertEqual(transition.current.next_attempt_at, NOW + timedelta(seconds=60))
        self.assertEqual(transition.current.last_card_count, 42)
        self.assertEqual(transition.current.last_newest_id, "1234567890")

    def test_first_forbidden_enters_fifteen_minute_cooldown(self) -> None:
        transition = transition_source_state(
            SourceRunState.initial("avito"),
            SourceOutcome.failure("access_restricted"),
            NOW,
            normal_interval_seconds=60,
        )

        self.assertEqual(transition.current.health, SourceRunHealth.COOLDOWN)
        self.assertEqual(transition.current.consecutive_failures, 1)
        self.assertEqual(transition.current.next_attempt_at, NOW + timedelta(minutes=15))

    def test_third_forbidden_blocks_for_six_hours(self) -> None:
        previous = SourceRunState(
            source="avito",
            health=SourceRunHealth.COOLDOWN,
            consecutive_failures=2,
            failure_code="access_restricted",
            outage_started_at=NOW - timedelta(hours=1),
        )
        transition = transition_source_state(
            previous,
            SourceOutcome.failure("access_restricted"),
            NOW,
            normal_interval_seconds=60,
        )

        self.assertEqual(transition.current.health, SourceRunHealth.BLOCKED)
        self.assertEqual(transition.current.next_attempt_at, NOW + timedelta(hours=6))

    def test_captcha_has_no_automatic_next_attempt(self) -> None:
        transition = transition_source_state(
            SourceRunState.initial("avito"),
            SourceOutcome.failure("captcha"),
            NOW,
            normal_interval_seconds=60,
        )

        self.assertEqual(transition.current.health, SourceRunHealth.MANUAL_ATTENTION)
        self.assertIsNone(transition.current.next_attempt_at)

    def test_transport_backoff_is_bounded(self) -> None:
        expected = [30, 120, 300, 900, 1800, 3600, 3600]
        state = SourceRunState.initial("avito")
        now = NOW
        for seconds in expected:
            transition = transition_source_state(
                state,
                SourceOutcome.failure("transport_error"),
                now,
                normal_interval_seconds=60,
            )
            self.assertEqual(transition.current.next_attempt_at, now + timedelta(seconds=seconds))
            state = transition.current
            now += timedelta(hours=2)

    def test_rate_limit_honors_retry_after_but_caps_six_hours(self) -> None:
        transition = transition_source_state(
            SourceRunState.initial("avito"),
            SourceOutcome.failure("rate_limited", retry_after_seconds=99_999),
            NOW,
            normal_interval_seconds=60,
        )

        self.assertEqual(transition.current.health, SourceRunHealth.COOLDOWN)
        self.assertEqual(transition.current.next_attempt_at, NOW + timedelta(hours=6))

    def test_third_unknown_structure_blocks(self) -> None:
        previous = SourceRunState(
            source="avito",
            health=SourceRunHealth.DEGRADED,
            consecutive_failures=2,
            failure_code="unrecognized_structure",
        )
        transition = transition_source_state(
            previous,
            SourceOutcome.failure("unrecognized_structure"),
            NOW,
            normal_interval_seconds=60,
        )

        self.assertEqual(transition.current.health, SourceRunHealth.BLOCKED)
        self.assertEqual(transition.current.next_attempt_at, NOW + timedelta(hours=6))


class SourceStatePersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_state_survives_repository_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            expected = SourceRunState(
                source="avito",
                health=SourceRunHealth.COOLDOWN,
                consecutive_failures=2,
                failure_code="access_restricted",
                last_attempt_at=NOW,
                last_success_at=NOW - timedelta(minutes=3),
                next_attempt_at=NOW + timedelta(hours=1),
                transitioned_at=NOW,
                outage_started_at=NOW - timedelta(minutes=2),
                last_card_count=31,
                last_newest_id="9876543210",
            )
            repository = SQLiteRepository(path)
            await repository.initialize()
            await repository.save_source_run_state(expected)
            await repository.close()

            reopened = SQLiteRepository(path)
            await reopened.initialize()
            try:
                self.assertEqual(await reopened.get_source_run_state("avito"), expected)
                self.assertEqual(await reopened.list_source_run_states(), [expected])
            finally:
                await reopened.close()


if __name__ == "__main__":
    unittest.main()
