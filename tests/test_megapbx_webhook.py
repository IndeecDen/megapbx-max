from __future__ import annotations

import base64
import logging
from dataclasses import replace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from megapbx_max.config import Settings
from megapbx_max.domain import EnqueueResult
from megapbx_max.megapbx_webhook import create_megapbx_router, parse_webhook_body


class FakeEnqueuer:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.inserted_values = iter((True, False))

    def enqueue_megapbx(self, payload: dict[str, Any]) -> EnqueueResult:
        self.payloads.append(payload)
        return EnqueueResult(job_id=len(self.payloads), inserted=next(self.inserted_values, False))


def settings() -> Settings:
    return Settings.from_env(
        {
            "MAX_BOT_TOKEN": "max-secret",
            "MAX_CHAT_ID": "42",
            "MAX_WEBHOOK_SECRET": "webhook_secret-1",
            "MEGAPBX_CRM_TOKEN": "crm-secret",
            "MEGAPBX_ALLOWED_DID": "100",
            "STATE_DB_PATH": "unused.sqlite3",
        }
    )


def client_for(handler: FakeEnqueuer, configured: Settings | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(create_megapbx_router(configured or settings(), handler))
    return TestClient(app)


def missed_payload(call_id: str = "call-1") -> dict[str, Any]:
    return {
        "cmd": "history",
        "status": "Missed",
        "callid": call_id,
        "phone": "+15555550123",
        "telnum": "100",
    }


def test_parser_supports_json_form_and_nested_form() -> None:
    assert parse_webhook_body('{"cmd":"history"}') == {"cmd": "history"}
    assert parse_webhook_body("cmd=history&status=Missed&callid=1")["status"] == "Missed"
    assert parse_webhook_body("payload=%7B%22cmd%22%3A%22history%22%7D") == {"cmd": "history"}


def test_webhook_rejects_invalid_utf8_and_strict_json_content_type() -> None:
    handler = FakeEnqueuer()
    client = client_for(handler)
    headers = {"X-CRM-Token": "crm-secret", "Content-Type": "application/json"}

    assert client.post("/megapbx/webhook", content=b"\xff", headers=headers).status_code == 400
    assert client.post(
        "/megapbx/webhook",
        content=b"cmd=history&status=Missed",
        headers=headers,
    ).status_code == 400
    assert client.post(
        "/megapbx/webhook",
        content=b"null",
        headers=headers,
    ).status_code == 400


def test_webhook_rejects_duplicate_form_fields() -> None:
    handler = FakeEnqueuer()
    client = client_for(handler)
    response = client.post(
        "/megapbx/webhook",
        content=b"cmd=history&cmd=event",
        headers={"X-CRM-Token": "crm-secret", "Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400


def test_webhook_auth_and_duplicate_response() -> None:
    handler = FakeEnqueuer()
    client = client_for(handler)
    payload = missed_payload()

    assert client.post("/megapbx/webhook", json=payload).status_code == 401
    first = client.post(
        "/megapbx/webhook",
        json=payload,
        headers={"X-CRM-Token": "crm-secret"},
    )
    second = client.post(
        "/megapbx/webhook",
        json=payload,
        headers={"X-CRM-Token": "crm-secret"},
    )

    assert first.status_code == 200
    assert first.json() == {"ok": True, "duplicate": False}
    assert second.json() == {"ok": True, "duplicate": True}
    assert len(handler.payloads) == 2


def test_bearer_and_basic_auth_are_supported() -> None:
    handler = FakeEnqueuer()
    client = client_for(handler)
    payload = missed_payload("auth-call")

    bearer = client.post(
        "/megapbx/webhook",
        json=payload,
        headers={"Authorization": "Bearer crm-secret"},
    )
    basic_value = base64.b64encode(b"user:crm-secret").decode()
    basic = client.post(
        "/megapbx/webhook",
        json=payload,
        headers={"Authorization": f"Basic {basic_value}"},
    )

    assert bearer.status_code == 200
    assert basic.status_code == 200


def test_query_token_is_disabled_by_default() -> None:
    handler = FakeEnqueuer()
    client = client_for(handler)

    response = client.post(
        "/megapbx/webhook",
        params={"token": "crm-secret"},
        json=missed_payload(),
    )

    assert response.status_code == 401
    assert handler.payloads == []


def test_webhook_fails_closed_without_server_token() -> None:
    handler = FakeEnqueuer()
    configured = replace(settings(), megapbx_crm_token="")
    response = client_for(handler, configured).post(
        "/megapbx/webhook",
        json=missed_payload(),
        headers={"X-CRM-Token": "anything"},
    )
    assert response.status_code == 503


def test_webhook_rejects_oversized_body() -> None:
    configured = replace(settings(), megapbx_webhook_body_bytes=1024)
    response = client_for(FakeEnqueuer(), configured).post(
        "/megapbx/webhook",
        content=b"x" * 1025,
        headers={
            "Content-Type": "application/json",
            "X-CRM-Token": "crm-secret",
        },
    )
    assert response.status_code == 413


def test_webhook_logs_do_not_contain_payload_pii(caplog) -> None:
    handler = FakeEnqueuer()
    client = client_for(handler)
    caplog.set_level(logging.INFO, logger="megapbx_max.megapbx_webhook")
    secret_phone = "+15555550102"
    secret_name = "Private Client"

    response = client.post(
        "/megapbx/webhook",
        json=missed_payload("private-1") | {"phone": secret_phone, "contact_name": secret_name},
        headers={"X-CRM-Token": "crm-secret"},
    )

    assert response.status_code == 200
    assert secret_phone not in caplog.text
    assert secret_name not in caplog.text
    assert "crm-secret" not in caplog.text
