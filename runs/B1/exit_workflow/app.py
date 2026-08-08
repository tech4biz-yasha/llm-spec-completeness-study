"""Application factory.

Everything the module talks to — database, broker, payment gateway, NOC bucket,
clock, principal resolver — is injected here and hangs off ``app.state``. That is
what lets the acceptance tests exercise the real services against a real
PostgreSQL while substituting the broker and the gateway.

Defaults fail closed. With nothing configured, the module refuses requests
rather than inventing an identity, dropping notifications, or reporting a refund
that no gateway ever saw.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from exit_workflow.api.errors import register_error_handlers
from exit_workflow.api.routes import router
from exit_workflow.api.security import PrincipalResolver, UnconfiguredPrincipalResolver
from exit_workflow.config import Settings, get_settings
from exit_workflow.db.base import dispose_engine, init_engine
from exit_workflow.db.base import session_factory as default_session_factory
from exit_workflow.domain.clock import Clock, DEFAULT_CLOCK
from exit_workflow.domain.reasons import ConfiguredExitReasons, ExitReasonReference
from exit_workflow.events.dispatcher import OutboxDispatcher
from exit_workflow.events.publisher import (
    EventPublisher,
    InMemoryEventPublisher,
    KafkaEventPublisher,
)
from exit_workflow.gateway.payments import PaymentGateway, UnconfiguredPaymentGateway
from exit_workflow.storage.noc import NocStorage, build_storage

logger = logging.getLogger(__name__)


def build_publisher(settings: Settings) -> EventPublisher:
    """Choose the event transport.

    In production the only acceptable answer is a real broker: an in-memory
    publisher would make every owner notification look delivered
    (rules.yaml#EXIT-04).
    """
    if settings.kafka_enabled:
        return KafkaEventPublisher(settings.kafka_bootstrap_servers)
    if settings.is_production:
        raise RuntimeError(
            "EXIT_KAFKA_ENABLED=false is not permitted in production; owner notification "
            "delivery is required by rules.yaml#EXIT-04"
        )
    logger.warning("Kafka disabled; events are recorded in memory and not delivered")
    return InMemoryEventPublisher()


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    principal_resolver: PrincipalResolver | None = None,
    publisher: EventPublisher | None = None,
    payment_gateway: PaymentGateway | None = None,
    noc_storage: NocStorage | None = None,
    exit_reasons: ExitReasonReference | None = None,
    clock: Clock = DEFAULT_CLOCK,
) -> FastAPI:
    settings = settings or get_settings()
    owns_engine = session_factory is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owns_engine:
            init_engine(settings)
            app.state.session_factory = default_session_factory()
        if isinstance(app.state.publisher, KafkaEventPublisher):
            await app.state.publisher.start()
        try:
            yield
        finally:
            if isinstance(app.state.publisher, KafkaEventPublisher):
                await app.state.publisher.stop()
            if owns_engine:
                await dispose_engine()

    app = FastAPI(
        title="Meridian exit workflow",
        version="1.0.0",
        description=(
            "Tenant exit workflow: initiation through deposit settlement and NOC issuance. "
            "Implements rules.yaml#EXIT-01..EXIT-10 and algorithm.md steps 1-13."
        ),
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.clock = clock
    app.state.principal_resolver = principal_resolver or UnconfiguredPrincipalResolver()
    app.state.publisher = publisher or build_publisher(settings)
    app.state.payment_gateway = payment_gateway or UnconfiguredPaymentGateway()
    app.state.noc_storage = noc_storage or build_storage(settings)
    # blockers.md#B-1 — empty unless the reference data dictionary has been
    # published and configured. Never defaulted to an invented list.
    app.state.exit_reasons = exit_reasons or ConfiguredExitReasons(settings.exit_reason_codes)

    if session_factory is not None:
        app.state.session_factory = session_factory
    else:
        app.state.session_factory = None  # bound by lifespan

    app.state.dispatcher = OutboxDispatcher(
        _LazySessionFactory(app),  # type: ignore[arg-type]
        app.state.publisher,
        settings=settings,
        clock=clock,
    )

    register_error_handlers(app)
    app.include_router(router)
    return app


class _LazySessionFactory:
    """Defers to ``app.state.session_factory``, which lifespan may set later."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def __call__(self, *args, **kwargs):
        factory = self._app.state.session_factory
        if factory is None:  # pragma: no cover - lifespan always binds it
            raise RuntimeError("session factory is not initialised")
        return factory(*args, **kwargs)
