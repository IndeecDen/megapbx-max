from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, status

from .config import Settings
from .jobs import DurableJobQueue, JobWorker
from .max_api.client import MaxApiClient
from .megapbx_webhook import create_megapbx_router
from .pbx import PbxDirectory
from .service import MegapbxService
from .storage import SQLiteStore
from .webhooks import create_max_webhook_router

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    settings: Settings
    storage: SQLiteStore
    messenger: MaxApiClient
    directory: PbxDirectory
    service: MegapbxService
    queue: DurableJobQueue
    worker: JobWorker
    ready: bool = False
    worker_task: asyncio.Task[None] | None = None


def create_app(
    settings: Settings | None = None,
    *,
    storage: SQLiteStore | None = None,
    messenger: MaxApiClient | None = None,
    directory: PbxDirectory | None = None,
    service: MegapbxService | None = None,
) -> FastAPI:
    configured = settings or Settings.from_env()
    configured_storage = storage or SQLiteStore(configured.state_db_path)
    configured_messenger = messenger or MaxApiClient(
        configured.max_bot_token,
        base_url=configured.max_api_base,
        max_retries=min(configured.max_api_max_retries, 10),
        retry_base_sec=configured.max_api_retry_base_sec,
        retry_max_sec=configured.max_api_retry_max_sec,
        timeout_sec=configured.max_api_timeout_sec,
        verify=_ca_bundle(),
    )
    configured_directory = directory or PbxDirectory(
        configured.megapbx_api_base,
        configured.megapbx_api_token,
        verify=_ca_bundle(),
    )
    configured_service = service or MegapbxService(
        configured,
        configured_storage,
        configured_messenger,
        configured_directory,
    )
    configured_queue = DurableJobQueue(configured, configured_storage)
    configured_worker = JobWorker(
        configured_queue,
        configured_service,
        max_attempts=configured.job_max_attempts,
        retry_base_sec=configured.job_retry_base_sec,
        retry_max_sec=configured.job_retry_max_sec,
        job_lease_sec=configured.job_lease_sec,
    )
    runtime = Runtime(
        settings=configured,
        storage=configured_storage,
        messenger=configured_messenger,
        directory=configured_directory,
        service=configured_service,
        queue=configured_queue,
        worker=configured_worker,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        configured_storage.initialize()
        cleanup_result = configured_storage.cleanup(
            max_age_sec=configured.missed_max_age_sec,
            dedup_ttl_sec=configured.missed_dedup_ttl_sec,
        )
        if cleanup_result.notifications or cleanup_result.deduplication:
            logger.info(
                "Persistent state cleanup: notifications=%d dedup=%d callbacks=%d jobs=%d unknown=%d",
                cleanup_result.notifications,
                cleanup_result.deduplication,
                cleanup_result.callbacks,
                cleanup_result.jobs,
                cleanup_result.unknown_deliveries,
            )
        worker_task = asyncio.create_task(configured_worker.run_forever())
        runtime.worker_task = worker_task
        tasks: list[asyncio.Task[None]] = [
            worker_task,
            asyncio.create_task(
                _cleanup_loop(
                    configured_storage,
                    configured.missed_max_age_sec,
                    configured.missed_dedup_ttl_sec,
                    configured.missed_cleanup_interval_sec,
                )
            ),
        ]
        if configured_directory.enabled:
            tasks.append(
                asyncio.create_task(
                    configured_directory.run_forever(configured.megapbx_enrichment_refresh_sec)
                )
            )
        runtime.ready = True
        try:
            yield
        finally:
            runtime.ready = False
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning("Background task stopped with error: %s", type(exc).__name__)
            await configured_service.aclose()
            await configured_directory.aclose()
            await configured_messenger.aclose()

    app = FastAPI(
        title="MegaPBX → MAX (missed calls)",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.include_router(create_megapbx_router(configured, configured_queue))
    app.include_router(create_max_webhook_router(configured, configured_queue))

    @app.get("/")
    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "max-webhook"}

    @app.get("/readyz")
    async def readiness() -> dict[str, str]:
        if not runtime.ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Application is starting",
            )
        if runtime.worker_task is None or runtime.worker_task.done():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Background worker is not running",
            )
        try:
            runtime.storage.ping()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Persistent storage is unavailable",
            ) from exc
        return {"status": "ready"}

    return app


def _ca_bundle() -> str | bool:
    configured = os.getenv("SSL_CERT_FILE", "").strip()
    if configured:
        return configured
    system_bundle = "/etc/ssl/certs/ca-certificates.crt"
    return system_bundle if os.path.isfile(system_bundle) else True


async def _cleanup_loop(
    storage: SQLiteStore,
    max_age_sec: int,
    dedup_ttl_sec: int,
    interval_sec: int,
) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        try:
            result = storage.cleanup(max_age_sec=max_age_sec, dedup_ttl_sec=dedup_ttl_sec)
            if result.notifications or result.deduplication or result.callbacks:
                logger.info(
                    "Persistent state cleanup: notifications=%d dedup=%d callbacks=%d jobs=%d unknown=%d",
                    result.notifications,
                    result.deduplication,
                    result.callbacks,
                    result.jobs,
                    result.unknown_deliveries,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Persistent state cleanup failed: %s", type(exc).__name__)
