from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from megapbx_max.max_api.client import MaxApiClient, MaxApiError, MaxOperationError
from megapbx_max.max_api.models import CallbackPayload, Update

MESSAGE_RESPONSE: dict[str, Any] = {
    "message": {
        "sender": {
            "user_id": 1,
            "first_name": "MegaPBX MAX",
            "is_bot": True,
        },
        "recipient": {
            "chat_id": 42,
            "chat_type": "chat",
        },
        "timestamp": 1_700_000_000_000,
        "body": {
            "mid": "mid-1",
            "seq": 7,
            "text": "Пропущенный звонок",
        },
    }
}


def test_callback_update_is_parsed() -> None:
    update = Update.model_validate(
        {
            "update_type": "message_callback",
            "timestamp": 1_700_000_000_000,
            "callback": {
                "timestamp": 1_700_000_000_000,
                "callback_id": "keyboard-1",
                "payload": '{"v":1,"action":"call_back","record_id":"record-1"}',
                "user": {"user_id": 9, "first_name": "Agent", "is_bot": False},
            },
        }
    )

    assert update.callback is not None
    assert update.callback.callback_id == "keyboard-1"
    payload = CallbackPayload.model_validate_json(update.callback.payload)
    assert payload.record_id == "record-1"


@pytest.mark.parametrize("field,value", [("user_id", "9"), ("is_bot", "false")])
def test_callback_user_rejects_coerced_identity_fields(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Update.model_validate({
            "update_type": "message_callback", "timestamp": 1,
            "callback": {
                "timestamp": 1, "callback_id": "cb-1",
                "user": {"user_id": 9, "first_name": "Agent", "is_bot": False, field: value},
            },
        })


def test_injected_http_client_is_not_mutated() -> None:
    import asyncio

    async def exercise() -> None:
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"updates": []})),
            headers={"X-Shared": "yes"},
        ) as http_client:
            client = MaxApiClient("max-secret", http_client=http_client)
            assert "Authorization" not in http_client.headers
            await client.get_updates(marker=None)
            assert "Authorization" not in http_client.headers
            assert http_client.headers["X-Shared"] == "yes"

    asyncio.run(exercise())


def test_callback_without_optional_payload_is_valid() -> None:
    update = Update.model_validate(
        {
            "update_type": "message_callback",
            "timestamp": 1,
            "callback": {
                "timestamp": 1,
                "callback_id": "cb-no-payload",
                "user": {"user_id": 1, "first_name": "User", "is_bot": False},
            },
        }
    )
    assert update.callback is not None
    assert update.callback.payload is None


def test_long_poll_uses_a_timeout_above_server_timeout() -> None:
    seen_timeout: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_timeout.update(request.extensions.get("timeout", {}))
        return httpx.Response(200, json={"updates": [], "marker": 1})

    async def exercise() -> None:
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = MaxApiClient("max-secret", timeout_sec=1, http_client=http_client)
            await client.get_updates(marker=None, timeout_sec=30)

    import asyncio

    asyncio.run(exercise())
    assert seen_timeout["read"] == 35


def test_send_message_uses_max_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=MESSAGE_RESPONSE)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = MaxApiClient("max-secret", http_client=http_client)
            message = await client.send_message(
                42,
                "<b>Пропущенный звонок</b>",
                buttons=[{"type": "callback", "text": "Я наберу", "payload": "v1:callback:call-1"}],
            )
            assert message.body is not None
            assert message.body.mid == "mid-1"

    import asyncio

    asyncio.run(exercise())
    assert len(requests) == 1
    request = requests[0]
    assert request.headers["Authorization"] == "max-secret"
    assert request.url.params["chat_id"] == "42"
    payload = __import__("json").loads(request.content)
    assert payload["attachments"][0]["type"] == "inline_keyboard"
    assert payload["attachments"][0]["payload"]["buttons"][0][0]["type"] == "callback"


def test_send_without_message_mid_is_ambiguous() -> None:
    response = {
        "message": {
            "recipient": {"chat_id": 42, "chat_type": "chat"},
            "timestamp": 1,
            "body": None,
        }
    }

    async def exercise() -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=response))
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=transport,
        ) as http_client:
            client = MaxApiClient("max-secret", http_client=http_client)
            with pytest.raises(MaxApiError) as exc_info:
                await client.send_message(42, "hello")
            assert exc_info.value.ambiguous is True

    import asyncio

    asyncio.run(exercise())


def test_success_false_is_not_treated_as_success() -> None:
    async def exercise() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"success": False, "message": "edit failed"})
        )
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=transport,
        ) as http_client:
            client = MaxApiClient("max-secret", http_client=http_client)
            with pytest.raises(MaxOperationError, match="edit failed"):
                await client.edit_message("mid-1", "closed")

    import asyncio

    asyncio.run(exercise())


def test_malformed_success_response_is_ambiguous_for_callback_answer() -> None:
    responses = [{}, {"success": "false"}, {"success": "true"}, {"success": 1}]
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return httpx.Response(200, json=response)

    async def exercise() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=transport,
        ) as http_client:
            client = MaxApiClient("max-secret", http_client=http_client)
            for _ in responses:
                with pytest.raises(MaxApiError) as exc_info:
                    await client.answer_callback("callback-1", notification="done")
                assert exc_info.value.ambiguous is True

    import asyncio

    asyncio.run(exercise())
    assert calls == len(responses)


def test_transient_status_is_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"code": "unavailable", "message": "try later"})
        return httpx.Response(200, json={"success": True})

    async def exercise() -> None:
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = MaxApiClient(
                "max-secret",
                max_retries=1,
                retry_base_sec=0,
                retry_max_sec=0,
                http_client=http_client,
            )
            await client.edit_message("mid-1", "closed")

    import asyncio

    asyncio.run(exercise())
    assert calls == 2


def test_write_timeout_is_wrapped_as_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteTimeout("write failed", request=request)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = MaxApiClient("max-secret", http_client=http_client)
            with pytest.raises(MaxApiError) as exc_info:
                await client.send_message(42, "hello")
            assert exc_info.value.ambiguous is True

    import asyncio

    asyncio.run(exercise())


def test_transient_non_json_response_is_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="temporary gateway page")
        return httpx.Response(200, json={"success": True})

    async def exercise() -> None:
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = MaxApiClient(
                "max-secret",
                max_retries=1,
                retry_base_sec=0,
                retry_max_sec=0,
                http_client=http_client,
            )
            await client.edit_message("mid-1", "closed")

    import asyncio

    asyncio.run(exercise())
    assert calls == 2


def test_new_message_5xx_is_not_retried_because_delivery_is_ambiguous() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"message": "temporary"})

    async def exercise() -> None:
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = MaxApiClient("max-secret", max_retries=3, http_client=http_client)
            with pytest.raises(MaxApiError) as exc_info:
                await client.send_message(42, "hello")
            assert exc_info.value.ambiguous is True

    import asyncio

    asyncio.run(exercise())
    assert calls == 1


def test_read_timeout_is_not_retried_for_new_message() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("ambiguous result", request=request)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = MaxApiClient(
                "max-secret",
                max_retries=3,
                retry_base_sec=0,
                retry_max_sec=0,
                http_client=http_client,
            )
            with pytest.raises(MaxApiError, match="read timed out") as exc_info:
                await client.send_message(42, "hello")
            assert exc_info.value.ambiguous is True

    import asyncio

    asyncio.run(exercise())
    assert calls == 1


def test_non_json_gateway_timeout_for_new_message_is_ambiguous() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(408, text="request timeout")

    async def exercise() -> None:
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = MaxApiClient(
                "max-secret",
                max_retries=3,
                retry_base_sec=0,
                retry_max_sec=0,
                http_client=http_client,
            )
            with pytest.raises(MaxApiError) as exc_info:
                await client.send_message(42, "hello")
            assert exc_info.value.ambiguous is True

    import asyncio

    asyncio.run(exercise())
    assert calls == 1


def test_gateway_timeout_for_new_message_is_ambiguous() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(408, json={"message": "request timeout"})

    async def exercise() -> None:
        async with httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = MaxApiClient(
                "max-secret",
                max_retries=3,
                retry_base_sec=0,
                retry_max_sec=0,
                http_client=http_client,
            )
            with pytest.raises(MaxApiError) as exc_info:
                await client.send_message(42, "hello")
            assert exc_info.value.ambiguous is True

    import asyncio

    asyncio.run(exercise())
    assert calls == 1
