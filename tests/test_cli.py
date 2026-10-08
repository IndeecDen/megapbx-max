from __future__ import annotations

import logging
from typing import Any

import pytest

from megapbx_max.cli import build_parser, run_async
from megapbx_max.config import ConfigurationError
from megapbx_max.max_api.models import BotInfo


def test_cli_suppresses_http_urls_even_at_debug(monkeypatch, caplog) -> None:
    from megapbx_max.cli import main

    async def fake_run(_args):
        logging.getLogger("httpx").info("HTTP Request callback_id=sensitive-callback")
        logging.getLogger("httpcore.http11").debug("request headers Authorization=sensitive-token")
        logging.getLogger("megapbx_max.service").info("Callback completed")
        return 0

    monkeypatch.setattr("megapbx_max.cli.run_async", fake_run)
    # Restore logger levels after the CLI's process-wide configuration.
    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)
    with caplog.at_level(logging.DEBUG):
        assert main(["--log-level", "DEBUG", "check"]) == 0
    assert "sensitive-callback" not in caplog.text
    assert "sensitive-token" not in caplog.text
    assert "Callback completed" in caplog.text


class FakeMaxClient:
    def __init__(self) -> None:
        self.subscribed: tuple[str, tuple[str, ...], str] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_me(self) -> BotInfo:
        return BotInfo(user_id=1, first_name="MAX Bot", name="MAX Bot", username="max_bot")

    async def subscribe(self, url: str, update_types: tuple[str, ...], secret: str) -> None:
        self.subscribed = (url, update_types, secret)

    async def unsubscribe(self, _url: str) -> None:
        return None

    async def get_subscriptions(self) -> list[dict[str, Any]]:
        return []


@pytest.mark.asyncio
async def test_subscribe_command_uses_environment_only(monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "max-secret")
    monkeypatch.setenv("MAX_WEBHOOK_URL", "https://bot.example.com/max/webhook")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "webhook_secret-1")
    fake = FakeMaxClient()
    monkeypatch.setattr("megapbx_max.cli._max_client_from_env", lambda: fake)
    args = build_parser().parse_args(["subscribe"])

    result = await run_async(args)

    assert result == 0
    assert fake.subscribed is not None
    assert fake.subscribed[0] == "https://bot.example.com/max/webhook"
    assert "message_callback" in fake.subscribed[1]
    assert "subscription configured" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_serve_awaits_uvicorn_server_in_current_loop(monkeypatch) -> None:
    import uvicorn

    started = False

    class FakeServer:
        async def serve(self) -> None:
            nonlocal started
            started = True

    monkeypatch.setenv("MAX_BOT_TOKEN", "max-secret")
    monkeypatch.setenv("MAX_CHAT_ID", "42")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "webhook_secret-1")
    monkeypatch.setenv("MEGAPBX_CRM_TOKEN", "crm-secret")
    monkeypatch.setenv("MEGAPBX_ALLOWED_DID", "100")
    monkeypatch.setattr(uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", lambda _config: FakeServer())
    args = build_parser().parse_args(["serve", "--host", "127.0.0.1", "--port", "8000"])

    assert await run_async(args) == 0
    assert started is True


def test_subscribe_rejects_http(monkeypatch) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "max-secret")
    monkeypatch.setenv("MAX_WEBHOOK_URL", "http://bot.example.com/max/webhook")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "webhook_secret-1")
    args = build_parser().parse_args(["subscribe"])
    fake = FakeMaxClient()
    monkeypatch.setattr("megapbx_max.cli._max_client_from_env", lambda: fake)

    with pytest.raises(ConfigurationError, match="HTTPS"):
        import asyncio

        asyncio.run(run_async(args))
