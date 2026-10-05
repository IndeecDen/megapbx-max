from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from megapbx_max.config import Settings
from megapbx_max.max_api.client import MaxApiError
from megapbx_max.max_api.models import Message, MessageBody, Recipient, Update
from megapbx_max.pbx import PbxDirectory
from megapbx_max.service import CallbackOutcomeUnknown, EventPending, MegapbxService
from megapbx_max.storage import SQLiteStore


class FakeDirectory(PbxDirectory):
    def __init__(self, user: str = "Operator", *, cancelled: bool = False) -> None:
        super().__init__(None, None)
        self.user = user
        self.cancelled = cancelled

    async def resolve_user(self, user: str) -> str:
        if self.cancelled:
            raise asyncio.CancelledError
        return self.user


class FakeMessenger:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self.fail_send: Exception | None = None
        self.fail_text_contains: str | None = None
        self.fail_edit: Exception | None = None
        self.answer_error: Exception | None = None
        self.send_delay = 0.0
        self.missing_body = False
        self._next_mid = 1

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        buttons: list[dict[str, Any]] | None = None,
        notify: bool = True,
    ) -> Message:
        await asyncio.sleep(self.send_delay)
        if self.fail_send is not None:
            raise self.fail_send
        if self.fail_text_contains is not None and self.fail_text_contains in text:
            raise MaxApiError("rejected", status_code=400)
        call = {
            "chat_id": chat_id,
            "text": text,
            "buttons": buttons or [],
            "notify": notify,
        }
        self.sent.append(call)
        mid = f"mid-{self._next_mid}"
        self._next_mid += 1
        return Message(
            recipient=Recipient(chat_id=chat_id, chat_type="chat"),
            timestamp=1_700_000_000_000,
            body=None if self.missing_body else MessageBody(mid=mid, seq=self._next_mid, text=text),
        )

    async def edit_message(
        self,
        message_id: str,
        text: str,
        *,
        buttons: list[dict[str, Any]] | None = None,
        remove_buttons: bool = False,
        notify: bool = True,
        chat_id: int | None = None,
    ) -> None:
        if self.fail_edit is not None:
            raise self.fail_edit
        self.edits.append(
            {
                "message_id": message_id,
                "text": text,
                "buttons": buttons or [],
                "chat_id": chat_id,
            }
        )

    async def answer_callback(
        self,
        callback_id: str,
        *,
        text: str | None = None,
        buttons: list[dict[str, Any]] | None = None,
        remove_buttons: bool = False,
        notification: str | None = None,
        chat_id: int | None = None,
    ) -> None:
        if self.answer_error is not None:
            raise self.answer_error
        self.answers.append(
            {
                "callback_id": callback_id,
                "text": text,
                "buttons": buttons or [],
                "notification": notification,
                "chat_id": chat_id,
            }
        )


def make_settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        {
            "MAX_BOT_TOKEN": "max-secret",
            "MAX_CHAT_ID": "42",
            "MAX_WEBHOOK_SECRET": "webhook_secret-1",
            "MEGAPBX_CRM_TOKEN": "crm-secret",
            "MEGAPBX_ALLOWED_DID": "100",
            "STATE_DB_PATH": str(tmp_path / "state.sqlite3"),
        }
    )


def make_service(tmp_path: Path, *, directory: FakeDirectory | None = None):
    settings = make_settings(tmp_path)
    storage = SQLiteStore(settings.state_db_path)
    storage.initialize()
    messenger = FakeMessenger()
    service = MegapbxService(settings, storage, messenger, directory or FakeDirectory())
    return service, storage, messenger


def missed_payload(call_id: str = "call-1", phone: str = "+15555550123") -> dict[str, Any]:
    return {
        "cmd": "history",
        "status": "Missed",
        "callid": call_id,
        "phone": phone,
        "groupRealName": "Support",
        "telnum": "100",
        "wait": 5,
        "duration": 0,
    }


def store_record(
    storage: SQLiteStore,
    call_id: str,
    *,
    mid: str,
    created_at: float | None = None,
    closed: bool = False,
) -> str:
    claim = storage.begin_delivery(call_id, dedup_ttl_sec=3600, now=created_at)
    assert claim is not None
    storage.complete_delivery(
        claim,
        chat_id=42,
        message_mid=mid,
        phone="+15555550123",
        diversion="100",
        text="original",
        now=created_at,
    )
    if closed:
        record_claim = storage.claim_record(claim.record_id, now=created_at)
        assert record_claim is not None
        storage.close_record(record_claim, "old", now=created_at)
    return claim.record_id


@pytest.mark.asyncio
async def test_duplicate_callid_is_sent_once(tmp_path: Path) -> None:
    service, _, messenger = make_service(tmp_path)
    messenger.send_delay = 0.01

    first, second = await asyncio.gather(
        service.send_missed_once(missed_payload()),
        service.send_missed_once(missed_payload()),
    )

    assert len(messenger.sent) == 1
    assert first.duplicate is False
    assert second.duplicate is True


@pytest.mark.asyncio
async def test_failed_send_releases_claim_and_counter(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    messenger.fail_send = MaxApiError("rejected", status_code=400)

    with pytest.raises(MaxApiError):
        await service.send_missed_once(missed_payload("failed"))

    messenger.fail_send = None
    result = await service.send_missed_once(missed_payload("successful"))
    assert result.duplicate is False
    assert "Пропущено (сегодня" not in messenger.sent[0]["text"]
    assert storage.get_latest_by_call_id("failed") is None


@pytest.mark.asyncio
async def test_parallel_same_phone_counter_rollback_is_correct(tmp_path: Path) -> None:
    service, _, messenger = make_service(tmp_path)
    messenger.send_delay = 0.01
    messenger.fail_text_contains = "Client A"

    results = await asyncio.gather(
        service.send_missed_once(missed_payload("call-a") | {"contact_name": "Client A"}),
        service.send_missed_once(missed_payload("call-b") | {"contact_name": "Client B"}),
        return_exceptions=True,
    )

    assert any(isinstance(result, MaxApiError) for result in results)
    assert len(messenger.sent) == 1
    assert "Client B" in messenger.sent[0]["text"]
    assert "Пропущено (сегодня" not in messenger.sent[0]["text"]


@pytest.mark.asyncio
async def test_failed_status_edit_does_not_change_stored_text(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    record_id = store_record(storage, "call-edit", mid="mid-edit")
    messenger.fail_edit = MaxApiError("edit rejected", status_code=400)

    with pytest.raises(MaxApiError):
        await service.update_failed_callback(
            {
                "cmd": "history",
                "type": "out",
                "missedStatus": "2",
                "status": "Busy",
                "phone": "+15555550123",
            }
        )

    record = storage.get_by_id(record_id)
    assert record is not None
    assert record.text == "original"
    assert storage.claim_record(record_id) is not None


@pytest.mark.asyncio
async def test_ambiguous_send_retains_claim_to_avoid_duplicate(tmp_path: Path) -> None:
    service, _, messenger = make_service(tmp_path)
    messenger.fail_send = MaxApiError("read timeout", ambiguous=True)

    with pytest.raises(MaxApiError):
        await service.send_missed_once(missed_payload("ambiguous"))

    messenger.fail_send = None
    duplicate = await service.send_missed_once(missed_payload("ambiguous"))
    assert duplicate.duplicate is True
    assert messenger.sent == []


@pytest.mark.asyncio
async def test_missing_mid_keeps_delivery_unknown_and_does_not_resend(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    messenger.missing_body = True

    with pytest.raises(MaxApiError) as exc_info:
        await service.send_missed_once(missed_payload("missing-mid"))
    assert exc_info.value.ambiguous is True
    assert len(storage.list_unknown_deliveries()) == 1

    duplicate = await service.send_missed_once(missed_payload("missing-mid"))
    assert duplicate.duplicate is True
    assert len(messenger.sent) == 1


@pytest.mark.asyncio
async def test_event_before_missed_is_retried_until_record_exists(tmp_path: Path) -> None:
    service, storage, _ = make_service(tmp_path)
    event = {"cmd": "event", "type": "ACCEPTED", "callid": "event-first"}

    with pytest.raises(EventPending):
        await service.handle_megapbx_payload(event)

    await service.send_missed_once(missed_payload("event-first"))
    await service.handle_megapbx_payload(event)
    record = storage.get_latest_by_call_id("event-first")
    assert record is not None and record.closed is True


@pytest.mark.asyncio
async def test_replayed_closed_callid_does_not_close_new_call(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    now = 1_800_000_000.0
    old_id = store_record(storage, "old-call", mid="mid-old", created_at=now - 2, closed=True)
    new_id = store_record(storage, "new-call", mid="mid-new", created_at=now - 1)

    await service.auto_close_by_event(
        {"cmd": "event", "type": "ACCEPTED", "callid": "old-call", "phone": "+15555550123"}
    )

    assert messenger.edits == []
    assert storage.get_by_id(old_id).closed is True  # type: ignore[union-attr]
    assert storage.get_by_id(new_id).closed is False  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_outgoing_event_does_not_close_call(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    record_id = store_record(storage, "call-1", mid="mid-1")

    await service.auto_close_by_event(
        {"cmd": "event", "type": "OUTGOING", "callid": "call-1", "phone": "+15555550123"}
    )

    assert messenger.edits == []
    assert storage.get_by_id(record_id).closed is False  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_history_missed_with_out_status_updates_same_notification(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    missed = missed_payload("late-history")
    result = await service.handle_megapbx_payload(missed)
    await service.handle_megapbx_payload(
        missed | {"type": "out", "missedStatus": "2", "status": "Busy"}
    )

    assert result.duplicate is False
    assert len(messenger.sent) == 1
    assert len(messenger.edits) == 1
    assert "↩️ Operator: ☎️ Занято" in messenger.edits[0]["text"]
    record = storage.get_latest_by_call_id("late-history")
    assert record is not None
    assert record.closed is False
    assert "↩️ Operator: ☎️ Занято" in record.text


@pytest.mark.asyncio
async def test_unknown_nonempty_callid_does_not_use_phone_fallback(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    record_id = store_record(storage, "current", mid="mid-current")

    await service.auto_close_by_event(
        {
            "cmd": "event",
            "type": "ACCEPTED",
            "callid": "unknown-after-cleanup",
            "phone": "+15555550123",
        }
    )

    assert messenger.edits == []
    assert storage.get_by_id(record_id).closed is False  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_cancelled_auto_close_releases_claim(tmp_path: Path) -> None:
    service, storage, _ = make_service(tmp_path, directory=FakeDirectory(cancelled=True))
    record_id = store_record(storage, "cancel-call", mid="mid-cancel")

    with pytest.raises(asyncio.CancelledError):
        await service.auto_close_by_event(
            {"cmd": "event", "type": "ACCEPTED", "callid": "cancel-call", "phone": "+15555550123"}
        )

    assert storage.claim_record(record_id) is not None


@pytest.mark.asyncio
async def test_max_callback_closes_message_once(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    await service.send_missed_once(missed_payload("callback-call"))
    button = messenger.sent[0]["buttons"][0]
    update = callback_update("cb-1", button["payload"], chat_id=42, mid="mid-1")

    await service.handle_update(update)
    await service.handle_update(update)

    record = storage.get_latest_by_call_id("callback-call")
    assert record is not None
    assert record.closed is True
    assert record.who == "Agent One"
    assert len(messenger.answers) == 1
    assert messenger.answers[0]["notification"] == "Отметили, спасибо!"
    assert messenger.answers[0]["buttons"][0]["payload"]


@pytest.mark.asyncio
async def test_callback_without_chat_context_fails_closed(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    await service.send_missed_once(missed_payload("callback-no-chat"))
    button = messenger.sent[0]["buttons"][0]
    update = Update.model_validate(
        {
            "update_type": "message_callback",
            "timestamp": 1_700_000_000_000,
            "callback": {
                "timestamp": 1_700_000_000_000,
                "callback_id": "cb-no-chat",
                "payload": button["payload"],
                "user": {"user_id": 9, "first_name": "Agent", "is_bot": False},
            },
        }
    )

    await service.handle_update(update)

    record = storage.get_latest_by_call_id("callback-no-chat")
    assert record is not None and record.closed is False
    assert messenger.answers[-1]["notification"] == "Это уведомление недоступно"


@pytest.mark.asyncio
async def test_max_callback_rejects_message_mid_mismatch(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    await service.send_missed_once(missed_payload("callback-mid"))
    button = messenger.sent[0]["buttons"][0]
    update = callback_update("cb-mid-mismatch", button["payload"], chat_id=42, mid="mid-other")

    await service.handle_update(update)

    record = storage.get_latest_by_call_id("callback-mid")
    assert record is not None and record.closed is False
    assert messenger.answers[-1]["notification"] == "Это уведомление недоступно"


@pytest.mark.asyncio
async def test_callback_405_is_recorded_as_external_unknown(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    await service.send_missed_once(missed_payload("callback-reconcile"))
    button = messenger.sent[0]["buttons"][0]
    update = callback_update("cb-reconcile", button["payload"], chat_id=42, mid="mid-1")
    messenger.answer_error = MaxApiError("method not allowed", status_code=405)

    with pytest.raises(CallbackOutcomeUnknown):
        await service.handle_update(update)

    record = storage.get_latest_by_call_id("callback-reconcile")
    assert record is not None
    assert record.closed is False
    assert storage.claim_callback("cb-reconcile").state == "external_unknown"


@pytest.mark.asyncio
async def test_ambiguous_callback_answer_is_not_replayed(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    await service.send_missed_once(missed_payload("callback-ambiguous"))
    button = messenger.sent[0]["buttons"][0]
    update = callback_update("cb-ambiguous", button["payload"], chat_id=42, mid="mid-1")
    messenger.answer_error = MaxApiError("response lost", status_code=500, ambiguous=True)

    with pytest.raises(CallbackOutcomeUnknown):
        await service.handle_update(update)

    record = storage.get_latest_by_call_id("callback-ambiguous")
    assert record is not None and record.closed is False
    assert storage.claim_callback("cb-ambiguous").state == "external_unknown"
    assert messenger.answers == []


@pytest.mark.asyncio
async def test_malformed_callback_answer_is_external_unknown(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    await service.send_missed_once(missed_payload("callback-malformed"))
    button = messenger.sent[0]["buttons"][0]
    update = callback_update("cb-malformed", button["payload"], chat_id=42, mid="mid-1")
    messenger.answer_error = MaxApiError("invalid success shape", ambiguous=True)

    with pytest.raises(CallbackOutcomeUnknown):
        await service.handle_update(update)

    assert storage.claim_callback("cb-malformed").state == "external_unknown"


@pytest.mark.asyncio
async def test_max_callback_rejects_other_chat(tmp_path: Path) -> None:
    service, storage, messenger = make_service(tmp_path)
    await service.send_missed_once(missed_payload())
    button = messenger.sent[0]["buttons"][0]
    update = callback_update("cb-other", button["payload"], chat_id=99, mid="mid-1")

    await service.handle_update(update)

    record = storage.get_latest_by_call_id("call-1")
    assert record is not None and record.closed is False
    assert messenger.answers[-1]["notification"] == "Это уведомление недоступно"


def callback_update(callback_id: str, raw_payload: str, *, chat_id: int, mid: str) -> Update:
    return Update.model_validate(
        {
            "update_type": "message_callback",
            "timestamp": 1_700_000_000_000,
            "callback": {
                "timestamp": 1_700_000_000_000,
                "callback_id": callback_id,
                "payload": raw_payload,
                "user": {
                    "user_id": 9,
                    "first_name": "Agent",
                    "last_name": "One",
                    "is_bot": False,
                },
            },
            "message": {
                "recipient": {"chat_id": chat_id, "chat_type": "chat"},
                "timestamp": 1_700_000_000_000,
                "body": {"mid": mid, "seq": 1, "text": "notification"},
            },
        }
    )


def test_button_payload_is_versioned_json(tmp_path: Path) -> None:
    service, _, messenger = make_service(tmp_path)
    button = service._callback_button("call_back", "record-1", "Я наберу")  # noqa: SLF001
    payload = json.loads(button["payload"])
    assert payload == {"v": 1, "action": "call_back", "record_id": "record-1"}
    assert messenger is not None
