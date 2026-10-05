from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from megapbx_max.config import Settings
from megapbx_max.main import create_app
from megapbx_max.max_api.models import Message, MessageBody, Recipient
from megapbx_max.pbx import PbxDirectory
from megapbx_max.service import MegapbxService
from megapbx_max.storage import SQLiteStore


class Messenger:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Message:
        self.sent.append(text)
        return Message(
            recipient=Recipient(chat_id=chat_id, chat_type="chat"),
            timestamp=1,
            body=MessageBody(mid="mid-main", seq=1, text=text),
        )

    async def edit_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def answer_callback(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None


def settings(tmp_path: Path) -> Settings:
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


def test_health_and_readiness(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    app = create_app(configured)

    with TestClient(app) as client:
        assert client.get("/").json() == {"status": "ok", "mode": "max-webhook"}
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").json() == {"status": "ready"}

    assert Path(configured.state_db_path).exists()


@pytest.mark.asyncio
async def test_lifespan_worker_processes_durable_job(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    storage = SQLiteStore(configured.state_db_path)
    messenger = Messenger()
    directory = PbxDirectory(None, None)
    service = MegapbxService(configured, storage, messenger, directory)
    app = create_app(
        configured,
        storage=storage,
        messenger=messenger,  # type: ignore[arg-type]
        directory=directory,
        service=service,
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/megapbx/webhook",
                json={
                    "cmd": "history",
                    "status": "Missed",
                    "callid": "lifespan-job",
                    "telnum": "100",
                    "phone": "+15555550123",
                },
                headers={"X-CRM-Token": "crm-secret"},
            )
            assert response.status_code == 200
            for _ in range(50):
                if messenger.sent:
                    break
                await asyncio.sleep(0.01)
    assert len(messenger.sent) == 1


def test_megapbx_webhook_is_acknowledged_after_durable_enqueue(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    storage = SQLiteStore(configured.state_db_path)
    messenger = Messenger()
    directory = PbxDirectory(None, None)
    service = MegapbxService(configured, storage, messenger, directory)
    app = create_app(
        configured,
        storage=storage,
        messenger=messenger,  # type: ignore[arg-type]
        directory=directory,
        service=service,
    )

    with TestClient(app) as client:
        response = client.post(
            "/megapbx/webhook",
            json={
                "cmd": "history",
                "status": "Missed",
                "callid": "main-job",
                "telnum": "100",
                "phone": "+15555550123",
            },
            headers={"X-CRM-Token": "crm-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True, "duplicate": False}
        assert asyncio.run(app.state.runtime.worker.run_once()) is True

    assert len(messenger.sent) == 1
