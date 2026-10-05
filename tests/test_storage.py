from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from megapbx_max.storage import SQLiteStore, StorageError


def test_legacy_schema_is_migrated_before_indexes_are_created(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE notifications (
                id TEXT PRIMARY KEY,
                call_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_mid TEXT NOT NULL,
                phone TEXT NOT NULL,
                diversion TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at REAL NOT NULL,
                closed_at REAL,
                closed_by TEXT
            );
            CREATE TABLE delivery_claims (
                call_id TEXT PRIMARY KEY,
                record_id TEXT NOT NULL UNIQUE,
                claimed_at REAL NOT NULL
            );
            CREATE TABLE callback_events (
                callback_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                updated_at REAL
            );
            INSERT INTO notifications VALUES
                ('record-legacy', 'call-legacy', 42, 'mid-legacy', '+15555550123', '100', 'text', 2000000000, NULL, NULL);
            INSERT INTO callback_events VALUES ('cb-done', 'success', 2000000000);
            INSERT INTO callback_events VALUES ('cb-open', 'processing', NULL);
            """
        )

    store = SQLiteStore(str(path))
    store.initialize()
    store.ping()
    store.cleanup(max_age_sec=3600, dedup_ttl_sec=3600)

    assert store.get_by_id("record-legacy") is not None
    assert store.claim_callback("cb-done").state == "committed"
    assert store.claim_callback("cb-open").acquired is True


def test_durable_job_store_uses_full_sqlite_synchronous_mode(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_in_memory_state_is_rejected() -> None:
    with pytest.raises(ValueError, match="persistent"):
        SQLiteStore(":memory:")


def make_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(str(tmp_path / "state.sqlite3"))
    store.initialize()
    return store


def test_unknown_callback_requires_explicit_reconciliation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = store.enqueue_job(event_key="max:callback:cb-unknown", kind="max_update", payload="{}")
    job = store.claim_job()
    assert job is not None
    claim = store.claim_callback("cb-unknown", now=100)
    assert claim.token is not None
    store.mark_callback_external_unknown("cb-unknown", claim.token, now=101)
    store.retry_job(job, delay_sec=0, error_code="CallbackOutcomeUnknown", max_attempts=1)
    assert store.get_job_state(result.job_id) == "dead"
    assert store.claim_callback("cb-unknown", now=1000).acquired is False

    store.cleanup(max_age_sec=0, dedup_ttl_sec=0, callback_ttl_sec=0, now=1000)
    assert store.get_job_state(result.job_id) == "dead"
    store.retry_external_unknown_callback("cb-unknown")
    assert store.get_job_state(result.job_id) == "pending"
    assert store.list_external_unknown_callbacks() == []
    assert store.claim_callback("cb-unknown").acquired is True
    with pytest.raises(StorageError):
        store.retry_external_unknown_callback("cb-unknown")


def test_unknown_callback_resolution_does_not_close_notification(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    claim = store.claim_callback("cb-resolve")
    assert claim.token is not None
    store.mark_callback_external_unknown("cb-resolve", claim.token)
    store.resolve_external_unknown_callback("cb-resolve")
    assert store.list_external_unknown_callbacks() == []
    assert store.claim_callback("cb-resolve").state == "committed"
    with pytest.raises(StorageError):
        store.resolve_external_unknown_callback("cb-resolve")


def test_callid_delivery_claim_and_deduplication(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    first = store.begin_delivery("call-1", dedup_ttl_sec=3600)
    duplicate_while_sending = store.begin_delivery("call-1", dedup_ttl_sec=3600)
    assert first is not None
    assert duplicate_while_sending is None

    store.complete_delivery(
        first,
        chat_id=42,
        message_mid="mid-1",
        phone="+15555550123",
        diversion="100",
        text="original",
    )

    assert store.begin_delivery("call-1", dedup_ttl_sec=3600) is None
    assert store.get_by_message(42, "mid-1") is not None


def test_failed_delivery_rolls_back_counter_reservation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    failed = store.begin_delivery("call-failed", dedup_ttl_sec=3600)
    second = store.begin_delivery("call-second", dedup_ttl_sec=3600)
    assert failed is not None
    assert second is not None

    first_counts = store.reserve_counter(failed.record_id, "+15555550123", "2026-09-24")
    assert (first_counts.today, first_counts.total) == (1, 1)
    store.fail_delivery(failed)

    second_counts = store.reserve_counter(second.record_id, "+15555550123", "2026-09-24")
    assert (second_counts.today, second_counts.total) == (1, 1)
    store.complete_delivery(
        second,
        chat_id=42,
        message_mid="mid-2",
        phone="+15555550123",
        diversion="100",
        text="original",
    )
    third = store.begin_delivery("call-third", dedup_ttl_sec=3600)
    assert third is not None
    third_counts = store.reserve_counter(third.record_id, "+15555550123", "2026-09-24")
    assert (third_counts.today, third_counts.total) == (2, 2)


def test_zero_dedup_ttl_allows_a_new_delivery(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.begin_delivery("call-zero", dedup_ttl_sec=0)
    assert first is not None
    store.complete_delivery(
        first,
        chat_id=42,
        message_mid="mid-zero-1",
        phone="+15555550123",
        diversion="100",
        text="original",
    )

    second = store.begin_delivery("call-zero", dedup_ttl_sec=0)
    assert second is not None
    assert second.record_id != first.record_id


def test_delivery_without_callid_is_still_cached(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    claim = store.begin_delivery("", dedup_ttl_sec=3600)
    assert claim is not None

    record = store.complete_delivery(
        claim,
        chat_id=42,
        message_mid="mid-no-callid",
        phone="",
        diversion="",
        text="original",
    )

    assert record.call_id == ""
    assert store.get_by_id(claim.record_id) is not None


def test_ambiguous_dispatch_is_not_automatically_retried(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    claim = store.begin_delivery("call-unknown", dedup_ttl_sec=3600, now=100)
    assert claim is not None
    store.reserve_counter(claim.record_id, "+15555550123", "2026-09-24", now=100)
    store.set_delivery_payload(
        claim,
        chat_id=42,
        phone="+15555550123",
        diversion="100",
        text="original",
        now=100,
    )
    store.mark_delivery_dispatched(claim, now=100)
    store.mark_delivery_unknown(claim, "read timeout", now=101)

    assert len(store.list_unknown_deliveries()) == 1
    assert store.begin_delivery("call-unknown", dedup_ttl_sec=3600, now=102) is None
    resolved = store.resolve_unknown_as_sent(claim.record_id, "mid-manual", now=103)
    assert resolved.message_mid == "mid-manual"
    assert store.begin_delivery("call-unknown", dedup_ttl_sec=3600, now=104) is None


def test_manual_unknown_resolution_updates_counters(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    claim = store.begin_delivery("call-manual", dedup_ttl_sec=3600)
    assert claim is not None
    store.reserve_counter(claim.record_id, "+15555550123", "2026-09-24")
    store.set_delivery_payload(
        claim,
        chat_id=42,
        phone="+15555550123",
        diversion="100",
        text="original",
    )
    store.mark_delivery_dispatched(claim)
    store.mark_delivery_unknown(claim, "read timeout")

    store.resolve_unknown_as_sent(claim.record_id, "mid-manual")
    next_claim = store.begin_delivery("call-manual-next", dedup_ttl_sec=3600)
    assert next_claim is not None
    counts = store.reserve_counter(next_claim.record_id, "+15555550123", "2026-09-24")
    assert (counts.today, counts.total) == (2, 2)


def test_manual_unknown_retry_rolls_back_counter(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    claim = store.begin_delivery("call-retry", dedup_ttl_sec=3600)
    assert claim is not None
    store.reserve_counter(claim.record_id, "+15555550123", "2026-09-24")
    store.set_delivery_payload(
        claim,
        chat_id=42,
        phone="+15555550123",
        diversion="100",
        text="original",
    )
    store.mark_delivery_dispatched(claim)
    store.mark_delivery_unknown(claim, "read timeout")
    job_result = store.enqueue_job(
        event_key="megapbx:missed:call-retry",
        kind="megapbx_event",
        payload='{"callid":"call-retry"}',
    )
    job = store.claim_job()
    assert job is not None
    store.complete_job(job)

    store.retry_unknown(claim.record_id)
    assert store.get_job_state(job_result.job_id) == "pending"
    next_claim = store.begin_delivery("call-retry-next", dedup_ttl_sec=3600)
    assert next_claim is not None
    counts = store.reserve_counter(next_claim.record_id, "+15555550123", "2026-09-24")
    assert (counts.today, counts.total) == (1, 1)


def test_retry_unknown_requeues_associated_job_without_callid(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    job_result = store.enqueue_job(
        event_key="megapbx:event:no-call-id",
        kind="megapbx_event",
        payload='{"cmd":"history"}',
    )
    job = store.claim_job()
    assert job is not None
    store.complete_job(job)

    claim = store.begin_delivery(
        "",
        dedup_ttl_sec=3600,
        job_event_key="megapbx:event:no-call-id",
    )
    assert claim is not None
    store.mark_delivery_dispatched(claim)
    store.mark_delivery_unknown(claim, "read timeout")

    store.retry_unknown(claim.record_id)
    assert store.get_job_state(job_result.job_id) == "pending"


def test_resolve_unknown_without_callid_completes_related_job(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = store.enqueue_job(
        event_key="megapbx:event:resolved-no-call-id",
        kind="megapbx_event",
        payload='{"cmd":"history"}',
    )
    claim = store.begin_delivery(
        "",
        dedup_ttl_sec=3600,
        job_event_key="megapbx:event:resolved-no-call-id",
    )
    assert claim is not None
    store.set_delivery_payload(
        claim,
        chat_id=42,
        phone="+15555550123",
        diversion="100",
        text="original",
    )
    store.mark_delivery_dispatched(claim)
    store.mark_delivery_unknown(claim, "read timeout")

    record = store.resolve_unknown_as_sent(claim.record_id, "mid-manual")
    assert record.message_mid == "mid-manual"
    assert store.get_job_state(result.job_id) == "completed"


def test_retry_unknown_fails_when_related_job_is_missing(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    claim = store.begin_delivery("", dedup_ttl_sec=3600)
    assert claim is not None
    store.mark_delivery_dispatched(claim)
    store.mark_delivery_unknown(claim, "read timeout")

    with pytest.raises(StorageError, match="associated durable job"):
        store.retry_unknown(claim.record_id)
    assert len(store.list_unknown_deliveries()) == 1


def test_retry_job_clears_stale_claim_token(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = store.enqueue_job(
        event_key="megapbx:event:clear-token",
        kind="megapbx_event",
        payload='{"cmd":"history"}',
    )
    job = store.claim_job()
    assert job is not None
    store.retry_job(job, delay_sec=0, error_code="temporary", max_attempts=2)

    assert store.get_job_state(result.job_id) == "pending"
    assert store.get_job(result.job_id) is None


def test_cleanup_keeps_job_linked_to_unknown_delivery(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = store.enqueue_job(
        event_key="megapbx:event:retained",
        kind="megapbx_event",
        payload='{"cmd":"history"}',
    )
    job = store.claim_job()
    assert job is not None
    store.complete_job(job)
    claim = store.begin_delivery(
        "",
        dedup_ttl_sec=3600,
        job_event_key="megapbx:event:retained",
    )
    assert claim is not None
    store.mark_delivery_dispatched(claim)
    store.mark_delivery_unknown(claim, "read timeout")

    store.cleanup(max_age_sec=0, dedup_ttl_sec=0, now=2000000000)
    assert store.get_job_state(result.job_id) == "completed"


def test_prepared_delivery_lease_can_be_retried_but_inflight_cannot(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    prepared = store.begin_delivery(
        "call-prepared",
        dedup_ttl_sec=3600,
        prepared_ttl_sec=10,
        now=100,
    )
    assert prepared is not None
    store.reserve_counter(prepared.record_id, "+15555550123", "2026-09-24", now=100)
    store.set_delivery_payload(
        prepared,
        chat_id=42,
        phone="+15555550123",
        diversion="100",
        text="original",
        now=100,
    )
    retry = store.begin_delivery("call-prepared", dedup_ttl_sec=3600, now=111)
    assert retry is not None
    assert retry.record_id != prepared.record_id

    inflight = store.begin_delivery("call-inflight", dedup_ttl_sec=3600, now=200)
    assert inflight is not None
    store.reserve_counter(inflight.record_id, "+15555550123", "2026-09-24", now=200)
    store.set_delivery_payload(
        inflight,
        chat_id=42,
        phone="+15555550123",
        diversion="100",
        text="original",
        now=200,
    )
    store.mark_delivery_dispatched(inflight, now=200)
    assert store.begin_delivery("call-inflight", dedup_ttl_sec=3600, now=801) is None
    assert len(store.list_unknown_deliveries()) == 1


def test_record_claim_close_and_phone_lookup(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    claim = store.begin_delivery("call-1", dedup_ttl_sec=3600)
    assert claim is not None
    store.complete_delivery(
        claim,
        chat_id=42,
        message_mid="mid-1",
        phone="+15555550123",
        diversion="100",
        text="original",
    )

    record_claim = store.claim_record(claim.record_id)
    assert record_claim is not None
    assert store.claim_record(claim.record_id) is None
    store.close_record(record_claim, "Operator")

    assert store.get_by_id(claim.record_id).closed is True  # type: ignore[union-attr]
    assert store.find_recent_by_phone("+15555550123", 3600) is None


def test_deduplication_survives_notification_cleanup(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    claim = store.begin_delivery("seen-call", dedup_ttl_sec=3600)
    assert claim is not None
    store.complete_delivery(
        claim,
        chat_id=42,
        message_mid="mid-1",
        phone="+15555550123",
        diversion="100",
        text="original",
    )

    record_claim = store.claim_record(claim.record_id)
    assert record_claim is not None
    store.close_record(record_claim, "Operator")
    store.cleanup(max_age_sec=0, dedup_ttl_sec=3600)

    assert store.get_by_id(claim.record_id) is None
    assert store.begin_delivery("seen-call", dedup_ttl_sec=3600) is None


def test_cleanup_preserves_open_notifications(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    claim = store.begin_delivery("open-call", dedup_ttl_sec=3600)
    assert claim is not None
    store.complete_delivery(
        claim,
        chat_id=42,
        message_mid="mid-open",
        phone="+15555550123",
        diversion="100",
        text="original",
    )

    store.cleanup(max_age_sec=0, dedup_ttl_sec=3600)

    record = store.get_by_id(claim.record_id)
    assert record is not None and record.closed is False


def test_expired_callback_lease_cannot_be_released_by_old_owner(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.claim_callback("cb-race", now=100)
    second = store.claim_callback("cb-race", now=300)
    assert first.token is not None
    assert second.token is not None
    assert second.acquired is True

    store.release_callback("cb-race", first.token)
    third = store.claim_callback("cb-race", now=300)
    assert third.acquired is False
    store.mark_callback_external_confirmed("cb-race", second.token, now=200)
    store.complete_callback("cb-race", second.token, now=200)


def test_terminal_callback_state_is_not_reclaimed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    claim = store.claim_callback("cb-terminal")
    assert claim.token is not None
    store.mark_callback_terminal("cb-terminal", claim.token)
    assert store.claim_callback("cb-terminal").state == "terminal"


def test_callback_idempotency_can_be_released_or_completed(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    first = store.claim_callback("cb-1")
    assert first.acquired is True
    assert first.token is not None
    assert store.claim_callback("cb-1").acquired is False

    store.release_callback("cb-1", first.token)
    second = store.claim_callback("cb-1")
    assert second.acquired is True
    assert second.token is not None
    store.mark_callback_external_confirmed("cb-1", second.token)
    store.complete_callback("cb-1", second.token)

    completed = store.claim_callback("cb-1")
    assert completed.acquired is False
    assert completed.state == "committed"
