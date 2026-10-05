from __future__ import annotations

import asyncio
import json
import secrets
from typing import Protocol

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError

from .config import Settings
from .max_api.models import Update
from .storage import StorageError


class MaxUpdateEnqueuer(Protocol):
    def enqueue_update(self, update: Update) -> object: ...


def create_max_webhook_router(settings: Settings, enqueuer: MaxUpdateEnqueuer) -> APIRouter:
    router = APIRouter(tags=["max-webhook"])

    @router.post("/max/webhook", status_code=status.HTTP_200_OK)
    async def receive_max_update(request: Request) -> dict[str, bool]:
        _verify_secret(request, settings.max_webhook_secret)
        payload = await _read_limited_json(request, settings.max_webhook_body_bytes)
        try:
            update = Update.model_validate(payload)
        except (ValidationError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid MAX update") from exc

        try:
            await asyncio.to_thread(enqueuer.enqueue_update, update)
        except StorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="MAX event could not be durably queued",
            ) from exc
        return {"ok": True}

    return router


def _verify_secret(request: Request, expected: str | None) -> None:
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MAX webhook is disabled until MAX_WEBHOOK_SECRET is configured",
        )
    provided = request.headers.get("X-Max-Bot-Api-Secret", "")
    if not secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")


async def _read_limited_json(request: Request, max_bytes: int) -> object:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Payload is too large")
        body.extend(chunk)
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from exc
