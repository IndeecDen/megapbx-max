from __future__ import annotations

import asyncio
import json
import logging
import secrets
from base64 import b64decode
from typing import Any, Protocol
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, status

from .config import Settings
from .pbx import fingerprint, is_allowed_destination, is_missed_call, safe_log_value
from .storage import StorageError

logger = logging.getLogger(__name__)


class PayloadEnqueuer(Protocol):
    def enqueue_megapbx(self, payload: dict[str, Any]) -> object: ...


def parse_webhook_body(body_text: str, *, allow_form_fallback: bool = True) -> dict[str, Any]:
    try:
        parsed = json.loads(body_text)
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        if not allow_form_fallback:
            raise ValueError("invalid JSON payload") from exc
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    if parsed is not None or not allow_form_fallback:
        raise ValueError("JSON payload must be an object")

    parsed_form = parse_qs(
        body_text,
        keep_blank_values=True,
        max_num_fields=100,
        strict_parsing=True,
    )
    if not parsed_form:
        raise ValueError("empty webhook payload")
    if any(len(values) != 1 for values in parsed_form.values()):
        raise ValueError("duplicate webhook fields are not supported")
    if "payload" in parsed_form:
        nested = parsed_form["payload"][0]
        try:
            nested_payload = json.loads(nested)
        except (json.JSONDecodeError, TypeError, RecursionError) as exc:
            raise ValueError("invalid nested payload") from exc
        if not isinstance(nested_payload, dict):
            raise ValueError("nested payload must be an object")
        return nested_payload
    result = {
        key: values[0]
        for key, values in parsed_form.items()
        if values and values[0] != ""
    }
    if not result:
        raise ValueError("empty webhook payload")
    return result


def create_megapbx_router(settings: Settings, enqueuer: PayloadEnqueuer) -> APIRouter:
    router = APIRouter(tags=["megapbx-webhook"])

    @router.post("/megapbx/webhook", status_code=status.HTTP_200_OK)
    async def receive_megapbx_event(request: Request, token: str | None = None) -> dict[str, Any]:
        _authenticate(request, settings, token)
        raw_body = await _read_limited_body(request, settings.megapbx_webhook_body_bytes)
        try:
            body_text = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid UTF-8 payload") from exc
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
        try:
            payload = parse_webhook_body(
                body_text,
                allow_form_fallback=content_type != "application/json",
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook payload") from exc

        logger.info(
            "MegaPBX webhook received: cmd=%s status=%s callid=%s bytes=%d",
            safe_log_value(payload.get("cmd")),
            safe_log_value(payload.get("status")),
            fingerprint(payload.get("callid")),
            len(raw_body),
        )
        try:
            enqueue_result = await asyncio.to_thread(enqueuer.enqueue_megapbx, payload)
        except StorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Webhook event could not be durably queued",
            ) from exc
        if is_missed_call(payload) and is_allowed_destination(payload, settings):
            is_new = bool(getattr(enqueue_result, "inserted", False)) or bool(
                getattr(enqueue_result, "reopened", False)
            )
            return {"ok": True, "duplicate": not is_new}
        return {"ok": True}

    return router


def _authenticate(request: Request, settings: Settings, query_token: str | None) -> str:
    supplied = request.headers.get("X-CRM-Token", "")
    if not supplied and settings.megapbx_allow_query_token:
        supplied = query_token or ""
    if not supplied:
        authorization = request.headers.get("Authorization", "")
        scheme, _, credentials = authorization.partition(" ")
        if scheme.casefold() == "bearer":
            supplied = credentials.strip()
        elif scheme.casefold() == "basic":
            try:
                userpass = b64decode(credentials.strip(), validate=True).decode("utf-8", "ignore")
                supplied = userpass.split(":", 1)[-1]
            except (ValueError, UnicodeError):
                supplied = ""
    if query_token and not settings.megapbx_allow_query_token:
        logger.warning("Ignoring query CRM token; use the X-CRM-Token header")
    if not settings.megapbx_crm_token:
        logger.error("MegaPBX webhook rejected: CRM token is not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook authentication is not configured",
        )
    if not supplied or not secrets.compare_digest(
        supplied.encode("utf-8"),
        settings.megapbx_crm_token.encode("utf-8"),
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad CRM token")
    return supplied


async def _read_limited_body(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("Content-Length")
    try:
        declared = int(content_length) if content_length else 0
    except ValueError:
        declared = 0
    if declared > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Webhook payload is too large",
        )
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Webhook payload is too large",
            )
        body.extend(chunk)
    return bytes(body)
