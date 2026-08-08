"""Assembles the service graph for one session.

Composition happens here rather than in the route handlers, so the same wiring serves
the API, the background workers and the tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Ports, get_ports
from app.core.clock import Clock
from app.core.config import Settings, get_settings
from app.db.session import session_scope
from app.repositories.exit_workflow import ExitWorkflowRepository
from app.repositories.support import (
    DocumentRepository,
    InspectionRepository,
    NocRepository,
    SettlementRepository,
)
from app.services.audit import AuditService
from app.services.contract_guard import ContractGuard
from app.services.documents import DocumentService
from app.services.events import EventRecorder
from app.services.exit_workflow import ExitWorkflowService
from app.services.inspection import InspectionService
from app.services.noc import NocService
from app.services.notifications import NotificationService
from app.services.payout_dispatch import PayoutDispatchService
from app.services.settlement import SettlementService
from app.services.unit_of_work import UnitOfWork
from app.services.workflow_engine import WorkflowEngine


@dataclass(frozen=True, slots=True)
class Services:
    uow: UnitOfWork
    engine: WorkflowEngine
    audit: AuditService
    events: EventRecorder
    notifications: NotificationService
    workflows: ExitWorkflowService
    inspections: InspectionService
    documents: DocumentService
    settlements: SettlementService
    nocs: NocService
    guard: ContractGuard
    # Repositories exposed for read paths that do not need a service.
    workflows_repo: ExitWorkflowRepository
    documents_repo: DocumentRepository
    inspections_repo: InspectionRepository
    settlements_repo: SettlementRepository
    nocs_repo: NocRepository


def build_services(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    ports: Ports | None = None,
    enable_payout_dispatch: bool = True,
) -> Services:
    settings = settings or get_settings()
    ports = ports or get_ports()
    clock = clock or ports.clock

    uow = UnitOfWork(session)
    audit = AuditService(session, settings, clock)
    events = EventRecorder(session, settings, clock)
    engine = WorkflowEngine(session, settings, clock, audit, events)
    notifications = NotificationService(ports.notifier, uow)

    nocs = NocService(
        session=session,
        settings=settings,
        clock=clock,
        engine=engine,
        notifications=notifications,
        storage=ports.storage,
        renderer=ports.noc_renderer,
    )
    inspections = InspectionService(
        session=session,
        settings=settings,
        clock=clock,
        engine=engine,
        notifications=notifications,
    )
    documents = DocumentService(
        session=session,
        settings=settings,
        clock=clock,
        engine=engine,
        storage=ports.storage,
    )
    settlements = SettlementService(
        session=session,
        settings=settings,
        clock=clock,
        engine=engine,
        notifications=notifications,
        uow=uow,
        noc_issuer=nocs,
        payout_dispatcher=(
            PayoutDispatchService(
                session_factory=session_scope,
                settings=settings,
                clock=clock,
                payments=ports.payments,
            )
            if enable_payout_dispatch
            else None
        ),
    )
    workflows = ExitWorkflowService(
        session=session,
        settings=settings,
        clock=clock,
        engine=engine,
        notifications=notifications,
        inspections=inspections,
    )

    return Services(
        uow=uow,
        engine=engine,
        audit=audit,
        events=events,
        notifications=notifications,
        workflows=workflows,
        inspections=inspections,
        documents=documents,
        settlements=settlements,
        nocs=nocs,
        guard=ContractGuard(session),
        workflows_repo=ExitWorkflowRepository(session),
        documents_repo=DocumentRepository(session),
        inspections_repo=InspectionRepository(session),
        settlements_repo=SettlementRepository(session),
        nocs_repo=NocRepository(session),
    )
