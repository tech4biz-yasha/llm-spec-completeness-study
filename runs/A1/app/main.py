"""Application factory, middleware and error handling."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app import __version__
from app.api.router import api_router
from app.api.routes import health
from app.config import Settings, get_settings
from app.db import dispose_engine, get_sessionmaker
from app.errors import ConflictError, DomainError
from app.logging_setup import configure_logging, request_id_var
from app.ports.events import EventPublisher, KafkaEventPublisher, NullEventPublisher
from app.ports.notifications import LoggingNotifier, NullNotifier
from app.ports.outbox import OutboxRelay

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


def _build_publisher(settings: Settings) -> EventPublisher:
    if settings.kafka_enabled:
        return KafkaEventPublisher(settings.kafka_bootstrap_servers)
    return NullEventPublisher()


async def _relay_loop(relay: OutboxRelay, interval: float) -> None:
    """Continuously drain the outbox. Survives individual failures."""
    while True:
        try:
            await relay.drain_all()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the relay must outlive transient DB errors
            logger.exception("outbox relay iteration failed")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(debug=settings.debug)

    publisher = _build_publisher(settings)
    notifier = LoggingNotifier() if settings.notifications_enabled else NullNotifier()
    relay = OutboxRelay(
        get_sessionmaker(),
        publisher=publisher,
        notifier=notifier,
        batch_size=settings.outbox_batch_size,
    )
    app.state.outbox_relay = relay

    task: asyncio.Task[None] | None = None
    if settings.outbox_relay_enabled:
        task = asyncio.create_task(_relay_loop(relay, settings.outbox_poll_interval_seconds))
        logger.info("outbox relay started", extra={"kafka_enabled": settings.kafka_enabled})

    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await publisher.close()
        await dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(debug=settings.debug)

    app = FastAPI(
        title="Meridian Exit Workflow",
        version=__version__,
        summary="Tenant exit workflow: initiation through completion, deposit settlement "
        "and NOC issuance (SRS T13, O15, O16, BR-1).",
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
    )

    # --- middleware ---------------------------------------------------------------------

    @app.middleware("http")
    async def correlate_and_time(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        # SRS §5.1 targets p95 < 200 ms; surfacing the figure makes regressions visible.
        response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
        logger.info(
            "request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(elapsed_ms, 2),
            },
        )
        return response

    # --- error handling -----------------------------------------------------------------

    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        if exc.status_code >= 500:
            logger.error("domain error", exc_info=exc)
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload(request_id))

    @app.exception_handler(StaleDataError)
    async def handle_stale_data(request: Request, exc: StaleDataError) -> JSONResponse:
        # Optimistic-lock collision: someone else advanced this workflow concurrently.
        error = ConflictError(
            "the record was modified concurrently; reload and retry",
            details={"reason": "optimistic_lock_conflict"},
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error.to_payload(getattr(request.state, "request_id", None)),
        )

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        logger.warning("integrity constraint violated", exc_info=exc)
        error = ConflictError(
            "the request conflicts with the current state of the resource",
            details={"constraint": getattr(getattr(exc, "orig", None), "constraint_name", None)},
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error.to_payload(getattr(request.state, "request_id", None)),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "request_validation_error",
                    "message": "the request payload failed validation",
                    "details": {"errors": exc.errors()},
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "an unexpected error occurred",
                    "details": {},
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

    # --- routes -------------------------------------------------------------------------

    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
