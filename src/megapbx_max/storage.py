from __future__ import annotations

import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .domain import (
    CallbackClaim,
    CleanupResult,
    CounterCounts,
    DeliveryClaim,
    EnqueueResult,
    Job,
    NotificationRecord,
    RecordClaim,
    UnknownDelivery,
)


class StorageError(RuntimeError):
    pass


class DeliveryClaimLost(StorageError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_mid TEXT NOT NULL,
    phone TEXT NOT NULL,
    diversion TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    closed_at REAL,
    closed_by TEXT,
    claim_token TEXT,
    claim_expires_at REAL
);

CREATE TABLE IF NOT EXISTS delivery_claims (
    call_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL UNIQUE,
    job_event_key TEXT,
    state TEXT NOT NULL DEFAULT 'prepared',
    chat_id INTEGER,
    phone TEXT NOT NULL DEFAULT '',
    diversion TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    claimed_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL,
    dispatched_at REAL,
    unknown_reason TEXT
);

CREATE TABLE IF NOT EXISTS deduplication (
    call_id TEXT PRIMARY KEY,
    seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS phone_counters (
    phone TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    successful_today INTEGER NOT NULL DEFAULT 0,
    successful_total INTEGER NOT NULL DEFAULT 0,
    pending_today INTEGER NOT NULL DEFAULT 0,
    pending_total INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS counter_reservations (
    record_id TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    day TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS callback_events (
    callback_id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'processing',
    claim_token TEXT,
    lease_until REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    external_confirmed_at REAL,
    external_who TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL,
    lease_until REAL,
    claim_token TEXT,
    last_error_code TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_notifications_phone_open
    ON notifications(phone, closed_at, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_call
    ON notifications(call_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_message
    ON notifications(chat_id, message_mid);
CREATE INDEX IF NOT EXISTS idx_delivery_claims_state
    ON delivery_claims(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_jobs_due
    ON jobs(state, available_at, id);
"""


def _migrate_schema(connection: sqlite3.Connection) -> None:
    """Bring databases created by early development versions up to schema v2.

    The application was developed before the first release, so operators may
    still have a database from an intermediate build.  Migrations are kept
    deliberately additive where possible and rebuild only the callback table
    whose old ``status`` representation has no direct equivalent.
    """

    notification_columns = _table_columns(connection, "notifications")
    notification_additions = {
        "closed_at": "REAL",
        "closed_by": "TEXT",
        "claim_token": "TEXT",
        "claim_expires_at": "REAL",
        "updated_at": "REAL",
    }
    _add_missing_columns(connection, "notifications", notification_columns, notification_additions)
    connection.execute("UPDATE notifications SET updated_at = COALESCE(updated_at, created_at)")

    delivery_columns = _table_columns(connection, "delivery_claims")
    delivery_additions = {
        "state": "TEXT NOT NULL DEFAULT 'prepared'",
        "job_event_key": "TEXT",
        "chat_id": "INTEGER",
        "phone": "TEXT NOT NULL DEFAULT ''",
        "diversion": "TEXT NOT NULL DEFAULT ''",
        "text": "TEXT NOT NULL DEFAULT ''",
        "expires_at": "REAL",
        "updated_at": "REAL",
        "dispatched_at": "REAL",
        "unknown_reason": "TEXT",
    }
    _add_missing_columns(connection, "delivery_claims", delivery_columns, delivery_additions)
    connection.execute("UPDATE delivery_claims SET updated_at = COALESCE(updated_at, claimed_at)")

    callback_columns = _table_columns(connection, "callback_events")
    if "status" in callback_columns:
        _migrate_legacy_callback_events(connection, callback_columns)
        return
    callback_additions = {
        "state": "TEXT NOT NULL DEFAULT 'processing'",
        "claim_token": "TEXT",
        "lease_until": "REAL NOT NULL DEFAULT 0",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "external_confirmed_at": "REAL",
        "external_who": "TEXT",
        "updated_at": "REAL",
    }
    _add_missing_columns(connection, "callback_events", callback_columns, callback_additions)
    connection.execute("UPDATE callback_events SET updated_at = COALESCE(updated_at, 0)")


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: set[str],
    additions: dict[str, str],
) -> None:
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migrate_legacy_callback_events(connection: sqlite3.Connection, columns: set[str]) -> None:
    def source(name: str, fallback: str) -> str:
        return name if name in columns else fallback

    status = source("status", "''")
    lease_until = f"COALESCE({source('lease_until', '0')}, 0)"
    attempts = f"COALESCE({source('attempts', '0')}, 0)"
    external_confirmed_at = source("external_confirmed_at", "NULL")
    external_who = source("external_who", "NULL")
    updated_at = f"COALESCE({source('updated_at', source('created_at', '0'))}, 0)"
    claim_token = source("claim_token", "NULL")

    connection.execute("DROP TABLE IF EXISTS callback_events_migrated")
    connection.execute(
        "CREATE TABLE callback_events_migrated ("
        "callback_id TEXT PRIMARY KEY, state TEXT NOT NULL, claim_token TEXT, "
        "lease_until REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
        "external_confirmed_at REAL, external_who TEXT, updated_at REAL NOT NULL)"
    )
    connection.execute(
        "INSERT INTO callback_events_migrated("
        "callback_id, state, claim_token, lease_until, attempts, external_confirmed_at, "
        "external_who, updated_at"
        f") SELECT callback_id, CASE WHEN {status} = 'success' THEN 'committed' ELSE 'processing' END, "
        f"{claim_token}, {lease_until}, {attempts}, {external_confirmed_at}, {external_who}, {updated_at} "
        "FROM callback_events"
    )
    connection.execute("DROP TABLE callback_events")
    connection.execute("ALTER TABLE callback_events_migrated RENAME TO callback_events")


class SQLiteStore:
    def __init__(self, path: str) -> None:
        if path == ":memory:":
            raise ValueError("STATE_DB_PATH must be a persistent file, not :memory:")
        if not path.strip() or "\x00" in path:
            raise ValueError("STATE_DB_PATH must be a non-empty filesystem path")
        self.path = str(Path(path).expanduser())

    def initialize(self) -> None:
        database_path = Path(self.path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            _migrate_schema(connection)
            connection.executescript(_INDEX_SCHEMA)
            connection.execute("PRAGMA user_version=2")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def ping(self) -> None:
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if int(version) < 2:
                raise StorageError("Persistent storage schema is outdated")
            connection.execute("SELECT 1 FROM jobs LIMIT 1").fetchone()

    def begin_delivery(
        self,
        call_id: str,
        *,
        dedup_ttl_sec: int,
        prepared_ttl_sec: float = 300,
        dispatch_ttl_sec: float = 600,
        now: float | None = None,
        job_event_key: str | None = None,
    ) -> DeliveryClaim | None:
        timestamp = time.time() if now is None else now
        record_id = str(uuid.uuid4())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if call_id:
                seen = connection.execute(
                    "SELECT seen_at FROM deduplication WHERE call_id = ?",
                    (call_id,),
                ).fetchone()
                if (
                    seen is not None
                    and dedup_ttl_sec > 0
                    and timestamp - float(seen["seen_at"]) <= dedup_ttl_sec
                ):
                    return None
                connection.execute(
                    "DELETE FROM deduplication WHERE call_id = ? AND seen_at < ?",
                    (call_id, timestamp - dedup_ttl_sec),
                )
                claim = connection.execute(
                    "SELECT record_id, state, expires_at FROM delivery_claims WHERE call_id = ?",
                    (call_id,),
                ).fetchone()
                if claim is not None:
                    state = str(claim["state"])
                    expires_at = claim["expires_at"]
                    if state == "unknown":
                        return None
                    if expires_at is not None and float(expires_at) > timestamp:
                        return None
                    if state == "prepared":
                        self._finish_counter(
                            connection,
                            str(claim["record_id"]),
                            successful=False,
                            now=timestamp,
                        )
                        connection.execute("DELETE FROM delivery_claims WHERE call_id = ?", (call_id,))
                    else:
                        connection.execute(
                            "UPDATE delivery_claims SET state = 'unknown', updated_at = ?, expires_at = NULL, "
                            "unknown_reason = 'dispatch lease expired' WHERE record_id = ?",
                            (timestamp, claim["record_id"]),
                        )
                        return None
            claim_key = call_id or f"internal:{record_id}"
            connection.execute(
                "INSERT INTO delivery_claims("
                "call_id, record_id, job_event_key, state, claimed_at, updated_at, expires_at"
                ") VALUES (?, ?, ?, 'prepared', ?, ?, ?)",
                (claim_key, record_id, job_event_key, timestamp, timestamp, timestamp + prepared_ttl_sec),
            )
        return DeliveryClaim(record_id=record_id, call_id=call_id)

    def set_delivery_payload(
        self,
        claim: DeliveryClaim,
        *,
        chat_id: int,
        phone: str,
        diversion: str,
        text: str,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE delivery_claims SET chat_id = ?, phone = ?, diversion = ?, text = ?, "
                "updated_at = ? WHERE record_id = ? AND state = 'prepared'",
                (chat_id, phone, diversion, text, timestamp, claim.record_id),
            )
            if cursor.rowcount != 1:
                raise DeliveryClaimLost("Prepared delivery claim no longer exists")

    def mark_delivery_dispatched(
        self,
        claim: DeliveryClaim,
        *,
        dispatch_ttl_sec: float = 600,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE delivery_claims SET state = 'inflight', dispatched_at = ?, updated_at = ?, "
                "expires_at = ? WHERE record_id = ? AND state = 'prepared'",
                (timestamp, timestamp, timestamp + dispatch_ttl_sec, claim.record_id),
            )
            if cursor.rowcount != 1:
                raise DeliveryClaimLost("Prepared delivery claim no longer exists")

    def mark_delivery_unknown(self, claim: DeliveryClaim, reason: str, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM delivery_claims WHERE record_id = ?",
                (claim.record_id,),
            ).fetchone()
            if row is None or row["state"] == "unknown":
                return
            connection.execute(
                "UPDATE delivery_claims SET state = 'unknown', updated_at = ?, expires_at = NULL, "
                "unknown_reason = ? WHERE record_id = ?",
                (timestamp, reason[:200], claim.record_id),
            )

    def reserve_counter(self, record_id: str, phone: str, day: str, *, now: float | None = None) -> CounterCounts:
        if not phone:
            return CounterCounts(today=0, total=0)
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM phone_counters WHERE phone = ?", (phone,)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO phone_counters(phone, day, updated_at) VALUES (?, ?, ?)",
                    (phone, day, timestamp),
                )
                successful_today = successful_total = pending_today = pending_total = 0
            elif row["day"] != day:
                successful_today = 0
                pending_today = 0
                successful_total = int(row["successful_total"])
                pending_total = int(row["pending_total"])
                connection.execute(
                    "UPDATE phone_counters SET day = ?, successful_today = 0, pending_today = 0, "
                    "updated_at = ? WHERE phone = ?",
                    (day, timestamp, phone),
                )
            else:
                successful_today = int(row["successful_today"])
                successful_total = int(row["successful_total"])
                pending_today = int(row["pending_today"])
                pending_total = int(row["pending_total"])

            connection.execute(
                "INSERT INTO counter_reservations(record_id, phone, day, created_at) VALUES (?, ?, ?, ?)",
                (record_id, phone, day, timestamp),
            )
            pending_today += 1
            pending_total += 1
            connection.execute(
                "UPDATE phone_counters SET pending_today = ?, pending_total = ?, updated_at = ? "
                "WHERE phone = ?",
                (pending_today, pending_total, timestamp, phone),
            )
        return CounterCounts(today=successful_today + pending_today, total=successful_total + pending_total)

    def complete_delivery(
        self,
        claim: DeliveryClaim,
        *,
        chat_id: int,
        message_mid: str,
        phone: str,
        diversion: str,
        text: str,
        now: float | None = None,
    ) -> NotificationRecord:
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM notifications WHERE id = ?",
                (claim.record_id,),
            ).fetchone()
            if existing is not None:
                self._finish_counter(connection, claim.record_id, successful=True, now=timestamp)
                return _record_from_row(existing)

            connection.execute("BEGIN IMMEDIATE")
            delivery = connection.execute(
                "SELECT record_id, state FROM delivery_claims WHERE record_id = ?",
                (claim.record_id,),
            ).fetchone()
            if delivery is None or delivery["state"] == "unknown":
                raise DeliveryClaimLost("Delivery claim is no longer completable")
            connection.execute(
                "INSERT INTO notifications("
                "id, call_id, chat_id, message_mid, phone, diversion, text, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    claim.record_id,
                    claim.call_id,
                    chat_id,
                    message_mid,
                    phone,
                    diversion,
                    text,
                    timestamp,
                    timestamp,
                ),
            )
            if claim.call_id:
                connection.execute(
                    "INSERT INTO deduplication(call_id, seen_at) VALUES (?, ?) "
                    "ON CONFLICT(call_id) DO UPDATE SET seen_at = excluded.seen_at",
                    (claim.call_id, timestamp),
                )
            connection.execute("DELETE FROM delivery_claims WHERE record_id = ?", (claim.record_id,))
            self._finish_counter(connection, claim.record_id, successful=True, now=timestamp)
            row = connection.execute("SELECT * FROM notifications WHERE id = ?", (claim.record_id,)).fetchone()
        if row is None:
            raise StorageError("Notification was not stored")
        return _record_from_row(row)

    def fail_delivery(self, claim: DeliveryClaim, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM delivery_claims WHERE record_id = ?",
                (claim.record_id,),
            ).fetchone()
            if row is None or row["state"] == "unknown":
                return
            connection.execute("DELETE FROM delivery_claims WHERE record_id = ?", (claim.record_id,))
            self._finish_counter(connection, claim.record_id, successful=False, now=timestamp)

    def list_unknown_deliveries(self) -> list[UnknownDelivery]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT record_id, call_id, state, claimed_at, updated_at "
                "FROM delivery_claims WHERE state = 'unknown' ORDER BY updated_at"
            ).fetchall()
        return [
            UnknownDelivery(
                record_id=str(row["record_id"]),
                call_id=str(row["call_id"]),
                state=str(row["state"]),
                created_at=float(row["claimed_at"]),
                updated_at=float(row["updated_at"]),
            )
            for row in rows
        ]

    def resolve_unknown_as_sent(self, record_id: str, message_mid: str, *, now: float | None = None) -> NotificationRecord:
        if not message_mid:
            raise StorageError("message_mid is required")
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM delivery_claims WHERE record_id = ? AND state = 'unknown'",
                (record_id,),
            ).fetchone()
            if row is None:
                raise DeliveryClaimLost("Unknown delivery claim was not found")
            if row["chat_id"] is None or not row["text"]:
                raise StorageError("Unknown delivery has incomplete metadata")
            call_id = str(row["call_id"])
            business_call_id = "" if call_id.startswith("internal:") else call_id
            event_key = row["job_event_key"]
            if not event_key and business_call_id:
                event_key = f"megapbx:missed:{business_call_id}"
            if event_key:
                job = connection.execute(
                    "SELECT id, state FROM jobs WHERE event_key = ?",
                    (str(event_key),),
                ).fetchone()
                if job is not None:
                    job_state = str(job["state"])
                    if job_state == "processing":
                        raise StorageError("Related durable job is currently processing")
                    if job_state not in {"pending", "completed", "dead"}:
                        raise StorageError("Related durable job has an unsupported state")
                    cursor = connection.execute(
                        "UPDATE jobs SET state = 'completed', available_at = ?, lease_until = NULL, "
                        "claim_token = NULL, last_error_code = NULL, updated_at = ? WHERE id = ? "
                        "AND state IN ('pending', 'completed', 'dead')",
                        (timestamp, timestamp, job["id"]),
                    )
                    if cursor.rowcount != 1:
                        raise StorageError("Related durable job changed while it was being resolved")
            connection.execute(
                "INSERT INTO notifications("
                "id, call_id, chat_id, message_mid, phone, diversion, text, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    business_call_id,
                    int(row["chat_id"]),
                    message_mid,
                    str(row["phone"]),
                    str(row["diversion"]),
                    str(row["text"]),
                    float(row["claimed_at"]),
                    timestamp,
                ),
            )
            if business_call_id:
                connection.execute(
                    "INSERT INTO deduplication(call_id, seen_at) VALUES (?, ?) "
                    "ON CONFLICT(call_id) DO UPDATE SET seen_at = excluded.seen_at",
                    (business_call_id, timestamp),
                )
            connection.execute("DELETE FROM delivery_claims WHERE record_id = ?", (record_id,))
            self._finish_counter(connection, record_id, successful=True, now=timestamp)
            stored = connection.execute("SELECT * FROM notifications WHERE id = ?", (record_id,)).fetchone()
        if stored is None:
            raise StorageError("Unknown delivery was not resolved")
        return _record_from_row(stored)

    def retry_unknown(self, record_id: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, call_id, job_event_key FROM delivery_claims WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            if row is None or row["state"] != "unknown":
                raise DeliveryClaimLost("Unknown delivery claim was not found")

            call_id = str(row["call_id"])
            event_key = row["job_event_key"]
            if not event_key:
                if call_id.startswith("internal:"):
                    raise StorageError("Unknown delivery has no associated durable job")
                # Claims created before job association was added can still be
                # recovered for the ordinary call-id event key.
                event_key = f"megapbx:missed:{call_id}"
            event_key = str(event_key)
            job = connection.execute(
                "SELECT id, state FROM jobs WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if job is None:
                raise StorageError("Related durable job was not found")
            job_state = str(job["state"])
            if job_state not in {"pending", "completed", "dead"}:
                raise StorageError("Related durable job is currently processing")

            timestamp = time.time()
            cursor = connection.execute(
                "UPDATE jobs SET state = 'pending', attempts = 0, available_at = ?, "
                "lease_until = NULL, claim_token = NULL, last_error_code = NULL, updated_at = ? "
                "WHERE id = ? AND state IN ('pending', 'completed', 'dead')",
                (timestamp, timestamp, job["id"]),
            )
            if cursor.rowcount != 1:
                raise StorageError("Related durable job changed while it was being recovered")
            connection.execute("DELETE FROM delivery_claims WHERE record_id = ?", (record_id,))
            self._finish_counter(connection, record_id, successful=False, now=timestamp)

    def get_by_id(self, record_id: str) -> NotificationRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM notifications WHERE id = ?", (record_id,)).fetchone()
        return _record_from_row(row) if row is not None else None

    def get_latest_by_call_id(self, call_id: str) -> NotificationRecord | None:
        if not call_id:
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE call_id = ? ORDER BY created_at DESC LIMIT 1",
                (call_id,),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def get_by_message(self, chat_id: int, message_mid: str) -> NotificationRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE chat_id = ? AND message_mid = ?",
                (chat_id, message_mid),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def find_recent_by_phone(self, phone: str, max_age_sec: int, *, now: float | None = None) -> NotificationRecord | None:
        if not phone:
            return None
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE phone = ? AND closed_at IS NULL "
                "AND created_at >= ? AND (claim_token IS NULL OR claim_expires_at <= ?) "
                "ORDER BY created_at DESC LIMIT 1",
                (phone, timestamp - max_age_sec, timestamp),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def claim_record(
        self,
        record_id: str,
        *,
        lease_sec: float = 60,
        now: float | None = None,
    ) -> RecordClaim | None:
        timestamp = time.time() if now is None else now
        token = str(uuid.uuid4())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM notifications WHERE id = ?", (record_id,)).fetchone()
            if row is None or row["closed_at"] is not None:
                return None
            if row["claim_token"] is not None and float(row["claim_expires_at"] or 0) > timestamp:
                return None
            connection.execute(
                "UPDATE notifications SET claim_token = ?, claim_expires_at = ?, updated_at = ? WHERE id = ?",
                (token, timestamp + lease_sec, timestamp, record_id),
            )
        return RecordClaim(record=_record_from_row(row), token=token)

    def release_record(self, record_id: str, token: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE notifications SET claim_token = NULL, claim_expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND claim_token = ?",
                (time.time(), record_id, token),
            )

    def close_record(self, record: RecordClaim, who: str, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE notifications SET closed_at = ?, closed_by = ?, claim_token = NULL, "
                "claim_expires_at = NULL, updated_at = ? WHERE id = ? AND claim_token = ? "
                "AND closed_at IS NULL",
                (timestamp, who, timestamp, record.record.id, record.token),
            )
            if cursor.rowcount != 1:
                raise StorageError("Notification claim was lost")

    def update_record_text(self, record: RecordClaim, text: str, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE notifications SET text = ?, claim_token = NULL, claim_expires_at = NULL, "
                "updated_at = ? WHERE id = ? AND claim_token = ? AND closed_at IS NULL",
                (text, timestamp, record.record.id, record.token),
            )
            if cursor.rowcount != 1:
                raise StorageError("Notification claim was lost")

    def claim_callback(
        self,
        callback_id: str,
        *,
        lease_sec: float = 120,
        now: float | None = None,
    ) -> CallbackClaim:
        timestamp = time.time() if now is None else now
        token = str(uuid.uuid4())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, claim_token, lease_until, attempts FROM callback_events "
                "WHERE callback_id = ?",
                (callback_id,),
            ).fetchone()
            if row is not None and row["state"] in {"committed", "external_unknown", "terminal"}:
                return CallbackClaim(
                    callback_id=callback_id,
                    acquired=False,
                    state=str(row["state"]),
                )
            if (
                row is not None
                and row["state"] == "processing"
                and float(row["lease_until"]) > timestamp
            ):
                return CallbackClaim(
                    callback_id=callback_id,
                    acquired=False,
                    state="processing",
                )
            next_state = "external_confirmed" if row is not None and row["state"] == "external_confirmed" else "processing"
            attempts = (int(row["attempts"]) + 1) if row is not None else 1
            connection.execute(
                "INSERT INTO callback_events("
                "callback_id, state, claim_token, lease_until, attempts, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(callback_id) DO UPDATE SET "
                "state = excluded.state, claim_token = excluded.claim_token, "
                "lease_until = excluded.lease_until, attempts = excluded.attempts, "
                "updated_at = excluded.updated_at",
                (callback_id, next_state, token, timestamp + lease_sec, attempts, timestamp),
            )
        return CallbackClaim(callback_id=callback_id, acquired=True, token=token, state=next_state)

    def release_callback(self, callback_id: str, token: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM callback_events WHERE callback_id = ? AND claim_token = ? "
                "AND state = 'processing'",
                (callback_id, token),
            )

    def mark_callback_external_confirmed(
        self,
        callback_id: str,
        token: str,
        *,
        who: str | None = None,
        lease_sec: float = 60,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE callback_events SET state = 'external_confirmed', external_confirmed_at = ?, "
                "external_who = ?, lease_until = ?, updated_at = ? WHERE callback_id = ? "
                "AND claim_token = ? AND state = 'processing'",
                (timestamp, who, timestamp + lease_sec, timestamp, callback_id, token),
            )
            if cursor.rowcount != 1:
                raise StorageError("Callback claim was lost before external confirmation")

    def mark_callback_external_unknown(
        self,
        callback_id: str,
        token: str,
        *,
        lease_sec: float = 300,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE callback_events SET state = 'external_unknown', lease_until = ?, updated_at = ? "
                "WHERE callback_id = ? AND claim_token = ? AND state = 'processing'",
                (timestamp + lease_sec, timestamp, callback_id, token),
            )
            if cursor.rowcount != 1:
                raise StorageError("Callback claim was lost before external-outcome recording")

    def mark_callback_terminal(
        self,
        callback_id: str,
        token: str,
        *,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE callback_events SET state = 'terminal', lease_until = ?, updated_at = ? "
                "WHERE callback_id = ? AND claim_token = ? AND state = 'processing'",
                (timestamp, timestamp, callback_id, token),
            )
            if cursor.rowcount != 1:
                raise StorageError("Callback claim was lost before terminal-state recording")

    def list_external_unknown_callbacks(self) -> list[dict[str, object]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT callback_id, state, updated_at, lease_until FROM callback_events "
                "WHERE state = 'external_unknown' ORDER BY updated_at"
            ).fetchall()
        return [
            {
                "callback_id": str(row["callback_id"]),
                "state": str(row["state"]),
                "updated_at": float(row["updated_at"]),
                "lease_until": float(row["lease_until"]),
            }
            for row in rows
        ]

    def resolve_external_unknown_callback(self, callback_id: str) -> None:
        """Reconcile an ambiguous callback without replaying the MAX request."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE callback_events SET state = 'committed', claim_token = NULL, "
                "lease_until = 0, updated_at = ? WHERE callback_id = ? AND state = 'external_unknown'",
                (time.time(), callback_id),
            )
            if cursor.rowcount != 1:
                raise StorageError("External-unknown callback was not found")

    def retry_external_unknown_callback(self, callback_id: str) -> None:
        """Atomically release an ambiguous answer and requeue its saved webhook."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT 1 FROM callback_events WHERE callback_id = ? AND state = 'external_unknown'",
                (callback_id,),
            ).fetchone()
            if row is None:
                raise StorageError("External-unknown callback was not found")
            job = connection.execute(
                "SELECT id, state FROM jobs WHERE event_key = ? AND kind = 'max_update'",
                (f"max:callback:{callback_id}",),
            ).fetchone()
            if job is None or job["state"] not in {"dead", "pending", "completed"}:
                raise StorageError("Callback job is missing or currently processing")
            timestamp = time.time()
            connection.execute(
                "UPDATE jobs SET state = 'pending', attempts = 0, available_at = ?, "
                "lease_until = NULL, claim_token = NULL, last_error_code = NULL, updated_at = ? "
                "WHERE id = ?",
                (timestamp, timestamp, job["id"]),
            )
            cursor = connection.execute(
                "DELETE FROM callback_events WHERE callback_id = ? AND state = 'external_unknown'",
                (callback_id,),
            )
            if cursor.rowcount != 1:
                raise StorageError("External-unknown callback was not found")

    def complete_callback(
        self,
        callback_id: str,
        token: str,
        *,
        record: RecordClaim | None = None,
        who: str | None = None,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if record is not None and who is None:
                callback_row = connection.execute(
                    "SELECT external_who FROM callback_events WHERE callback_id = ?",
                    (callback_id,),
                ).fetchone()
                if callback_row is not None:
                    who = callback_row["external_who"]
            if record is not None:
                cursor = connection.execute(
                    "UPDATE notifications SET closed_at = ?, closed_by = ?, claim_token = NULL, "
                    "claim_expires_at = NULL, updated_at = ? WHERE id = ? AND claim_token = ? "
                    "AND closed_at IS NULL",
                    (timestamp, who, timestamp, record.record.id, record.token),
                )
                if cursor.rowcount != 1:
                    raise StorageError("Notification claim was lost")
            cursor = connection.execute(
                "UPDATE callback_events SET state = 'committed', lease_until = ?, updated_at = ? "
                "WHERE callback_id = ? AND claim_token = ? AND state IN ('processing', 'external_confirmed')",
                (timestamp, timestamp, callback_id, token),
            )
            if cursor.rowcount != 1:
                raise StorageError("Callback claim was lost before commit")

    def enqueue_job(
        self,
        *,
        event_key: str,
        kind: str,
        payload: str,
        reopen_completed_after_sec: float | None = None,
        now: float | None = None,
    ) -> EnqueueResult:
        if not event_key or not kind or not payload:
            raise StorageError("Job event_key, kind and payload are required")
        timestamp = time.time() if now is None else now
        reopened = False
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO jobs("
                "event_key, kind, payload, state, available_at, created_at, updated_at"
                ") VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (event_key, kind, payload, timestamp, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT id, state, created_at, updated_at FROM jobs WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if row is not None:
                state = str(row["state"])
                last_updated = float(row["updated_at"] if row["updated_at"] is not None else row["created_at"])
                completed_expired = (
                    state == "completed"
                    and reopen_completed_after_sec is not None
                    and timestamp - last_updated >= max(0.0, reopen_completed_after_sec)
                )
                if state == "dead" or completed_expired:
                    connection.execute(
                        "UPDATE jobs SET kind = ?, payload = ?, state = 'pending', attempts = 0, "
                        "available_at = ?, lease_until = NULL, claim_token = NULL, "
                        "last_error_code = NULL, updated_at = ? WHERE id = ? AND state = ?",
                        (kind, payload, timestamp, timestamp, row["id"], state),
                    )
                    reopened = True
        if row is None:
            raise StorageError("Job was not stored")
        return EnqueueResult(
            job_id=int(row["id"]),
            inserted=cursor.rowcount == 1,
            reopened=reopened,
        )

    def claim_job(
        self,
        *,
        lease_sec: float = 120,
        now: float | None = None,
    ) -> Job | None:
        timestamp = time.time() if now is None else now
        token = str(uuid.uuid4())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, event_key, kind, payload, attempts FROM jobs "
                "WHERE (state = 'pending' AND available_at <= ?) "
                "OR (state = 'processing' AND lease_until IS NOT NULL AND lease_until <= ?) "
                "ORDER BY id LIMIT 1",
                (timestamp, timestamp),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE jobs SET state = 'processing', claim_token = ?, lease_until = ?, "
                "attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (token, timestamp + lease_sec, timestamp, row["id"]),
            )
        return Job(
            id=int(row["id"]),
            event_key=str(row["event_key"]),
            kind=str(row["kind"]),
            payload=str(row["payload"]),
            attempt=int(row["attempts"]) + 1,
            claim_token=token,
        )

    def complete_job(self, job: Job) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET state = 'completed', lease_until = NULL, updated_at = ? "
                "WHERE id = ? AND state = 'processing' AND claim_token = ?",
                (time.time(), job.id, job.claim_token),
            )
            if cursor.rowcount != 1:
                raise StorageError("Job claim was lost before completion")

    def release_job(self, job: Job, *, delay_sec: float = 0.0) -> bool:
        timestamp = time.time()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET state = 'pending', attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END, "
                "available_at = ?, lease_until = NULL, claim_token = NULL, updated_at = ? "
                "WHERE id = ? AND state = 'processing' AND claim_token = ?",
                (timestamp + max(0.0, delay_sec), timestamp, job.id, job.claim_token),
            )
        return cursor.rowcount == 1

    def retry_job(
        self,
        job: Job,
        *,
        delay_sec: float,
        error_code: str,
        max_attempts: int,
        dead_state: str = "dead",
        consume_attempt: bool = True,
    ) -> None:
        timestamp = time.time()
        next_state = dead_state if job.attempt >= max_attempts else "pending"
        available_at = timestamp + max(0.0, delay_sec) if next_state == "pending" else timestamp
        attempts_sql = "attempts" if consume_attempt else "CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END"
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET state = ?, attempts = {attempts_sql}, available_at = ?, lease_until = NULL, "
                "claim_token = NULL, last_error_code = ?, updated_at = ? WHERE id = ? AND state = 'processing' "
                "AND claim_token = ?",
                (next_state, available_at, error_code[:64], timestamp, job.id, job.claim_token),
            )
            if cursor.rowcount != 1:
                raise StorageError("Job claim was lost before retry")

    def get_job_state(self, job_id: int) -> str | None:
        with self._connection() as connection:
            row = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return str(row["state"]) if row is not None else None

    def get_job(self, job_id: int) -> Job | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, event_key, kind, payload, attempts, claim_token FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None or row["claim_token"] is None:
            return None
        return Job(
            id=int(row["id"]),
            event_key=str(row["event_key"]),
            kind=str(row["kind"]),
            payload=str(row["payload"]),
            attempt=int(row["attempts"]),
            claim_token=str(row["claim_token"]),
        )

    def cleanup(
        self,
        *,
        max_age_sec: int,
        dedup_ttl_sec: int,
        callback_ttl_sec: int | None = None,
        now: float | None = None,
    ) -> CleanupResult:
        timestamp = time.time() if now is None else now
        callback_ttl = max(dedup_ttl_sec, 3600) if callback_ttl_sec is None else callback_ttl_sec
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            notification_count = connection.execute(
                "DELETE FROM notifications WHERE created_at < ? AND closed_at IS NOT NULL",
                (timestamp - max_age_sec,),
            ).rowcount
            dedup_count = connection.execute(
                "DELETE FROM deduplication WHERE seen_at < ?",
                (timestamp - dedup_ttl_sec,),
            ).rowcount
            callback_count = connection.execute(
                "DELETE FROM callback_events WHERE state IN ('committed', 'terminal') AND updated_at < ?",
                (timestamp - callback_ttl,),
            ).rowcount
            job_count = connection.execute(
                "DELETE FROM jobs WHERE state IN ('completed', 'dead') AND updated_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM callback_events AS unknown_callback "
                "WHERE unknown_callback.state = 'external_unknown' "
                "AND jobs.event_key = 'max:callback:' || unknown_callback.callback_id) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM delivery_claims AS unknown_claim "
                "WHERE unknown_claim.state = 'unknown' AND ("
                "unknown_claim.job_event_key = jobs.event_key OR ("
                "unknown_claim.job_event_key IS NULL "
                "AND unknown_claim.call_id NOT LIKE 'internal:%' "
                "AND jobs.event_key = 'megapbx:missed:' || unknown_claim.call_id"
                ")))",
                (timestamp - callback_ttl,),
            ).rowcount
            connection.execute(
                "UPDATE notifications SET claim_token = NULL, claim_expires_at = NULL "
                "WHERE claim_expires_at <= ?",
                (timestamp,),
            )
            expired_claims = connection.execute(
                "SELECT record_id, state FROM delivery_claims "
                "WHERE state IN ('prepared', 'inflight') AND expires_at <= ?",
                (timestamp,),
            ).fetchall()
            unknown_count = 0
            for delivery_claim in expired_claims:
                state = str(delivery_claim["state"])
                if state == "prepared":
                    self._finish_counter(
                        connection,
                        str(delivery_claim["record_id"]),
                        successful=False,
                        now=timestamp,
                    )
                    connection.execute(
                        "DELETE FROM delivery_claims WHERE record_id = ?",
                        (delivery_claim["record_id"],),
                    )
                else:
                    connection.execute(
                        "UPDATE delivery_claims SET state = 'unknown', updated_at = ?, "
                        "expires_at = NULL, unknown_reason = 'dispatch lease expired' "
                        "WHERE record_id = ?",
                        (timestamp, delivery_claim["record_id"]),
                    )
                    unknown_count += 1
            stale_reservations = connection.execute(
                "SELECT cr.record_id FROM counter_reservations AS cr "
                "LEFT JOIN delivery_claims AS dc ON dc.record_id = cr.record_id "
                "WHERE cr.created_at < ? AND (dc.record_id IS NULL OR dc.state != 'unknown')",
                (timestamp - 86400,),
            ).fetchall()
            for reservation in stale_reservations:
                self._finish_counter(
                    connection,
                    str(reservation["record_id"]),
                    successful=False,
                    now=timestamp,
                )
            connection.execute(
                "DELETE FROM phone_counters WHERE successful_total = 0 AND pending_total = 0"
            )
        return CleanupResult(
            notifications=max(0, notification_count),
            deduplication=max(0, dedup_count),
            callbacks=max(0, callback_count),
            jobs=max(0, job_count),
            unknown_deliveries=max(0, unknown_count),
        )

    def _finish_counter(
        self,
        connection: sqlite3.Connection,
        record_id: str,
        *,
        successful: bool,
        now: float,
    ) -> None:
        reservation = connection.execute(
            "SELECT phone, day FROM counter_reservations WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if reservation is None:
            return
        phone = str(reservation["phone"])
        day = str(reservation["day"])
        connection.execute("DELETE FROM counter_reservations WHERE record_id = ?", (record_id,))
        row = connection.execute("SELECT * FROM phone_counters WHERE phone = ?", (phone,)).fetchone()
        if row is None:
            return
        successful_today = int(row["successful_today"])
        successful_total = int(row["successful_total"])
        pending_today = int(row["pending_today"])
        pending_total = int(row["pending_total"])
        if row["day"] == day:
            pending_today = max(0, pending_today - 1)
        pending_total = max(0, pending_total - 1)
        if successful:
            successful_total += 1
            if row["day"] == day:
                successful_today += 1
        connection.execute(
            "UPDATE phone_counters SET successful_today = ?, successful_total = ?, pending_today = ?, "
            "pending_total = ?, updated_at = ? WHERE phone = ?",
            (successful_today, successful_total, pending_today, pending_total, now, phone),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=1.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=1000")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise StorageError("SQLite operation failed") from exc
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()


def _record_from_row(row: sqlite3.Row) -> NotificationRecord:
    return NotificationRecord(
        id=str(row["id"]),
        call_id=str(row["call_id"]),
        chat_id=int(row["chat_id"]),
        message_mid=str(row["message_mid"]),
        phone=str(row["phone"]),
        diversion=str(row["diversion"]),
        text=str(row["text"]),
        created_at=float(row["created_at"]),
        closed=row["closed_at"] is not None,
        who=str(row["closed_by"]) if row["closed_by"] is not None else None,
    )
