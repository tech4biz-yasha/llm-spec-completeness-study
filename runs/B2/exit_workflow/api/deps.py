"""Composition root and request dependencies.

Authentication is NOT implemented here. risks.md#R3 leaves session scope, token
revocation and role-toggle semantics undecided, so this module accepts an
already-authenticated principal from the platform edge (gateway / auth module)
and enforces only the per-endpoint authorisation api.yaml states. Nothing about
session design is guessed (blockers.md#B-006).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..clock import Clock, SystemClock
from ..config import Settings, get_settings
from ..domain.states import Actor
from ..errors import NotAuthorized
from ..ports import EventPublisher, ExitReasonReference, NocRenderer, ObjectStore, PaymentGateway
from ..services.initiation import InitiationService
from ..services.inspection import InspectionService
from ..services.noc import NocService
from ..services.notification import NotificationService
from ..services.settlement import SettlementService
from ..services.stall import StallService
from ..services.transitions import Principal, TransitionService


@dataclass(slots=True)
class Container:
    settings: Settings
    clock: Clock
    session_factory: async_sessionmaker[AsyncSession]
    transitions: TransitionService
    notifications: NotificationService
    initiation: InitiationService
    inspection: InspectionService
    settlement: SettlementService
    noc: NocService
    stall: StallService


def build_container(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    publisher: EventPublisher,
    gateway: PaymentGateway,
    renderer: NocRenderer,
    object_store: ObjectStore,
    reason_reference: ExitReasonReference,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> Container:
    settings = settings or get_settings()
    clock = clock or SystemClock()
    transitions = TransitionService(clock)
    notifications = NotificationService(
        session_factory=session_factory,
        publisher=publisher,
        transitions=transitions,
        clock=clock,
        settings=settings,
    )
    noc = NocService(
        session_factory=session_factory,
        clock=clock,
        transitions=transitions,
        renderer=renderer,
        object_store=object_store,
        settings=settings,
    )
    return Container(
        settings=settings,
        clock=clock,
        session_factory=session_factory,
        transitions=transitions,
        notifications=notifications,
        initiation=InitiationService(
            session_factory=session_factory,
            clock=clock,
            transitions=transitions,
            notifications=notifications,
            reason_reference=reason_reference,
        ),
        inspection=InspectionService(
            session_factory=session_factory, clock=clock, transitions=transitions
        ),
        settlement=SettlementService(
            session_factory=session_factory,
            clock=clock,
            transitions=transitions,
            gateway=gateway,
            noc=noc,
        ),
        noc=noc,
        stall=StallService(
            session_factory=session_factory, clock=clock, transitions=transitions
        ),
    )


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_principal(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_user_role: Annotated[str | None, Header(alias="X-User-Role")] = None,
) -> Principal:
    """Read the principal asserted by the platform edge.

    The headers are a transport detail of this deployment, not a security
    boundary: the edge is responsible for authenticating the caller. Per-endpoint
    role checks (api.yaml `authz`) happen in the services, against the workflow.
    """
    if not x_user_id or not x_user_role:
        raise NotAuthorized("authenticated principal is required")
    try:
        role = Actor(x_user_role.strip().lower())
    except ValueError as exc:
        raise NotAuthorized(f"unknown role {x_user_role!r}") from exc
    return Principal(id=x_user_id.strip(), role=role)


ContainerDep = Annotated[Container, Depends(get_container)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
