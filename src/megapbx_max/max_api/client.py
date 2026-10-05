from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Self

import httpx
from pydantic import ValidationError

from .models import BotInfo, Message, SendMessageResult, SimpleResult, UpdateList

_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SAFE_POST_RETRY_STATUSES = frozenset({429})
_RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


class MaxApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.ambiguous = ambiguous


class MaxOperationError(MaxApiError):
    """MAX returned HTTP 200 but explicitly reported success=false."""


class _RateLimiter:
    """Space requests to stay below the documented global 30 rps limit."""

    def __init__(self, requests_per_second: float = 30.0) -> None:
        self._interval = 1.0 / requests_per_second
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
            self._next_allowed = max(now, self._next_allowed) + self._interval


class _KeyedRateLimiter:
    """Enforce the documented two message operations/second limit per chat."""

    def __init__(self, operations_per_second: float = 2.0) -> None:
        self._interval = 1.0 / operations_per_second
        self._next_allowed: dict[int, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: int) -> None:
        async with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_allowed.get(key, now))
            self._next_allowed[key] = scheduled + self._interval
            delay = scheduled - now
        if delay > 0:
            await asyncio.sleep(delay)


class MaxApiClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://platform-api2.max.ru",
        max_retries: int = 2,
        retry_base_sec: float = 0.5,
        retry_max_sec: float = 8.0,
        timeout_sec: float = 10.0,
        requests_per_second: float = 30.0,
        verify: str | bool = True,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise ValueError("MAX bot token must not be empty")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if not math.isfinite(retry_base_sec) or not math.isfinite(retry_max_sec):
            raise ValueError("retry delays must be finite")
        if retry_base_sec < 0 or retry_max_sec < 0:
            raise ValueError("retry delays must not be negative")
        if not math.isfinite(timeout_sec) or not math.isfinite(requests_per_second):
            raise ValueError("timeout and requests_per_second must be finite")
        if timeout_sec <= 0 or requests_per_second <= 0:
            raise ValueError("timeout and requests_per_second must be positive")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": token, "Accept": "application/json"},
            timeout=timeout_sec,
            verify=verify,
        )
        if http_client is not None:
            # Mutating an injected client is intentional: every request must be authenticated.
            http_client.headers["Authorization"] = token
        self._max_retries = max_retries
        self._retry_base_sec = retry_base_sec
        self._retry_max_sec = retry_max_sec
        self._rate_limiter = _RateLimiter(requests_per_second)
        self._chat_rate_limiter = _KeyedRateLimiter(2.0)

    @property
    def base_url(self) -> str:
        return str(self._client.base_url).rstrip("/")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_me(self) -> BotInfo:
        data = await self._request("GET", "/me")
        try:
            return BotInfo.model_validate(data)
        except ValidationError as exc:
            raise MaxApiError("MAX /me response has an unexpected shape") from exc

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        buttons: Sequence[Mapping[str, Any]] | None = None,
        notify: bool = True,
    ) -> Message:
        if not text:
            raise ValueError("MAX message text must not be empty")
        if len(text) > 4000:
            raise ValueError("MAX message text must contain at most 4000 characters")
        body: dict[str, Any] = {"text": text, "notify": notify, "format": "html"}
        if buttons:
            body["attachments"] = [
                {
                    "type": "inline_keyboard",
                    "payload": {"buttons": [list(buttons)]},
                }
            ]
        data = await self._request(
            "POST",
            "/messages",
            params={"chat_id": chat_id, "disable_link_preview": True},
            json_body=body,
            chat_id=chat_id,
            retry_statuses=_SAFE_POST_RETRY_STATUSES,
        )
        try:
            message = SendMessageResult.model_validate(data).message
        except ValidationError as exc:
            raise MaxApiError("MAX send response has an unexpected shape", ambiguous=True) from exc
        if message.body is None or not message.body.mid:
            raise MaxApiError("MAX send response has no message body.mid", ambiguous=True)
        return message

    async def edit_message(
        self,
        message_id: str,
        text: str,
        *,
        buttons: Sequence[Mapping[str, Any]] | None = None,
        remove_buttons: bool = False,
        notify: bool = True,
        chat_id: int | None = None,
    ) -> None:
        if len(text) > 4000:
            raise ValueError("MAX message text must contain at most 4000 characters")
        body: dict[str, Any] = {"text": text, "notify": notify, "format": "html"}
        if buttons:
            body["attachments"] = [
                {
                    "type": "inline_keyboard",
                    "payload": {"buttons": [list(buttons)]},
                }
            ]
        elif remove_buttons:
            body["attachments"] = []
        data = await self._request(
            "PUT",
            "/messages",
            params={"message_id": message_id, "disable_link_preview": True},
            json_body=body,
            chat_id=chat_id,
        )
        _raise_for_simple_result(data, "edit message")

    async def answer_callback(
        self,
        callback_id: str,
        *,
        text: str | None = None,
        buttons: Sequence[Mapping[str, Any]] | None = None,
        remove_buttons: bool = False,
        notification: str | None = None,
        chat_id: int | None = None,
    ) -> None:
        if text is not None and len(text) > 4000:
            raise ValueError("MAX message text must contain at most 4000 characters")
        body: dict[str, Any] = {}
        if text is not None:
            message: dict[str, Any] = {"text": text, "format": "html"}
            if buttons:
                message["attachments"] = [
                    {"type": "inline_keyboard", "payload": {"buttons": [list(buttons)]}}
                ]
            elif remove_buttons:
                message["attachments"] = []
            body["message"] = message
        if notification is not None:
            body["notification"] = notification
        data = await self._request(
            "POST",
            "/answers",
            params={"callback_id": callback_id, "disable_link_preview": True},
            json_body=body,
            chat_id=chat_id,
            retry_statuses=_SAFE_POST_RETRY_STATUSES,
        )
        _raise_for_simple_result(data, "answer callback")

    async def get_subscriptions(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/subscriptions")
        if data.get("success") is False:
            raise MaxOperationError(_string_value(data.get("message")) or "MAX could not list subscriptions")
        subscriptions = data.get("subscriptions", [])
        if not isinstance(subscriptions, list):
            raise MaxApiError("MAX subscriptions response has an unexpected shape", ambiguous=False)
        return [item for item in subscriptions if isinstance(item, dict)]

    async def unsubscribe(self, url: str) -> None:
        data = await self._request("DELETE", "/subscriptions", params={"url": url})
        _raise_for_simple_result(data, "unsubscribe webhook")

    async def subscribe(self, url: str, update_types: Sequence[str], secret: str) -> None:
        data = await self._request(
            "POST",
            "/subscriptions",
            json_body={"url": url, "update_types": list(update_types), "secret": secret},
        )
        _raise_for_simple_result(data, "subscribe")

    async def get_updates(
        self,
        *,
        marker: int | None,
        timeout_sec: int = 30,
        limit: int = 100,
        update_types: Sequence[str] | None = None,
    ) -> UpdateList:
        params: dict[str, Any] = {"marker": marker, "timeout": timeout_sec, "limit": limit}
        if update_types:
            params["types"] = ",".join(update_types)
        data = await self._request(
            "GET",
            "/updates",
            params=params,
            timeout_sec=max(float(timeout_sec) + 5.0, 10.0),
        )
        try:
            return UpdateList.model_validate(data)
        except ValidationError as exc:
            raise MaxApiError("MAX /updates response has an unexpected shape") from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        chat_id: int | None = None,
        retry_statuses: frozenset[int] = _RETRYABLE_STATUSES,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            await self._rate_limiter.acquire()
            if chat_id is not None:
                await self._chat_rate_limiter.acquire(chat_id)
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    timeout=(
                        httpx.USE_CLIENT_DEFAULT
                        if timeout_sec is None
                        else httpx.Timeout(timeout_sec)
                    ),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                if attempt >= self._max_retries:
                    raise MaxApiError("MAX API is unavailable") from exc
                attempt += 1
                await self._sleep_before_retry(attempt, None)
                continue
            except httpx.ReadTimeout as exc:
                if method.upper() not in _RETRYABLE_METHODS or attempt >= self._max_retries:
                    raise MaxApiError("MAX API read timed out", ambiguous=True) from exc
                attempt += 1
                await self._sleep_before_retry(attempt, None)
                continue
            except (httpx.WriteTimeout, httpx.WriteError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                raise MaxApiError("MAX API connection ended with an unknown outcome", ambiguous=True) from exc

            try:
                payload = _decode_json_object(response)
            except MaxApiError:
                if response.status_code in retry_statuses and attempt < self._max_retries:
                    attempt += 1
                    await self._sleep_before_retry(attempt, response)
                    continue
                raise
            if response.is_success:
                return payload
            if response.status_code in retry_statuses and attempt < self._max_retries:
                attempt += 1
                await self._sleep_before_retry(attempt, response)
                continue
            code = _string_value(payload.get("code"))
            detail = _string_value(payload.get("message")) or response.reason_phrase or "request failed"
            raise MaxApiError(
                detail,
                status_code=response.status_code,
                code=code,
                ambiguous=response.status_code >= 500 or response.status_code == 408,
            )

    async def _sleep_before_retry(self, attempt: int, response: httpx.Response | None) -> None:
        delay = min(self._retry_base_sec * (2 ** (attempt - 1)), self._retry_max_sec)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                server_delay: float | None = None
                try:
                    server_delay = max(float(retry_after), 0.0)
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(retry_after)
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=UTC)
                        server_delay = max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)
                    except (TypeError, ValueError, OverflowError):
                        server_delay = None
                if server_delay is not None:
                    delay = min(self._retry_max_sec, max(delay, server_delay))
        if delay > 0:
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))


def _decode_json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MaxApiError(
            "MAX API returned a non-JSON response",
            status_code=response.status_code,
            ambiguous=response.is_success or response.status_code >= 500 or response.status_code == 408,
        ) from exc
    if not isinstance(value, dict):
        raise MaxApiError(
            "MAX API returned an unexpected JSON value",
            status_code=response.status_code,
            ambiguous=response.is_success,
        )
    return value


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _raise_for_simple_result(data: dict[str, Any], operation: str) -> None:
    try:
        result = SimpleResult.model_validate(data)
    except ValidationError as exc:
        # A 2xx response without an explicit success flag does not prove that a
        # mutating request was rejected; it may have been applied before the
        # response was truncated or changed.  Callers must reconcile it rather
        # than replaying the operation automatically.
        raise MaxApiError(f"MAX {operation} response has an unexpected shape", ambiguous=True) from exc
    if not result.success:
        raise MaxOperationError(result.message or f"MAX could not {operation}")
