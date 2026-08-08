"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app import __version__
from app.api.errors import register_exception_handlers
from app.api.middleware import AccessLogMiddleware, RequestContextMiddleware
from app.api.v1.router import api_router
from app.container import get_ports
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, get_engine
from app.workers.outbox_dispatcher import OutboxDispatcher
from app.workers.reconciler import Reconciler

log = get_logger("app")

DESCRIPTION = """
Tenant **exit workflow** backend: initiation through completion, including third-party
inspection, deposit settlement and digital NOC issuance.

Implements SRS **T13** (10-step tenant exit flow), **O15** (3rd-party inspection
workflow), **O16** (return deposit & settlement, exit NOC) and **BR-1** (exit workflow
lock on new contracts).

### Flow

1. `POST /exit-workflows` - initiate (draft)
2. `PATCH /exit-workflows/{id}` - move-out date and reason
3. `POST /exit-workflows/{id}/documents` - supporting documents
4. `POST /exit-workflows/{id}/submit` - Workflow ID allocated, owner notified
5. `POST /exit-workflows/{id}/approve` - owner approves; agency engaged by email
6. `POST /exit-workflows/{id}/inspection/slots` - agency offers dates
7. `POST /exit-workflows/{id}/inspection/schedule` - a date is chosen
8. `POST /exit-workflows/{id}/inspection/report` - damage report; review opens
9. `POST /exit-workflows/{id}/settlement/finalise` then `.../settlement/pay`
10. `GET /exit-workflows/{id}/noc/content` - download the Exit NOC
11. `POST /exit-workflows/{id}/complete` - releases the BR-1 lock

### Errors

Every error returns `{"error": {"code", "message", "details", "request_id"}}`. Business
rules from SRS §4.7 surface as HTTP 409 with `code = "business_rule_violation"` and a
`details.rule` naming the rule.
"""

TAGS_METADATA: list[dict[str, Any]] = [
    {"name": "exit-workflows", "description": "Initiation, submission, approval, completion."},
    {"name": "documents", "description": "Supporting documents and damage photos."},
    {"name": "inspection", "description": "Third-party inspection and damage review."},
    {"name": "settlement", "description": "Deposit settlement: deposit minus damage."},
    {"name": "noc", "description": "Digital Exit NOC issuance, download and verification."},
    {"name": "business-rules", "description": "BR-1 contract eligibility."},
    {"name": "webhooks", "description": "Payment provider callbacks."},
    {"name": "operations", "description": "Health and readiness."},
]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        debug=settings.debug, json_output=settings.environment not in ("local", "test")
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ports = get_ports()
        tasks: list[asyncio.Task[None]] = []
        dispatcher: OutboxDispatcher | None = None
        reconciler: Reconciler | None = None

        if settings.enable_background_workers and settings.environment != "test":
            dispatcher = OutboxDispatcher(ports.events, settings=settings, clock=ports.clock)
            reconciler = Reconciler(settings=settings, ports=ports, clock=ports.clock)
            tasks = [
                asyncio.create_task(dispatcher.run_forever(), name="outbox-dispatcher"),
                asyncio.create_task(reconciler.run_forever(), name="reconciler"),
            ]
            log.info("app.workers_started")

        log.info("app.started", environment=settings.environment, version=__version__)
        try:
            yield
        finally:
            if dispatcher is not None:
                dispatcher.stop()
            if reconciler is not None:
                reconciler.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: B014, BLE001
                    pass
            await dispose_engine()
            log.info("app.stopped")

    app = FastAPI(
        title="Meridian Exit Workflow API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url="/redoc" if settings.environment != "production" else None,
        openapi_url="/openapi.json",
        root_path=settings.root_path,
        lifespan=lifespan,
    )

    # Order matters: the request-id middleware must wrap the access log so every line
    # carries a correlation id.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID", "X-Checksum-SHA256", "Idempotent-Replay"],
        )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/health", tags=["operations"], summary="Liveness probe")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/ready", tags=["operations"], summary="Readiness probe")
    async def ready() -> dict[str, str]:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
        return {"status": "ready", "version": __version__}

    return app


app = create_app()
