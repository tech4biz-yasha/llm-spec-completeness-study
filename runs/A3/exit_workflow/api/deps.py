"""FastAPI dependency wiring."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.container import AppContainer
from exit_workflow.core.config import Settings
from exit_workflow.core.errors import UnauthorizedError, ValidationError
from exit_workflow.core.logging import principal_ctx, request_id_ctx
from exit_workflow.core.security import Principal, decode_token
from exit_workflow.services.audit import AuditRecorder
from exit_workflow.services.context import ServiceContext
from exit_workflow.services.documents import DocumentService
from exit_workflow.services.eligibility import EligibilityService
from exit_workflow.services.events import EventRecorder
from exit_workflow.services.idempotency import IdempotencyService
from exit_workflow.services.inspection import InspectionService
from exit_workflow.services.noc import NocService
from exit_workflow.services.notifications import NotificationService
from exit_workflow.services.settlement import SettlementService
from exit_workflow.services.workflow import ExitWorkflowService

_bearer = HTTPBearer(auto_error=False, description="Platform-issued access token")


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_settings(request: Request) -> Settings:
    return request.app.state.container.settings


ContainerDep = Annotated[AppContainer, Depends(get_container)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One transaction per request: commit on success, roll back on error."""

    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()


SessionDep = Annotated[AsyncSession, Depends(db_session)]


async def current_principal(
    request: Request,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> Principal:
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("A bearer access token is required.")
    principal = decode_token(credentials.credentials, settings)
    principal_ctx.set(principal.audit_actor)
    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def _client_ip(request: Request) -> str | None:
    # X-Forwarded-For is only meaningful behind the platform ingress, which
    # overwrites it; the left-most entry is the originating client.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None


def service_context(request: Request, principal: PrincipalDep) -> ServiceContext:
    return ServiceContext(
        principal=principal,
        request_id=request_id_ctx.get() or getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


ContextDep = Annotated[ServiceContext, Depends(service_context)]


def idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    """Required on money-moving endpoints."""

    if not idempotency_key or not idempotency_key.strip():
        raise ValidationError(
            "An Idempotency-Key header is required for this request.",
            extra={"header": "Idempotency-Key"},
        )
    value = idempotency_key.strip()
    if len(value) > 255:
        raise ValidationError("Idempotency-Key must be at most 255 characters.")
    return value


IdempotencyKeyDep = Annotated[str, Depends(idempotency_key)]


# -- service factories ------------------------------------------------------
def audit_recorder(session: SessionDep, settings: SettingsDep) -> AuditRecorder:
    return AuditRecorder(session, settings)


def event_recorder(session: SessionDep, settings: SettingsDep) -> EventRecorder:
    return EventRecorder(session, settings)


def notification_service(session: SessionDep, settings: SettingsDep) -> NotificationService:
    return NotificationService(session, settings)


def eligibility_service(session: SessionDep) -> EligibilityService:
    return EligibilityService(session)


def idempotency_service(session: SessionDep) -> IdempotencyService:
    return IdempotencyService(session)


AuditDep = Annotated[AuditRecorder, Depends(audit_recorder)]
EventsDep = Annotated[EventRecorder, Depends(event_recorder)]
NotificationsDep = Annotated[NotificationService, Depends(notification_service)]
EligibilityDep = Annotated[EligibilityService, Depends(eligibility_service)]
IdempotencyDep = Annotated[IdempotencyService, Depends(idempotency_service)]


def document_service(
    session: SessionDep,
    settings: SettingsDep,
    ctx: ContextDep,
    container: ContainerDep,
    audit: AuditDep,
    events: EventsDep,
) -> DocumentService:
    return DocumentService(
        session, settings, ctx, storage=container.storage, audit=audit, events=events
    )


DocumentServiceDep = Annotated[DocumentService, Depends(document_service)]


def workflow_service(
    session: SessionDep,
    settings: SettingsDep,
    ctx: ContextDep,
    container: ContainerDep,
    audit: AuditDep,
    events: EventsDep,
    notifications: NotificationsDep,
    eligibility: EligibilityDep,
) -> ExitWorkflowService:
    return ExitWorkflowService(
        session,
        settings,
        ctx,
        audit=audit,
        events=events,
        notifications=notifications,
        contracts=container.contracts,
        eligibility=eligibility,
    )


WorkflowServiceDep = Annotated[ExitWorkflowService, Depends(workflow_service)]


def inspection_service(
    session: SessionDep,
    settings: SettingsDep,
    ctx: ContextDep,
    container: ContainerDep,
    audit: AuditDep,
    events: EventsDep,
    notifications: NotificationsDep,
    documents: DocumentServiceDep,
) -> InspectionService:
    return InspectionService(
        session,
        settings,
        ctx,
        audit=audit,
        events=events,
        notifications=notifications,
        agencies=container.agencies,
        documents=documents,
    )


InspectionServiceDep = Annotated[InspectionService, Depends(inspection_service)]


def noc_service(
    session: SessionDep,
    settings: SettingsDep,
    ctx: ContextDep,
    container: ContainerDep,
    audit: AuditDep,
    events: EventsDep,
    notifications: NotificationsDep,
) -> NocService:
    return NocService(
        session,
        settings,
        ctx,
        storage=container.storage,
        audit=audit,
        events=events,
        notifications=notifications,
    )


NocServiceDep = Annotated[NocService, Depends(noc_service)]


def settlement_service(
    session: SessionDep,
    settings: SettingsDep,
    ctx: ContextDep,
    container: ContainerDep,
    audit: AuditDep,
    events: EventsDep,
    notifications: NotificationsDep,
    noc: NocServiceDep,
) -> SettlementService:
    return SettlementService(
        session,
        settings,
        ctx,
        audit=audit,
        events=events,
        notifications=notifications,
        gateway=container.gateway,
        noc=noc,
    )


SettlementServiceDep = Annotated[SettlementService, Depends(settlement_service)]


def parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{field} must be a UUID.", extra={"field": field}) from exc
