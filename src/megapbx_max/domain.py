from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    id: str
    call_id: str
    chat_id: int
    message_mid: str
    phone: str
    diversion: str
    text: str
    created_at: float
    closed: bool
    who: str | None


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    record_id: str
    call_id: str


@dataclass(frozen=True, slots=True)
class CounterCounts:
    today: int
    total: int


@dataclass(frozen=True, slots=True)
class RecordClaim:
    record: NotificationRecord
    token: str


@dataclass(frozen=True, slots=True)
class CallbackClaim:
    callback_id: str
    acquired: bool
    token: str | None = None
    state: str = "new"


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    event_key: str
    kind: str
    payload: str
    attempt: int
    claim_token: str


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: int
    inserted: bool
    reopened: bool = False


@dataclass(frozen=True, slots=True)
class UnknownDelivery:
    record_id: str
    call_id: str
    state: str
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class CleanupResult:
    notifications: int = 0
    deduplication: int = 0
    callbacks: int = 0
    jobs: int = 0
    unknown_deliveries: int = 0
