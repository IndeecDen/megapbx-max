from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from megapbx_max.config import Settings
from megapbx_max.jobs import DurableJobQueue, JobWorker
from megapbx_max.max_api.client import MaxApiError
from megapbx_max.max_api.models import Update
from megapbx_max.service import CallbackInProgress, NotificationUnavailable
from megapbx_max.storage import SQLiteStore


class Service:
    def __init__(self) -> None:
        self.updates: list[Update] = []
        self.events: list[dict[str, Any]] = []
        self.fail = False
        self.callback_in_progress = False
        self.callback_attempts = 0

    async def handle_update(self, update: Update) -> None:
        if self.fail:
            raise RuntimeError("test failure")
        if self.callback_in_progress:
            self.callback_attempts += 1
            if self.callback_attempts == 1:
                raise CallbackInProgress("callback is busy")
        self.updates.append(update)

    async def handle_megapbx_payload(
        self,
        payload: dict[str, Any],
        *,
        job_event_key: str | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("test failure")
        self.events.append(payload)


def make(tmp_path: Path) -> tuple[Settings, SQLiteStore, DurableJobQueue, Service]:
    settings = Settings.from_env(
        {
            "MAX_BOT_TOKEN": "max-secret",
            "MAX_CHAT_ID": "42",
            "MEGAPBX_CRM_TOKEN": "crm-secret",
            "MEGAPBX_ALLOWED_DID": "100",
            "STATE_DB_PATH": str(tmp_path / "state.sqlite3"),
        }
    )
    storage = SQLiteStore(settings.state_db_path)
    storage.initialize()
    return settings, storage, DurableJobQueue(settings, storage), Service()


def update() -> Update:
    return Update.model_validate(
        {
            "update_type": "bot_started",
            "timestamp": 1_700_000_000_000,
            "chat_id": 42,
            "user": {"user_id": 1, "first_name": "User", "is_bot": False},
        }
    )


@pytest.mark.asyncio
async def test_max_update_is_durable_and_deduplicated(tmp_path: Path) -> None:
    settings, storage, queue, service = make(tmp_path)
    first = queue.enqueue_update(update())
    second = queue.enqueue_update(update())

    assert first.inserted is True
    assert second.inserted is False
    assert first.job_id == second.job_id

    worker = JobWorker(queue, service)  # type: ignore[arg-type]
    assert await worker.run_once() is True
    assert len(service.updates) == 1
    assert await worker.run_once() is False
    assert storage.ping() is None


@pytest.mark.asyncio
async def test_durable_payloads_redact_credentials_before_persisting(tmp_path: Path) -> None:
    _, storage, queue, _ = make(tmp_path)
    result = queue.enqueue_megapbx(
        {
            "cmd": "history",
            "status": "Missed",
            "callid": "redact-1",
            "telnum": "100",
            "token": "super-secret",
            "nested": {"api_key": "nested-secret", "safe": "kept"},
        }
    )
    job = storage.claim_job()
    assert job is not None
    assert "super-secret" not in job.payload
    assert "nested-secret" not in job.payload
    assert "kept" in job.payload
    assert result.inserted is True
    storage.complete_job(job)

    update_payload = update().model_dump(mode="json")
    update_payload["api_key"] = "max-secret"
    queue.enqueue_update(Update.model_validate(update_payload))
    max_job = storage.claim_job()
    assert max_job is not None
    assert "max-secret" not in max_job.payload


@pytest.mark.asyncio
async def test_missed_job_reopens_after_dedup_ttl(tmp_path: Path) -> None:
    settings, storage, queue, service = make(tmp_path)
    settings = replace(settings, missed_dedup_ttl_sec=0)
    queue = DurableJobQueue(settings, storage)
    payload = {"cmd": "history", "status": "Missed", "callid": "ttl-zero", "telnum": "100"}
    worker = JobWorker(queue, service)  # type: ignore[arg-type]

    assert queue.enqueue_megapbx(payload).inserted is True
    assert await worker.run_once() is True
    reopened = queue.enqueue_megapbx(payload)
    assert reopened.inserted is False
    assert reopened.reopened is True
    assert await worker.run_once() is True
    assert len(service.events) == 2


@pytest.mark.asyncio
async def test_megapbx_job_is_processed_by_worker(tmp_path: Path) -> None:
    _, _, queue, service = make(tmp_path)
    result = queue.enqueue_megapbx(
        {"cmd": "history", "status": "Missed", "callid": "job-1", "telnum": "100"}
    )
    assert result.inserted is True

    worker = JobWorker(queue, service)  # type: ignore[arg-type]
    assert await worker.run_once() is True
    assert service.events[0]["callid"] == "job-1"


@pytest.mark.asyncio
async def test_worker_associates_unknown_delivery_without_callid(tmp_path: Path) -> None:
    settings, storage, queue, _ = make(tmp_path)
    result = queue.enqueue_megapbx(
        {"cmd": "history", "status": "Missed", "telnum": "100"}
    )
    worker = JobWorker(
        queue,
        UnknownDeliveryService(storage),  # type: ignore[arg-type]
        retry_base_sec=0,
        retry_max_sec=0,
    )

    assert await worker.run_once() is True
    unknown = storage.list_unknown_deliveries()
    assert len(unknown) == 1
    storage.retry_unknown(unknown[0].record_id)
    assert storage.get_job_state(result.job_id) == "pending"


class UnknownDeliveryService:
    def __init__(self, storage: SQLiteStore) -> None:
        self.storage = storage

    async def handle_update(self, update: Update) -> None:
        return None

    async def handle_megapbx_payload(
        self,
        payload: dict[str, Any],
        *,
        job_event_key: str | None = None,
    ) -> None:
        assert job_event_key is not None
        claim = self.storage.begin_delivery(
            "",
            dedup_ttl_sec=3600,
            job_event_key=job_event_key,
        )
        assert claim is not None
        self.storage.mark_delivery_dispatched(claim)
        self.storage.mark_delivery_unknown(claim, "read timeout")
        raise NotificationUnavailable(MaxApiError("response lost", ambiguous=True))


class BlockingService(Service):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = True

    async def handle_megapbx_payload(
        self,
        payload: dict[str, Any],
        *,
        job_event_key: str | None = None,
    ) -> None:
        if self.block:
            self.started.set()
            await self.release.wait()
        await super().handle_megapbx_payload(payload, job_event_key=job_event_key)


@pytest.mark.asyncio
async def test_cancelled_worker_releases_claimed_job(tmp_path: Path) -> None:
    settings, storage, queue, _ = make(tmp_path)
    service = BlockingService()
    result = queue.enqueue_megapbx({"cmd": "history", "status": "Missed", "callid": "cancel-job", "telnum": "100"})
    worker = JobWorker(queue, service, retry_base_sec=0, retry_max_sec=0)  # type: ignore[arg-type]
    task = asyncio.create_task(worker.run_once())
    await service.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert storage.get_job_state(result.job_id) == "pending"
    service.block = False
    service.release.set()
    assert await worker.run_once() is True
    assert len(service.events) == 1


@pytest.mark.asyncio
async def test_callback_in_progress_is_retried_without_dead_letter(tmp_path: Path) -> None:
    settings, storage, queue, service = make(tmp_path)
    service.callback_in_progress = True
    result = queue.enqueue_update(update())
    worker = JobWorker(queue, service, max_attempts=1, retry_base_sec=0, retry_max_sec=0)  # type: ignore[arg-type]

    assert await worker.run_once() is True
    assert storage.get_job_state(result.job_id) == "pending"
    await asyncio.sleep(1.05)
    assert await worker.run_once() is True
    assert len(service.updates) == 1


@pytest.mark.asyncio
async def test_callback_in_progress_does_not_consume_retry_budget(tmp_path: Path) -> None:
    settings, storage, queue, _ = make(tmp_path)
    service = BusyThenFailureService(busy_count=3)
    result = queue.enqueue_update(update())
    worker = JobWorker(
        queue,
        service,  # type: ignore[arg-type]
        max_attempts=2,
        retry_base_sec=0,
        retry_max_sec=0,
    )

    for _ in range(3):
        assert await worker.run_once() is True
        await asyncio.sleep(1.05)
    assert storage.get_job_state(result.job_id) == "pending"
    assert await worker.run_once() is True
    assert storage.get_job_state(result.job_id) == "pending"


class BusyThenFailureService(Service):
    def __init__(self, busy_count: int) -> None:
        super().__init__()
        self.busy_count = busy_count
        self.busy_seen = 0

    async def handle_update(self, update: Update) -> None:
        if self.busy_seen < self.busy_count:
            self.busy_seen += 1
            raise CallbackInProgress("callback is busy")
        self.fail = True
        await super().handle_update(update)


class PermanentFailureService:
    async def handle_update(self, update: Update) -> None:
        return None

    async def handle_megapbx_payload(
        self,
        payload: dict[str, Any],
        *,
        job_event_key: str | None = None,
    ) -> None:
        raise NotificationUnavailable(MaxApiError("bad request", status_code=400))


@pytest.mark.asyncio
async def test_permanent_max_error_dead_letters_without_retry(tmp_path: Path) -> None:
    _, storage, queue, _ = make(tmp_path)
    result = queue.enqueue_megapbx({"cmd": "history", "status": "Missed", "callid": "permanent", "telnum": "100"})
    worker = JobWorker(queue, PermanentFailureService(), max_attempts=20, retry_base_sec=0, retry_max_sec=0)  # type: ignore[arg-type]

    assert await worker.run_once() is True
    assert storage.get_job_state(result.job_id) == "dead"
    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_failed_job_is_retryable_until_dead_letter(tmp_path: Path) -> None:
    _, storage, queue, service = make(tmp_path)
    service.fail = True
    result = queue.enqueue_megapbx({"cmd": "history", "status": "Missed", "callid": "job-fail", "telnum": "100"})
    worker = JobWorker(queue, service, max_attempts=1, retry_base_sec=0, retry_max_sec=0)  # type: ignore[arg-type]

    assert await worker.run_once() is True
    assert storage.get_job_state(result.job_id) == "dead"

    # A later platform delivery reopens a dead job instead of silently losing it.
    assert queue.enqueue_megapbx(
        {"cmd": "history", "status": "Missed", "callid": "job-fail", "telnum": "100"}
    ).inserted is False
    assert storage.get_job_state(result.job_id) == "pending"
