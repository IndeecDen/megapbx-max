from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol

from ..storage import StorageError
from .client import MaxApiClient, MaxApiError
from .models import Update

logger = logging.getLogger(__name__)


class UpdateDispatcher(Protocol):
    async def handle_update(self, update: Update) -> None: ...


async def poll_once(
    client: MaxApiClient,
    dispatcher: UpdateDispatcher,
    *,
    marker: int | None = None,
    update_types: Sequence[str],
    timeout_sec: int = 30,
) -> int | None:
    """Process one page and return its marker only after every update succeeds."""
    page = await client.get_updates(
        marker=marker,
        timeout_sec=timeout_sec,
        limit=100,
        update_types=update_types,
    )
    for update in page.updates:
        await dispatcher.handle_update(update)
    return page.marker if page.marker is not None else marker


async def run_long_polling(
    client: MaxApiClient,
    dispatcher: UpdateDispatcher,
    *,
    update_types: Sequence[str],
    stop_event: asyncio.Event | None = None,
) -> None:
    """Development-only polling loop. Production must use MAX Webhook."""
    stop = stop_event or asyncio.Event()
    marker: int | None = None
    while not stop.is_set():
        try:
            marker = await poll_once(
                client,
                dispatcher,
                marker=marker,
                update_types=update_types,
                timeout_sec=30,
            )
        except (MaxApiError, StorageError) as exc:
            logger.warning("MAX long polling failed; retrying: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass
        else:
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.3)
            except TimeoutError:
                pass
