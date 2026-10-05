from __future__ import annotations

from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from megapbx_max.config import Settings
from megapbx_max.domain import EnqueueResult
from megapbx_max.max_api.models import Update
from megapbx_max.webhooks import create_max_webhook_router

CALLBACK_UPDATE = {
    "update_type": "message_callback",
    "timestamp": 1_700_000_000_000,
    "callback": {
        "timestamp": 1_700_000_000_000,
        "callback_id": "keyboard-1",
        "payload": '{"v":1,"action":"call_back","record_id":"record-1"}',
        "user": {"user_id": 9, "first_name": "Agent", "is_bot": False},
    },
}


class Enqueuer:
    def __init__(self) -> None:
        self.updates: list[Update] = []

    def enqueue_update(self, update: Update) -> EnqueueResult:
        self.updates.append(update)
        return EnqueueResult(job_id=len(self.updates), inserted=True)


def settings() -> Settings:
    return Settings.from_env(
        {
            "MAX_BOT_TOKEN": "max-secret",
            "MAX_CHAT_ID": "42",
            "MAX_WEBHOOK_SECRET": "webhook_secret-1",
            "MEGAPBX_CRM_TOKEN": "crm-secret",
            "MEGAPBX_ALLOWED_DID": "78005553535",
        }
    )


def app_with_handler(handler: Enqueuer) -> FastAPI:
    app = FastAPI()
    app.include_router(create_max_webhook_router(settings(), handler))
    return app


def test_max_webhook_checks_secret_and_durably_queues_update() -> None:
    handler = Enqueuer()
    client = TestClient(app_with_handler(handler))

    response = client.post("/max/webhook", json=CALLBACK_UPDATE)
    assert response.status_code == 401

    response = client.post(
        "/max/webhook",
        json=CALLBACK_UPDATE,
        headers={"X-Max-Bot-Api-Secret": "webhook_secret-1"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(handler.updates) == 1
    assert handler.updates[0].callback is not None


def test_max_webhook_rejects_invalid_update() -> None:
    client = TestClient(app_with_handler(Enqueuer()))

    response = client.post(
        "/max/webhook",
        json={"update_type": "message_callback", "timestamp": 1},
        headers={"X-Max-Bot-Api-Secret": "webhook_secret-1"},
    )
    assert response.status_code == 400


def test_max_webhook_fails_closed_when_secret_is_empty() -> None:
    configured = replace(settings(), max_webhook_secret="")
    app = FastAPI()
    app.include_router(create_max_webhook_router(configured, Enqueuer()))
    client = TestClient(app)

    response = client.post("/max/webhook", json=CALLBACK_UPDATE)

    assert response.status_code == 503


def test_max_webhook_limits_body() -> None:
    configured = replace(settings(), max_webhook_body_bytes=1024)
    app = FastAPI()
    app.include_router(create_max_webhook_router(configured, Enqueuer()))
    client = TestClient(app)

    response = client.post(
        "/max/webhook",
        content=b"x" * 1025,
        headers={
            "Content-Type": "application/json",
            "X-Max-Bot-Api-Secret": "webhook_secret-1",
        },
    )
    assert response.status_code == 413
