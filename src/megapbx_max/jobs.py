from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import uuid
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from .config import Settings
from .domain import EnqueueResult, Job
from .max_api.client import MaxApiError
from .max_api.models import Update
from .pbx import as_text, is_allowed_destination, is_missed_call
from .service import (
    CallbackInProgress,
    CallbackOutcomeUnknown,
    CallbackTerminal,
    EventPending,
    MegapbxService,
    NotificationUnavailable,
)
from .storage import SQLiteStore, StorageError

logger = logging.getLogger(__name__)

_DENIED_PAYLOAD_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "authorization",
    "api_key",
    "apikey",
    "signature",
    "sign",
    "auth",
)
_MAX_PAYLOAD_DEPTH = 12


def _sanitize_payload(value: Any, depth: int = 0) -> Any:
    """Remove credential-like fields before a webhook is persisted for retry."""
    if depth > _MAX_PAYLOAD_DEPTH:
        return "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            normalized = key_text.casefold()
            if any(part in normalized for part in _DENIED_PAYLOAD_KEY_PARTS):
                continue
            result[key_text] = _sanitize_payload(nested, depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize_payload(item, depth + 1) for item in value]
    return value


class DurableJobQueue:
    def __init__(self, settings: Settings, storage: SQLiteStore) -> None:
        self.settings = settings
        self.storage = storage

    def enqueue_update(self, update: Update) -> EnqueueResult:
        payload = _canonical_json(_sanitize_payload(update.model_dump(mode="json", by_alias=True)))
        if update.update_type == "message_callback" and update.callback is not None:
            event_key = f"max:callback:{update.callback.callback_id}"
        else:
            event_key = f"max:event:{hashlib.sha256(payload.encode()).hexdigest()}"
        return self.storage.enqueue_job(event_key=event_key, kind="max_update", payload=payload)

    def enqueue_megapbx(self, payload: Mapping[str, Any]) -> EnqueueResult:
        sanitized = _sanitize_payload(dict(payload))
        canonical = _canonical_json(sanitized)
        if is_missed_call(dict(payload)) and is_allowed_destination(dict(payload), self.settings):
            call_id = as_text(payload.get("callid"))
            event_key = f"megapbx:missed:{call_id}" if call_id else f"megapbx:event:{uuid.uuid4()}"
            reopen_after = self.settings.missed_dedup_ttl_sec
        else:
            event_key = f"megapbx:event:{hashlib.sha256(canonical.encode()).hexdigest()}"
            reopen_after = None
        return self.storage.enqueue_job(
            event_key=event_key,
            kind="megapbx_event",
            payload=canonical,
            reopen_completed_after_sec=reopen_after,
        )


class QueueUpdateDispatcher:
    def __init__(self, queue: DurableJobQueue) -> None:
        self.queue = queue

    async def handle_update(self, update: Update) -> None:
        await asyncio.to_thread(self.queue.enqueue_update, update)


class JobWorker:
    def __init__(
        self,
        queue: DurableJobQueue,
        service: MegapbxService,
        *,
        max_attempts: int = 20,
        retry_base_sec: float = 1.0,
        retry_max_sec: float = 300.0,
        job_lease_sec: float = 300.0,
    ) -> None:
        self.queue = queue
        self.service = service
        self.storage = queue.storage
        self.max_attempts = max_attempts
        self.retry_base_sec = retry_base_sec
        self.retry_max_sec = retry_max_sec
        self.job_lease_sec = job_lease_sec

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except StorageError:
                logger.exception("Durable job worker storage is temporarily unavailable")
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.25)
                except TimeoutError:
                    pass

    async def run_once(self) -> bool:
        job = await asyncio.to_thread(self.storage.claim_job, lease_sec=self.job_lease_sec)
        if job is None:
            return False
        try:
            if job.kind == "max_update":
                update = Update.model_validate_json(job.payload)
                await self.service.handle_update(update)
            elif job.kind == "megapbx_event":
                payload = json.loads(job.payload)
                if not isinstance(payload, dict):
                    raise ValueError("MegaPBX job payload must be an object")
                await self.service.handle_megapbx_payload(payload, job_event_key=job.event_key)
            else:
                raise ValueError("Unknown durable job kind")
            await asyncio.to_thread(self.storage.complete_job, job)
        except asyncio.CancelledError:
            try:
                await asyncio.to_thread(self.storage.release_job, job)
            except StorageError:
                logger.exception("Cancelled durable job could not be released: job=%d", job.id)
            raise
        except CallbackInProgress:
            await self._retry(
                job,
                delay_sec=1.0,
                error_code="callback_in_progress",
                dead_state="pending",
                consume_attempt=False,
            )
        except (ValidationError, ValueError, TypeError):
            await self._retry(
                job,
                delay_sec=0,
                error_code="invalid_payload",
                dead_state="dead",
                max_attempts=1,
            )
        except (CallbackOutcomeUnknown, CallbackTerminal) as exc:
            await self._retry(
                job,
                delay_sec=0,
                error_code=type(exc).__name__,
                dead_state="dead",
                max_attempts=1,
            )
        except EventPending:
            await self._retry(
                job,
                delay_sec=self._delay(job.attempt),
                error_code="event_not_ready",
            )
        except NotificationUnavailable as exc:
            cause = exc.cause
            if isinstance(cause, MaxApiError) and not cause.ambiguous and cause.status_code in {400, 401, 403, 404, 405}:
                await self._retry(
                    job,
                    delay_sec=0,
                    error_code=f"max_http_{cause.status_code}",
                    dead_state="dead",
                    max_attempts=1,
                )
            else:
                await self._retry(job, delay_sec=self._delay(job.attempt), error_code=type(exc).__name__)
        except StorageError as exc:
            await self._retry(job, delay_sec=self._delay(job.attempt), error_code=type(exc).__name__)
        except MaxApiError as exc:
            if not exc.ambiguous and exc.status_code in {400, 401, 403, 404, 405}:
                await self._retry(
                    job,
                    delay_sec=0,
                    error_code=f"max_http_{exc.status_code}",
                    dead_state="dead",
                    max_attempts=1,
                )
            else:
                await self._retry(job, delay_sec=self._delay(job.attempt), error_code=type(exc).__name__)
        except Exception as exc:
            await self._retry(job, delay_sec=self._delay(job.attempt), error_code=type(exc).__name__)
        return True

    async def _retry(
        self,
        job: Job,
        *,
        delay_sec: float,
        error_code: str,
        dead_state: str = "dead",
        max_attempts: int | None = None,
        consume_attempt: bool = True,
    ) -> None:
        try:
            await asyncio.to_thread(
                self.storage.retry_job,
                job,
                delay_sec=delay_sec,
                error_code=error_code,
                max_attempts=self.max_attempts if max_attempts is None else max_attempts,
                dead_state=dead_state,
                consume_attempt=consume_attempt,
            )
        except asyncio.CancelledError:
            try:
                await asyncio.to_thread(self.storage.release_job, job)
            except StorageError:
                logger.exception("Cancelled durable retry could not be released: job=%d", job.id)
            raise
        except StorageError:
            logger.exception("Durable job retry state could not be saved: job=%d", job.id)
            return
        logger.warning(
            "Durable job deferred: id=%d kind=%s attempt=%d error=%s",
            job.id,
            job.kind,
            job.attempt,
            error_code,
        )

    def _delay(self, attempt: int) -> float:
        base = self.retry_base_sec * (2 ** min(attempt - 1, 10))
        return min(self.retry_max_sec, base * random.uniform(0.8, 1.2))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
