from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

from .config import (
    DEFAULT_MAX_API_BASE,
    DEFAULT_MAX_UPDATE_TYPES,
    ConfigurationError,
    Settings,
    validate_max_webhook_url,
)
from .jobs import DurableJobQueue, JobWorker, QueueUpdateDispatcher
from .max_api.client import MaxApiClient, MaxApiError
from .max_api.polling import run_long_polling
from .pbx import PbxDirectory
from .service import MegapbxService
from .storage import SQLiteStore, StorageError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="megapbx-max", description="MegaPBX missed-call bot for MAX")
    parser.add_argument("--log-level", default="INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the FastAPI webhook server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    subparsers.add_parser("check", help="validate the MAX token")

    subscribe = subparsers.add_parser("subscribe", help="register/update the production MAX webhook")
    subscribe.add_argument("--url")
    subscribe.add_argument("--secret-file", help="read the webhook secret from a protected file")

    unsubscribe = subparsers.add_parser("unsubscribe", help="remove a MAX webhook subscription")
    unsubscribe.add_argument("--url")

    subparsers.add_parser("subscriptions", help="list MAX webhook subscriptions")

    subparsers.add_parser("deliveries", help="list deliveries with an unknown external outcome")
    subparsers.add_parser("callback-unknown", help="list callbacks with an unknown external answer")
    resolve_callback = subparsers.add_parser(
        "resolve-callback-unknown", help="dismiss an unknown callback without retrying or closing the notification"
    )
    resolve_callback.add_argument("--callback-id", required=True)
    retry_callback = subparsers.add_parser(
        "retry-callback-unknown", help="explicitly retry an unknown callback"
    )
    retry_callback.add_argument("--callback-id", required=True)
    resolve = subparsers.add_parser("resolve-unknown", help="resolve an unknown delivery as sent")
    resolve.add_argument("--record-id", required=True)
    resolve.add_argument("--message-mid", required=True)
    retry = subparsers.add_parser("retry-unknown", help="allow a new attempt for an unknown delivery")
    retry.add_argument("--record-id", required=True)

    poll = subparsers.add_parser("poll", help="run development-only MAX long polling")
    poll.add_argument("--once", action="store_true", help="fetch one page and exit")

    discover = subparsers.add_parser("discover-chat-id", help="print chat IDs from bot_started/bot_added events")
    discover.add_argument("--timeout", type=int, default=90)

    return parser


async def run_async(args: argparse.Namespace) -> int:
    if args.command == "serve":
        import uvicorn

        settings = Settings.from_env()
        if settings.max_webhook_secret is None:
            raise ConfigurationError("MAX_WEBHOOK_SECRET is required for server mode")
        config = uvicorn.Config(
            "megapbx_max.main:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            log_level=args.log_level.casefold(),
            access_log=False,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
        )
        await uvicorn.Server(config).serve()
        return 0

    if args.command in {"check", "subscribe", "unsubscribe", "subscriptions"}:
        async with _max_client_from_env() as client:
            if args.command == "check":
                bot = await client.get_me()
                print(
                    json.dumps(
                        {
                            "user_id": bot.user_id,
                            "name": bot.name or bot.first_name,
                            "username": bot.username,
                            "is_bot": bot.is_bot,
                        },
                        ensure_ascii=False,
                    )
                )
            elif args.command == "subscribe":
                url = _validate_webhook_url(args.url or _env("MAX_WEBHOOK_URL"))
                secret_value = _read_secret_file(args.secret_file) if args.secret_file else _env("MAX_WEBHOOK_SECRET")
                secret = _validate_webhook_secret(secret_value)
                await client.subscribe(url, _update_types_from_env(), secret)
                print("MAX webhook subscription configured")
            elif args.command == "unsubscribe":
                url = _validate_webhook_url(args.url or _env("MAX_WEBHOOK_URL"))
                await client.unsubscribe(url)
                print("MAX webhook subscription removed")
            else:
                subscriptions = await client.get_subscriptions()
                safe = [
                    {
                        "url": item.get("url"),
                        "update_types": item.get("update_types"),
                    }
                    for item in subscriptions
                ]
                print(json.dumps(safe, ensure_ascii=False, indent=2))
        return 0

    if args.command == "discover-chat-id":
        async with _max_client_from_env() as client:
            return await _discover(client, timeout_sec=args.timeout)

    if args.command in {
        "deliveries", "callback-unknown", "resolve-callback-unknown", "retry-callback-unknown",
        "resolve-unknown", "retry-unknown",
    }:
        settings = Settings.from_env()
        storage = SQLiteStore(settings.state_db_path)
        storage.initialize()
        if args.command == "callback-unknown":
            print(json.dumps(storage.list_external_unknown_callbacks(), ensure_ascii=False, indent=2))
        elif args.command == "resolve-callback-unknown":
            storage.resolve_external_unknown_callback(args.callback_id)
            print("Unknown callback dismissed; notification state was not changed")
        elif args.command == "retry-callback-unknown":
            storage.retry_external_unknown_callback(args.callback_id)
            print("Unknown callback released for an explicit retry")
        elif args.command == "deliveries":
            values = [
                {
                    "record_id": item.record_id,
                    "call_id": item.call_id,
                    "state": item.state,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
                for item in storage.list_unknown_deliveries()
            ]
            print(json.dumps(values, ensure_ascii=False, indent=2))
        elif args.command == "resolve-unknown":
            record = storage.resolve_unknown_as_sent(args.record_id, args.message_mid)
            print(json.dumps({"record_id": record.id, "message_mid": record.message_mid}, ensure_ascii=False))
        else:
            storage.retry_unknown(args.record_id)
            print("Unknown delivery released for an explicit retry")
        return 0

    settings = Settings.from_env()
    storage = SQLiteStore(settings.state_db_path)
    storage.initialize()
    async with _max_client(settings) as client:
        if args.command == "poll":
            try:
                if await client.get_subscriptions():
                    print("MAX webhook subscription is active; unsubscribe before long polling", file=sys.stderr)
                    return 2
            except MaxApiError:
                pass
        directory = PbxDirectory(
            settings.megapbx_api_base,
            settings.megapbx_api_token,
            verify=_ca_bundle(),
        )
        service = MegapbxService(settings, storage, client, directory)
        queue = DurableJobQueue(settings, storage)
        worker = JobWorker(
            queue,
            service,
            max_attempts=settings.job_max_attempts,
            retry_base_sec=settings.job_retry_base_sec,
            retry_max_sec=settings.job_retry_max_sec,
            job_lease_sec=settings.job_lease_sec,
        )
        dispatcher = QueueUpdateDispatcher(queue)
        try:
            if args.once:
                page = await client.get_updates(
                    marker=None,
                    timeout_sec=30,
                    update_types=("message_callback", "bot_started", "bot_added"),
                )
                for update in page.updates:
                    await dispatcher.handle_update(update)
                while await worker.run_once():
                    pass
                return 0
            stop = asyncio.Event()
            worker_task = asyncio.create_task(worker.run_forever(stop))
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except NotImplementedError:
                    pass
            try:
                await run_long_polling(
                    client,
                    dispatcher,
                    update_types=settings.max_webhook_update_types,
                    stop_event=stop,
                )
            finally:
                stop.set()
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
            return 0
        finally:
            await service.aclose()
            await directory.aclose()


async def _discover(client: MaxApiClient, *, timeout_sec: int) -> int:
    seen: set[int] = set()
    marker: int | None = None
    try:
        async with asyncio.timeout(max(timeout_sec, 1)):
            while True:
                remaining = max(timeout_sec, 1)
                page = await client.get_updates(
                    marker=marker,
                    timeout_sec=min(remaining, 90),
                    update_types=("bot_started", "bot_added"),
                )
                for update in page.updates:
                    if update.chat_id is not None and update.chat_id not in seen:
                        seen.add(update.chat_id)
                        print(update.chat_id)
                if page.marker is not None:
                    marker = page.marker
                if seen:
                    return 0
    except TimeoutError:
        return 1
    return 1


def _max_client_from_env() -> MaxApiClient:
    token = _env("MAX_BOT_TOKEN")
    if not token:
        raise ConfigurationError("MAX_BOT_TOKEN is required")
    if "\r" in token or "\n" in token:
        raise ConfigurationError("MAX_BOT_TOKEN must not contain line breaks")
    base_url = _env("MAX_API_BASE") or DEFAULT_MAX_API_BASE
    parsed = urlparse(base_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("MAX_API_BASE contains an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (port is not None and port < 1)
    ):
        raise ConfigurationError(
            "MAX_API_BASE must be an absolute HTTPS URL without credentials, query or fragment"
        )
    return MaxApiClient(
        token,
        base_url=base_url,
        max_retries=_env_int("MAX_API_MAX_RETRIES", 2, minimum=0, maximum=10),
        retry_base_sec=_env_float("MAX_API_RETRY_BASE_SEC", 0.5, minimum=0.0),
        retry_max_sec=_env_float("MAX_API_RETRY_MAX_SEC", 8.0, minimum=0.0),
        timeout_sec=_env_float("MAX_API_TIMEOUT_SEC", 8.0, minimum=1.0),
        verify=_ca_bundle(),
    )


def _max_client(settings: Settings) -> MaxApiClient:
    return MaxApiClient(
        settings.max_bot_token,
        base_url=settings.max_api_base,
        max_retries=min(settings.max_api_max_retries, 10),
        retry_base_sec=settings.max_api_retry_base_sec,
        retry_max_sec=settings.max_api_retry_max_sec,
        timeout_sec=settings.max_api_timeout_sec,
        verify=_ca_bundle(),
    )


def _update_types_from_env() -> tuple[str, ...]:
    raw = _env("MAX_WEBHOOK_UPDATE_TYPES")
    if not raw:
        return DEFAULT_MAX_UPDATE_TYPES
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if "message_callback" not in values:
        raise ConfigurationError("MAX_WEBHOOK_UPDATE_TYPES must include message_callback")
    if len(set(values)) != len(values):
        raise ConfigurationError("MAX_WEBHOOK_UPDATE_TYPES contains duplicates")
    return values


def _validate_webhook_url(value: str | None) -> str:
    if not value:
        raise ConfigurationError("A MAX webhook URL is required")
    return validate_max_webhook_url(value)


def _validate_webhook_secret(value: str | None) -> str:
    if not value or not 5 <= len(value) <= 256:
        raise ConfigurationError("A MAX webhook secret of 5 to 256 characters is required")
    if any(not (char.isascii() and (char.isalnum() or char in {"_", "-"})) for char in value):
        raise ConfigurationError("MAX webhook secret contains unsupported characters")
    return value


def _ca_bundle() -> str | bool:
    configured = _env("SSL_CERT_FILE")
    if configured:
        return configured
    system_bundle = "/etc/ssl/certs/ca-certificates.crt"
    return system_bundle if Path(system_bundle).is_file() else True


def _read_secret_file(path: str) -> str | None:
    secret_path = Path(path)
    try:
        mode = stat.S_IMODE(secret_path.stat().st_mode)
        if mode & 0o077:
            raise ConfigurationError("Webhook secret file must not be accessible by group/others")
        value = secret_path.read_text(encoding="utf-8").strip()
    except ConfigurationError:
        raise
    except OSError as exc:
        raise ConfigurationError(f"Cannot read webhook secret file: {path}") from exc
    return value or None


def _env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _env_int(name: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigurationError(f"{name} is out of range")
    return value


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < minimum:
        raise ConfigurationError(f"{name} is out of range")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(run_async(args))
    except KeyboardInterrupt:
        return 130
    except (ConfigurationError, MaxApiError, StorageError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 2  # pragma: no cover
