"""Async SQLite repository with conservative deduplication and a durable outbox."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Iterable

import aiosqlite

from rent_monitor.core.dedupe import cross_source_match, find_duplicate_group, group_key_for_listing, normalize_address
from rent_monitor.core.models import (
    CommissionStatus,
    Listing,
    Notification,
    SellerType,
    SourceHealth,
    SourceStatusRecord,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS listing_groups (
    group_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    group_id TEXT NOT NULL REFERENCES listing_groups(group_id),
    url TEXT NOT NULL,
    price_rub INTEGER,
    title TEXT,
    address TEXT,
    address_norm TEXT,
    rooms INTEGER,
    area_m2 REAL,
    metro TEXT,
    metro_minutes INTEGER,
    seller_type TEXT NOT NULL,
    commission_status TEXT NOT NULL,
    commission_value INTEGER,
    commission_unit TEXT,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS listings_dedupe_idx
    ON listings(address_norm, rooms, price_rub, area_m2, first_seen_at);
CREATE INDEX IF NOT EXISTS listings_group_idx ON listings(group_id, first_seen_at);

CREATE TABLE IF NOT EXISTS baseline_candidates (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE TABLE IF NOT EXISTS source_baselines (
    source TEXT PRIMARY KEY,
    complete INTEGER NOT NULL DEFAULT 0 CHECK (complete IN (0, 1)),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS source_status (
    source TEXT PRIMARY KEY,
    health TEXT NOT NULL CHECK (health IN ('ok', 'degraded', 'paused', 'error')),
    last_attempt_at TEXT,
    last_success_at TEXT,
    failure_code TEXT
);

CREATE TABLE IF NOT EXISTS owner_binding (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    allowed_chat_id INTEGER UNIQUE,
    pairing_code_hash TEXT,
    pairing_expires_at TEXT
);
INSERT OR IGNORE INTO owner_binding(singleton) VALUES (1);

CREATE TABLE IF NOT EXISTS application_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1))
);
INSERT OR IGNORE INTO application_state(singleton) VALUES (1);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL UNIQUE REFERENCES listing_groups(group_id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'delivered')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    claimed_until TEXT,
    delivered_at TEXT,
    last_error_code TEXT
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx ON notification_outbox(status, created_at);
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _encode_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _decode_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _valid_failure_code(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", value):
        raise ValueError("failure_code must be a short closed-category code")
    return value


def _listing_values(listing: Listing) -> tuple[object, ...]:
    return (
        listing.source,
        listing.source_id,
        listing.url,
        listing.price_rub,
        listing.title,
        listing.address,
        normalize_address(listing.address) if listing.address else None,
        listing.rooms,
        listing.area_m2,
        listing.metro,
        listing.metro_minutes,
        listing.seller_type.value,
        listing.commission_status.value,
        listing.commission_value,
        listing.commission_unit,
        _encode_datetime(listing.published_at),
    )


def _listing_from_row(row: aiosqlite.Row) -> Listing:
    return Listing(
        source=row["source"],
        source_id=row["source_id"],
        url=row["url"],
        price_rub=row["price_rub"],
        title=row["title"],
        address=row["address"],
        rooms=row["rooms"],
        area_m2=row["area_m2"],
        metro=row["metro"],
        metro_minutes=row["metro_minutes"],
        seller_type=SellerType(row["seller_type"]),
        commission_status=CommissionStatus(row["commission_status"]),
        commission_value=row["commission_value"],
        commission_unit=row["commission_unit"],
        published_at=_decode_datetime(row["published_at"]),
    )


class SQLiteRepository:
    """Owns one SQLite connection; all mutations use explicit write transactions.

    `upsert_listing(..., notify=True)` is the atomic path for a newly accepted
    listing: normalized listing, duplicate group, and outbox row commit together.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._connection is not None:
            return
        if self.database_path != ":memory:":
            Path(self.database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.database_path, isolation_level=None)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 5000")
        await connection.execute("PRAGMA journal_mode = WAL")
        await connection.executescript(_SCHEMA)
        self._connection = connection

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    def _db(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("SQLiteRepository.initialize() must be called first")
        return self._connection

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize this repository's operations and commit or roll back atomically."""
        async with self._lock:
            connection = self._db()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()

    async def upsert_listing(self, listing: Listing, *, notify: bool = False) -> tuple[str, bool]:
        """Insert/update a listing, optionally enqueueing it in the same transaction.

        Returns `(group_id, is_new_source_listing)`. Existing listings are refreshed
        without creating a second notification.
        """
        async with self._transaction() as connection:
            group_id, is_new, _ = await self._upsert_listing(connection, listing, notify=notify)
            return group_id, is_new

    async def _upsert_listing(
        self, connection: aiosqlite.Connection, listing: Listing, *, notify: bool
    ) -> tuple[str, bool, bool]:
        existing_cursor = await connection.execute(
            "SELECT group_id FROM listings WHERE source = ? AND source_id = ?",
            (listing.source, listing.source_id),
        )
        existing = await existing_cursor.fetchone()
        await existing_cursor.close()
        now = _now().isoformat()
        values = _listing_values(listing)

        if existing is not None:
            group_id = existing["group_id"]
            await connection.execute(
                """UPDATE listings SET url=?, price_rub=?, title=?, address=?, address_norm=?,
                   rooms=?, area_m2=?, metro=?, metro_minutes=?, seller_type=?,
                   commission_status=?, commission_value=?, commission_unit=?, published_at=?,
                   last_seen_at=? WHERE source=? AND source_id=?""",
                (*values[2:], now, listing.source, listing.source_id),
            )
            is_new = False
        else:
            group_id = await self._select_group_for(connection, listing)
            await connection.execute(
                "INSERT OR IGNORE INTO listing_groups(group_id, created_at) VALUES (?, ?)",
                (group_id, now),
            )
            await connection.execute(
                """INSERT INTO listings(
                   source, source_id, group_id, url, price_rub, title, address, address_norm,
                   rooms, area_m2, metro, metro_minutes, seller_type, commission_status,
                   commission_value, commission_unit, published_at, first_seen_at, last_seen_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (listing.source, listing.source_id, group_id, *values[2:], now, now),
            )
            is_new = True

        outbox_created = False
        if notify:
            cursor = await connection.execute(
                """INSERT INTO notification_outbox(group_id, created_at)
                   VALUES (?, ?) ON CONFLICT(group_id) DO NOTHING""",
                (group_id, now),
            )
            outbox_created = cursor.rowcount == 1
            await cursor.close()
        return group_id, is_new, outbox_created

    async def _select_group_for(self, connection: aiosqlite.Connection, listing: Listing) -> str:
        normalized = normalize_address(listing.address) if listing.address else ""
        if normalized and listing.rooms is not None and listing.price_rub is not None and listing.area_m2 is not None:
            cursor = await connection.execute(
                """SELECT * FROM listings
                   WHERE source <> ? AND address_norm = ? AND rooms = ? AND price_rub = ?
                     AND area_m2 BETWEEN ? AND ?
                   ORDER BY first_seen_at, source, source_id""",
                (
                    listing.source,
                    normalized,
                    listing.rooms,
                    listing.price_rub,
                    listing.area_m2 - 1.0,
                    listing.area_m2 + 1.0,
                ),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            candidates = [_listing_from_row(row) for row in rows]
            duplicate_key = find_duplicate_group(listing, candidates)
            if duplicate_key is not None:
                for row, candidate in zip(rows, candidates):
                    if cross_source_match(listing, candidate):
                        return row["group_id"]
        return group_key_for_listing(listing)

    async def enqueue_notification(self, group_id: str, listing: Listing) -> bool:
        """Atomically persist a listing and create its group's outbox item once."""
        async with self._transaction() as connection:
            actual_group_id, _, created = await self._upsert_listing(connection, listing, notify=True)
            if actual_group_id != group_id:
                raise ValueError("group_id does not match the listing's persisted duplicate group")
            return created

    async def claim_pending_notifications(self, limit: int = 20) -> list[Notification]:
        if limit < 1:
            return []
        now = _now()
        claimed_until = now + timedelta(minutes=5)
        async with self._transaction() as connection:
            cursor = await connection.execute(
                """SELECT id, group_id, attempts FROM notification_outbox
                   WHERE status = 'pending' OR (status = 'sending' AND claimed_until < ?)
                   ORDER BY created_at, id LIMIT ?""",
                (now.isoformat(), limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            notifications: list[Notification] = []
            for row in rows:
                await connection.execute(
                    """UPDATE notification_outbox
                       SET status='sending', attempts=attempts+1, claimed_until=?
                       WHERE id=?""",
                    (claimed_until.isoformat(), row["id"]),
                )
                listing_cursor = await connection.execute(
                    """SELECT * FROM listings WHERE group_id=?
                       ORDER BY first_seen_at, source, source_id""",
                    (row["group_id"],),
                )
                listing_rows = await listing_cursor.fetchall()
                await listing_cursor.close()
                if not listing_rows:
                    # Keep the queue record for diagnosis/recovery; it will not be
                    # delivered without a normalized listing to render.
                    continue
                group_listings = tuple(_listing_from_row(item) for item in listing_rows)
                notifications.append(
                    Notification(
                        notification_id=row["id"],
                        group_id=row["group_id"],
                        listing=group_listings[0],
                        alternatives=group_listings[1:],
                        attempts=row["attempts"] + 1,
                    )
                )
            return notifications

    async def mark_delivered(self, notification_id: int) -> None:
        async with self._transaction() as connection:
            await connection.execute(
                """UPDATE notification_outbox
                   SET status='delivered', delivered_at=?, claimed_until=NULL, last_error_code=NULL
                   WHERE id=?""",
                (_now().isoformat(), notification_id),
            )

    async def retry_notification(self, notification_id: int, error_code: str) -> None:
        """Return a failed send to the queue with a closed-category error code."""
        safe_code = _valid_failure_code(error_code)
        async with self._transaction() as connection:
            await connection.execute(
                """UPDATE notification_outbox
                   SET status='pending', claimed_until=NULL, last_error_code=? WHERE id=?""",
                (safe_code, notification_id),
            )

    async def find_duplicate_candidates(self, listing: Listing) -> list[Listing]:
        normalized = normalize_address(listing.address) if listing.address else ""
        if not normalized or listing.rooms is None or listing.price_rub is None or listing.area_m2 is None:
            return []
        async with self._lock:
            cursor = await self._db().execute(
                """SELECT * FROM listings
                   WHERE source <> ? AND address_norm = ? AND rooms = ? AND price_rub = ?
                     AND area_m2 BETWEEN ? AND ?
                   ORDER BY first_seen_at, source, source_id""",
                (
                    listing.source,
                    normalized,
                    listing.rooms,
                    listing.price_rub,
                    listing.area_m2 - 1.0,
                    listing.area_m2 + 1.0,
                ),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [item for row in rows if cross_source_match(listing, item := _listing_from_row(row))]

    async def record_baseline_candidates(
        self, source: str, source_ids: Iterable[str], *, observed_at: datetime | None = None
    ) -> None:
        timestamp = (observed_at or _now()).isoformat()
        async with self._transaction() as connection:
            await connection.executemany(
                """INSERT INTO baseline_candidates(source, source_id, observed_at)
                   VALUES (?, ?, ?) ON CONFLICT(source, source_id) DO NOTHING""",
                ((source, source_id, timestamp) for source_id in set(source_ids)),
            )

    async def set_source_baseline(
        self, source: str, complete: bool = True, *, completed_at: datetime | None = None
    ) -> None:
        timestamp = (completed_at or _now()).isoformat() if complete else None
        async with self._transaction() as connection:
            await connection.execute(
                """INSERT INTO source_baselines(source, complete, completed_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(source) DO UPDATE SET complete=excluded.complete,
                     completed_at=excluded.completed_at""",
                (source, int(complete), timestamp),
            )

    async def has_source_baseline(self, source: str) -> bool:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT complete FROM source_baselines WHERE source=?", (source,)
            )
            row = await cursor.fetchone()
            await cursor.close()
        return bool(row and row["complete"])

    async def is_baseline_candidate(self, source: str, source_id: str) -> bool:
        """Return whether an ID was seen in that source's initial search cohort.

        The scheduler can use this before delayed detail-page enrichment so an
        initial baseline listing is never treated as a newly discovered result.
        """
        async with self._lock:
            cursor = await self._db().execute(
                """SELECT 1 FROM baseline_candidates
                   WHERE source=? AND source_id=? LIMIT 1""",
                (source, source_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return row is not None

    async def record_source_status(
        self,
        source: str,
        status: SourceHealth | str,
        *,
        attempted_at: datetime | None = None,
        successful_at: datetime | None = None,
        failure_code: str | None = None,
    ) -> None:
        health = status if isinstance(status, SourceHealth) else SourceHealth(status)
        safe_code = _valid_failure_code(failure_code)
        attempted_at = attempted_at or _now()
        successful_at = (successful_at or attempted_at) if health is SourceHealth.OK else None
        async with self._transaction() as connection:
            await connection.execute(
                """INSERT INTO source_status(source, health, last_attempt_at, last_success_at, failure_code)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source) DO UPDATE SET health=excluded.health,
                     last_attempt_at=excluded.last_attempt_at,
                     last_success_at=COALESCE(excluded.last_success_at, source_status.last_success_at),
                     failure_code=excluded.failure_code""",
                (
                    source,
                    health.value,
                    attempted_at.isoformat(),
                    successful_at.isoformat() if successful_at is not None else None,
                    safe_code,
                ),
            )

    async def get_source_statuses(self) -> list[SourceStatusRecord]:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT * FROM source_status ORDER BY source"
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [
            SourceStatusRecord(
                source=row["source"],
                health=SourceHealth(row["health"]),
                last_attempt_at=_decode_datetime(row["last_attempt_at"]),
                last_success_at=_decode_datetime(row["last_success_at"]),
                failure_code=row["failure_code"],
            )
            for row in rows
        ]

    async def issue_pairing_code(self, *, ttl_seconds: int = 600) -> str:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        code = secrets.token_urlsafe(24)
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        expires_at = (_now() + timedelta(seconds=ttl_seconds)).isoformat()
        async with self._transaction() as connection:
            cursor = await connection.execute(
                "SELECT allowed_chat_id FROM owner_binding WHERE singleton=1"
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row["allowed_chat_id"] is not None:
                raise RuntimeError("owner chat is already bound")
            await connection.execute(
                """UPDATE owner_binding SET pairing_code_hash=?, pairing_expires_at=?
                   WHERE singleton=1""",
                (code_hash, expires_at),
            )
        return code

    async def consume_pairing_code(self, code: str, chat_id: int) -> bool:
        if chat_id <= 0:
            return False
        candidate_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        now = _now()
        async with self._transaction() as connection:
            cursor = await connection.execute(
                """SELECT allowed_chat_id, pairing_code_hash, pairing_expires_at
                   FROM owner_binding WHERE singleton=1"""
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row["allowed_chat_id"] is not None or row["pairing_code_hash"] is None:
                return False
            expiry = _decode_datetime(row["pairing_expires_at"])
            if expiry is None or expiry <= now or not hmac.compare_digest(
                candidate_hash, row["pairing_code_hash"]
            ):
                return False
            await connection.execute(
                """UPDATE owner_binding SET allowed_chat_id=?, pairing_code_hash=NULL,
                   pairing_expires_at=NULL WHERE singleton=1 AND allowed_chat_id IS NULL""",
                (chat_id,),
            )
            return True

    async def bind_chat_id(self, chat_id: int) -> None:
        """Bind the sole owner chat; intended for trusted pairing-flow code only."""
        if chat_id <= 0:
            raise ValueError("chat_id must be a positive private-chat identifier")
        async with self._transaction() as connection:
            cursor = await connection.execute(
                "UPDATE owner_binding SET allowed_chat_id=? WHERE singleton=1 AND allowed_chat_id IS NULL",
                (chat_id,),
            )
            inserted = cursor.rowcount == 1
            await cursor.close()
            if not inserted:
                raise RuntimeError("owner chat is already bound")

    async def get_allowed_chat_id(self) -> int | None:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT allowed_chat_id FROM owner_binding WHERE singleton=1"
            )
            row = await cursor.fetchone()
            await cursor.close()
        return row["allowed_chat_id"] if row else None

    async def set_paused(self, paused: bool) -> None:
        async with self._transaction() as connection:
            await connection.execute(
                "UPDATE application_state SET paused=? WHERE singleton=1", (int(paused),)
            )

    async def is_paused(self) -> bool:
        async with self._lock:
            cursor = await self._db().execute(
                "SELECT paused FROM application_state WHERE singleton=1"
            )
            row = await cursor.fetchone()
            await cursor.close()
        return bool(row and row["paused"])
