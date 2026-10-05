from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from .config import Settings
from .domain import DeliveryClaim
from .max_api.client import MaxApiError, MaxOperationError
from .max_api.models import CallbackPayload, Message, Update
from .pbx import (
    PbxDirectory,
    append_callback_status,
    as_text,
    build_missed_text,
    display_caller,
    extract_destination,
    fingerprint,
    is_allowed_destination,
    is_missed_call,
    normalize_phone,
    now_local,
    safe_log_value,
    user_full_name,
)
from .storage import SQLiteStore, StorageError

logger = logging.getLogger(__name__)


class NotificationUnavailable(RuntimeError):
    def __init__(self, cause: BaseException) -> None:
        super().__init__("MAX notification could not be delivered")
        self.cause = cause


class CallbackInProgress(RuntimeError):
    pass


class EventPending(RuntimeError):
    """A MegaPBX correlation event arrived before its notification record."""


class CallbackOutcomeUnknown(RuntimeError):
    """MAX may have accepted a callback answer, but the outcome is not known."""


class CallbackTerminal(RuntimeError):
    """A callback answer was rejected as a permanent request failure."""


class Messenger(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        buttons: Sequence[Mapping[str, Any]] | None = None,
        notify: bool = True,
    ) -> Message: ...

    async def edit_message(
        self,
        message_id: str,
        text: str,
        *,
        buttons: Sequence[Mapping[str, Any]] | None = None,
        remove_buttons: bool = False,
        notify: bool = True,
        chat_id: int | None = None,
    ) -> None: ...

    async def answer_callback(
        self,
        callback_id: str,
        *,
        text: str | None = None,
        buttons: Sequence[Mapping[str, Any]] | None = None,
        remove_buttons: bool = False,
        notification: str | None = None,
        chat_id: int | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WebhookResult:
    duplicate: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True}
        if self.duplicate is not None:
            result["duplicate"] = self.duplicate
        return result


class MegapbxService:
    def __init__(
        self,
        settings: Settings,
        storage: SQLiteStore,
        messenger: Messenger,
        directory: PbxDirectory,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.messenger = messenger
        self.directory = directory
        self._pending: dict[str, asyncio.Task[None]] = {}
        self._pending_lock = asyncio.Lock()
        self._phone_locks = tuple(asyncio.Lock() for _ in range(64))

    async def handle_megapbx_payload(
        self,
        payload: dict[str, Any],
        *,
        job_event_key: str | None = None,
    ) -> WebhookResult:
        cmd = as_text(payload.get("cmd")).casefold()
        status = as_text(payload.get("status")).casefold()
        notification_result: WebhookResult | None = None
        if is_missed_call(payload) and is_allowed_destination(payload, self.settings):
            try:
                notification_result = await self.send_missed_once(payload, job_event_key=job_event_key)
            except (MaxApiError, StorageError, RuntimeError) as exc:
                logger.error("Missed-call notification failed: %s", type(exc).__name__)
                raise NotificationUnavailable(exc) from exc

        if cmd == "event":
            if not await self.auto_close_by_event(payload):
                raise EventPending("MegaPBX event is waiting for its notification record")

        if (
            cmd == "history"
            and status == "success"
            and as_text(payload.get("type")).casefold() == "out"
            and as_text(payload.get("missedStatus")) == "2"
        ):
            if not await self.auto_close_by_callback(payload):
                raise EventPending("MegaPBX callback is waiting for its notification record")

        if (
            cmd == "history"
            and as_text(payload.get("type")).casefold() == "out"
            and status != "success"
            and as_text(payload.get("missedStatus")) == "2"
        ):
            if not await self.update_failed_callback(payload):
                raise EventPending("MegaPBX callback status is waiting for its notification record")

        return notification_result or WebhookResult()

    async def handle_update(self, update: Update) -> None:
        if update.update_type == "message_callback":
            if update.callback is None:
                raise ValueError("MAX callback update has no callback")
            await self.handle_callback(update)
            return
        if update.update_type in {"bot_added", "bot_started"} and update.chat_id is not None:
            logger.info("MAX %s received: chat_id=%d", update.update_type, update.chat_id)
            return
        if update.update_type in {"bot_removed", "bot_admin_permissions_changed"}:
            logger.info("MAX %s received: chat_id=%s", update.update_type, update.chat_id)

    async def send_missed_once(
        self,
        payload: dict[str, Any],
        *,
        job_event_key: str | None = None,
    ) -> WebhookResult:
        call_id = as_text(payload.get("callid"))
        if not call_id:
            logger.warning("Missed webhook has no callid; durable deduplication unavailable")
            claim = self.storage.begin_delivery(
                "",
                dedup_ttl_sec=self.settings.missed_dedup_ttl_sec,
                job_event_key=job_event_key,
            )
            if claim is None:  # pragma: no cover - empty call IDs never conflict
                return WebhookResult(duplicate=True)
            await self._send_new_missed(payload, claim)
            return WebhookResult(duplicate=False)

        async with self._pending_lock:
            task = self._pending.get(call_id)
            owner = task is None
            if owner:
                claim = self.storage.begin_delivery(
                    call_id,
                    dedup_ttl_sec=self.settings.missed_dedup_ttl_sec,
                    job_event_key=job_event_key,
                )
                if claim is None:
                    return WebhookResult(duplicate=True)
                task = asyncio.create_task(self._send_new_missed(payload, claim))
                self._pending[call_id] = task
                task.add_done_callback(partial(self._forget_pending, call_id))

        assert task is not None
        try:
            await asyncio.shield(task)
            return WebhookResult(duplicate=not owner)
        finally:
            if task.done():
                async with self._pending_lock:
                    if self._pending.get(call_id) is task:
                        self._pending.pop(call_id, None)

    async def aclose(self, grace_period_sec: float = 10.0) -> None:
        tasks = list(self._pending.values())
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=max(0.0, grace_period_sec))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if not task.cancelled():
                task.exception()

    def _forget_pending(self, call_id: str, task: asyncio.Task[None]) -> None:
        if self._pending.get(call_id) is task:
            self._pending.pop(call_id, None)

    async def _send_new_missed(self, payload: dict[str, Any], claim: DeliveryClaim) -> None:
        message: Message | None = None
        dispatched = False
        try:
            caller, phone = display_caller(payload)
            phone_lock = self._phone_locks[hash(phone or "<empty-phone>") % len(self._phone_locks)]
            async with phone_lock:
                destination = extract_destination(payload, self.settings, self.directory)
                call_day = now_local(self.settings.tz_offset_hours).strftime("%Y-%m-%d")
                counter = self.storage.reserve_counter(claim.record_id, phone, call_day)
                text = build_missed_text(
                    caller,
                    destination,
                    wait=payload.get("wait"),
                    duration=payload.get("duration"),
                    today=counter.today,
                    total=counter.total,
                    offset_hours=self.settings.tz_offset_hours,
                )
                button = self._callback_button("call_back", claim.record_id, "📲 Я наберу")
                self.storage.set_delivery_payload(
                    claim,
                    chat_id=self.settings.max_chat_id,
                    phone=phone,
                    diversion=as_text(payload.get("diversion")),
                    text=text,
                )
                self.storage.mark_delivery_dispatched(claim)
                dispatched = True
                message = await self.messenger.send_message(
                    self.settings.max_chat_id,
                    text,
                    buttons=[button],
                )
                if message.body is None or not message.body.mid:
                    raise MaxApiError("MAX send response has no message body.mid", ambiguous=True)
                await self._complete_delivery_with_retry(
                    claim,
                    message_mid=message.body.mid,
                    phone=phone,
                    diversion=as_text(payload.get("diversion")),
                    text=text,
                )
                logger.info(
                    "Missed-call notification sent: callid=%s message=%s",
                    fingerprint(claim.call_id),
                    fingerprint(message.body.mid),
                )
        except BaseException as exc:
            deterministic = isinstance(exc, MaxApiError) and not exc.ambiguous
            if dispatched and not deterministic:
                try:
                    self.storage.mark_delivery_unknown(claim, type(exc).__name__)
                except Exception as storage_exc:
                    logger.error(
                        "Delivery outcome is uncertain and unknown state could not be saved: "
                        "callid=%s error=%s state_error=%s",
                        fingerprint(claim.call_id),
                        type(exc).__name__,
                        type(storage_exc).__name__,
                    )
            else:
                await self._release_delivery(claim)
            logger.error(
                "Missed-call delivery failed: callid=%s error=%s ambiguous=%s",
                fingerprint(claim.call_id),
                type(exc).__name__,
                dispatched and not deterministic,
            )
            raise

    async def _complete_delivery_with_retry(
        self,
        claim: DeliveryClaim,
        *,
        message_mid: str,
        phone: str,
        diversion: str,
        text: str,
    ) -> None:
        for attempt in range(3):
            try:
                self.storage.complete_delivery(
                    claim,
                    chat_id=self.settings.max_chat_id,
                    message_mid=message_mid,
                    phone=phone,
                    diversion=diversion,
                    text=text,
                )
                return
            except StorageError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.05 * (2**attempt))

    async def _release_delivery(self, claim: DeliveryClaim) -> None:
        try:
            self.storage.fail_delivery(claim)
        except Exception as exc:
            logger.error("Failed to release delivery claim: %s", type(exc).__name__)

    async def auto_close_by_event(self, payload: dict[str, Any]) -> bool:
        event_type = as_text(payload.get("type")).upper()
        if event_type not in {"ACCEPTED", "COMPLETED"}:
            return True
        call_id = as_text(payload.get("callid"))
        if call_id:
            record = self.storage.get_latest_by_call_id(call_id)
        else:
            record = self._recent_record(payload.get("phone"))
        if record is None:
            return False
        return await self._close_record(record.id, as_text(payload.get("user")), source="event")

    async def auto_close_by_callback(self, payload: dict[str, Any]) -> bool:
        record = self._recent_record(payload.get("phone"))
        if record is None:
            return False
        return await self._close_record(record.id, as_text(payload.get("user")), source="callback")

    async def update_failed_callback(self, payload: dict[str, Any]) -> bool:
        record = self._recent_record(payload.get("phone"))
        if record is None:
            return False
        claim = self.storage.claim_record(record.id)
        if claim is None:
            return bool(record.closed)
        try:
            who = await self.directory.resolve_user(as_text(payload.get("user")))
            new_text = append_callback_status(
                claim.record.text,
                who,
                as_text(payload.get("status")),
            )
            button = self._callback_button("call_back", claim.record.id, "📲 Я наберу")
            await self.messenger.edit_message(
                claim.record.message_mid,
                new_text,
                buttons=[button],
                chat_id=claim.record.chat_id,
            )
            self.storage.update_record_text(claim, new_text)
            logger.info(
                "Missed call callback status updated: callid=%s status=%s",
                fingerprint(claim.record.call_id),
                safe_log_value(payload.get("status")),
            )
        except asyncio.CancelledError:
            self.storage.release_record(claim.record.id, claim.token)
            raise
        except Exception as exc:
            self.storage.release_record(claim.record.id, claim.token)
            logger.warning("Missed call callback status update failed: %s", type(exc).__name__)
            raise
        return True

    async def _answer_callback_safely(
        self,
        callback_id: str,
        callback_token: str,
        **kwargs: Any,
    ) -> None:
        try:
            await self.messenger.answer_callback(callback_id, **kwargs)
        except MaxApiError as exc:
            if exc.status_code == 429:
                raise
            if (
                exc.ambiguous
                or exc.status_code == 405
                or exc.status_code == 408
                or (exc.status_code is not None and exc.status_code >= 500)
            ):
                self.storage.mark_callback_external_unknown(callback_id, callback_token)
                raise CallbackOutcomeUnknown("MAX callback answer outcome is unknown") from exc
            if isinstance(exc, MaxOperationError) or exc.status_code is not None:
                self.storage.mark_callback_terminal(callback_id, callback_token)
                raise CallbackTerminal("MAX callback answer was permanently rejected") from exc
            # A connection failure without an HTTP response is safe to retry;
            # unlike a 2xx response with an invalid shape, it does not prove that
            # MAX accepted the callback answer.
            raise

    async def handle_callback(self, update: Update) -> None:
        callback = update.callback
        if callback is None or callback.payload is None or callback.user.is_bot:
            return
        payload = self._parse_callback_payload(callback.payload)
        if payload is None:
            logger.warning("Ignoring malformed MAX callback payload")
            return

        callback_claim = self.storage.claim_callback(callback.callback_id)
        if not callback_claim.acquired:
            if callback_claim.state == "processing":
                raise CallbackInProgress("MAX callback is already being processed")
            return
        callback_token = callback_claim.token
        if callback_token is None:
            raise StorageError("MAX callback claim has no ownership token")
        record = None
        record_claim = None
        try:
            record = self.storage.get_by_id(payload.record_id)
            chat_id = update.message.recipient.chat_id if update.message is not None else update.chat_id
            if callback_claim.state == "external_confirmed":
                await self._commit_external_callback(callback.callback_id, callback_token, record)
                return

            if chat_id is None:
                await self._answer_and_commit(
                    callback.callback_id,
                    callback_token,
                    "Это уведомление недоступно",
                    None,
                )
                return

            if record is not None and chat_id != record.chat_id:
                await self._answer_and_commit(
                    callback.callback_id,
                    callback_token,
                    "Это уведомление недоступно",
                    chat_id,
                )
                return
            if chat_id is not None and chat_id != self.settings.max_chat_id:
                await self._answer_and_commit(
                    callback.callback_id,
                    callback_token,
                    "Это уведомление недоступно",
                    chat_id,
                )
                return
            if (
                record is not None
                and update.message is not None
                and update.message.body is not None
                and record.message_mid != update.message.body.mid
            ):
                await self._answer_and_commit(
                    callback.callback_id,
                    callback_token,
                    "Это уведомление недоступно",
                    chat_id or record.chat_id,
                )
                return

            if payload.action == "call_back_done":
                await self._answer_and_commit(
                    callback.callback_id,
                    callback_token,
                    "Уже отмечено как перезвонивший!",
                    chat_id,
                )
                return

            if record is None and update.message is not None:
                if chat_id is None or update.message.body is None:
                    await self._answer_and_commit(
                        callback.callback_id,
                        callback_token,
                        "Не удалось определить сообщение",
                        chat_id,
                    )
                    return
                record = self.storage.get_by_message(chat_id, update.message.body.mid)
            if record is None:
                await self._answer_and_commit(
                    callback.callback_id,
                    callback_token,
                    "Не удалось определить сообщение",
                    chat_id,
                )
                return

            record_claim = self.storage.claim_record(record.id)
            if record_claim is None:
                await self._answer_and_commit(
                    callback.callback_id,
                    callback_token,
                    "Уже отмечено как перезвонивший!",
                    chat_id or record.chat_id,
                )
                return

            who = user_full_name(callback.user)
            await self._answer_callback_safely(
                callback.callback_id,
                callback_token,
                text=record_claim.record.text,
                buttons=[
                    self._callback_button(
                        "call_back_done",
                        record_claim.record.id,
                        f"🤳 Перезвонил {who}",
                    )
                ],
                notification="Отметили, спасибо!",
                chat_id=chat_id or record_claim.record.chat_id,
            )
            self.storage.mark_callback_external_confirmed(callback.callback_id, callback_token, who=who)
            self.storage.complete_callback(
                callback.callback_id,
                callback_token,
                record=record_claim,
                who=who,
            )
            logger.info(
                "Missed call closed by MAX callback: callid=%s operator=%s",
                fingerprint(record_claim.record.call_id),
                fingerprint(callback.user.user_id),
            )
        except asyncio.CancelledError:
            if record_claim is not None:
                self.storage.release_record(record_claim.record.id, record_claim.token)
            self.storage.release_callback(callback.callback_id, callback_token)
            raise
        except Exception:
            if record_claim is not None:
                self.storage.release_record(record_claim.record.id, record_claim.token)
            self.storage.release_callback(callback.callback_id, callback_token)
            raise

    async def _answer_and_commit(
        self,
        callback_id: str,
        callback_token: str,
        notification: str,
        chat_id: int | None,
    ) -> None:
        await self._answer_callback_safely(
            callback_id,
            callback_token,
            notification=notification,
            chat_id=chat_id,
        )
        self.storage.mark_callback_external_confirmed(callback_id, callback_token)
        self.storage.complete_callback(callback_id, callback_token)

    async def _commit_external_callback(
        self,
        callback_id: str,
        callback_token: str,
        record: Any,
    ) -> None:
        if record is not None and not record.closed:
            record_claim = self.storage.claim_record(record.id)
            if record_claim is None:
                raise CallbackInProgress("Notification is still being closed")
            self.storage.complete_callback(callback_id, callback_token, record=record_claim, who=record.who)
        else:
            self.storage.complete_callback(callback_id, callback_token)

    async def _close_record(self, record_id: str, user: str, *, source: str) -> bool:
        record = self.storage.get_by_id(record_id)
        if record is None:
            return False
        if record.closed:
            return True
        claim = self.storage.claim_record(record_id)
        if claim is None:
            return False
        try:
            who = await self.directory.resolve_user(user)
            await self.messenger.edit_message(
                claim.record.message_mid,
                claim.record.text,
                buttons=[
                    self._callback_button(
                        "call_back_done",
                        claim.record.id,
                        f"🤳 Перезвонил {who}",
                    )
                ],
                chat_id=claim.record.chat_id,
            )
            self.storage.close_record(claim, who)
            logger.info(
                "Missed call auto-closed by %s: callid=%s",
                source,
                fingerprint(claim.record.call_id),
            )
        except asyncio.CancelledError:
            self.storage.release_record(claim.record.id, claim.token)
            raise
        except Exception as exc:
            self.storage.release_record(claim.record.id, claim.token)
            logger.warning("Missed call auto-close by %s failed: %s", source, type(exc).__name__)
            raise
        return True

    def _recent_record(self, phone: Any):
        normalized = normalize_phone(as_text(phone)) if as_text(phone) else ""
        return self.storage.find_recent_by_phone(normalized, self.settings.missed_max_age_sec)

    def _parse_callback_payload(self, raw: str) -> CallbackPayload | None:
        try:
            return CallbackPayload.model_validate_json(raw)
        except ValidationError:
            return None

    def _callback_button(
        self,
        action: Literal["call_back", "call_back_done"],
        record_id: str,
        text: str,
    ) -> dict[str, Any]:
        payload = CallbackPayload(action=action, record_id=record_id)
        encoded = json.dumps(payload.model_dump(by_alias=True), ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 1024:  # defensive; a UUID payload is far below the limit
            raise ValueError("MAX callback payload exceeds 1024 characters")
        button_text = as_text(text)
        if len(button_text) > 128:
            button_text = button_text[:127].rstrip() + "…"
        return {
            "type": "callback",
            "text": button_text,
            "payload": encoded,
        }
