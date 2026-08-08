"""Application assembly.

``build_app`` takes every port explicitly. The exit-reason reference list has no default:
risks.md carries it as an open item (Appendix A, "Reference data dictionary, specifically
exit reasons"), so the application refuses to start rather than fall back to a guessed
list of reasons. See blockers.md#B-2.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from .adapters.events import LoggingEventPublisher
from .adapters.noc_pdf import SimpleNocPdfRenderer
from .adapters.storage import InMemoryObjectStorage
from .api.deps import HeaderPrincipalResolver, PrincipalResolver
from .api.errors import install_error_handlers
from .api.routes import router
from .clock import Clock, SystemClock
from .config import UAE_REGIONS, Settings, get_settings
from .db.session import build_engine, build_session_factory
from .errors import SpecUnresolved
from .ports.events import EventPublisher
from .ports.payments import PaymentGateway
from .ports.reference import ExitReasonReference
from .ports.renderer import NocRenderer
from .ports.storage import ObjectStorage
from .services.workflow import ExitWorkflowService

__all__ = ["build_app"]


def build_app(
    *,
    reasons: ExitReasonReference,
    gateway: PaymentGateway,
    settings: Settings | None = None,
    engine: Engine | None = None,
    session_factory: sessionmaker[Session] | None = None,
    clock: Clock | None = None,
    storage: ObjectStorage | None = None,
    renderer: NocRenderer | None = None,
    publisher: EventPublisher | None = None,
    principal_resolver: PrincipalResolver | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    clock = clock or SystemClock()
    engine = engine or build_engine(settings)
    session_factory = session_factory or build_session_factory(engine)
    storage = storage or InMemoryObjectStorage(region=settings.noc_bucket_region)
    renderer = renderer or SimpleNocPdfRenderer()
    publisher = publisher or LoggingEventPublisher()

    if reasons is None or not reasons.codes():  # pragma: no cover - guarded by the port
        raise SpecUnresolved(
            "B-2",
            "no exit reason reference list supplied; rules.yaml#EXIT-02 requires one and "
            "risks.md carries it as an open item",
        )
    # rules.yaml#EXIT-09 — NOCs live in the UAE region.
    if storage.region not in UAE_REGIONS:
        raise ValueError(
            f"NOC storage region {storage.region!r} is not a UAE region (rules.yaml#EXIT-09)"
        )

    service = ExitWorkflowService(
        session_factory=session_factory,
        settings=settings,
        clock=clock,
        reasons=reasons,
        gateway=gateway,
        storage=storage,
        renderer=renderer,
        publisher=publisher,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="Tenant exit workflow",
        version="1.0.0",
        description=(
            "Initiation through completion, deposit settlement and NOC issuance. "
            "Behaviour is fixed by the specification kit: rules.yaml, states.yaml, "
            "edges.yaml, api.yaml and algorithm.md."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.exit_workflow_service = service
    app.state.principal_resolver = principal_resolver or HeaderPrincipalResolver()
    install_error_handlers(app)
    app.include_router(router)
    return app
