"""FastAPI dependencies: auth, session/transaction boundary, service graph."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, Header, Path, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Ports, get_ports
from app.core.config import Settings, get_settings
from app.core.context import RequestContext
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.pagination import Cursor, clamp_limit
from app.core.security import Principal, decode_token
from app.db.session import get_sessionmaker
from app.domain.enums import ActorRole
from app.models.exit_workflow import ExitWorkflow
from app.services.factory import Services, build_services

_bearer = HTTPBearer(auto_error=False, description="JWT issued by the Meridian identity service")


def get_app_settings() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_app_ports() -> Ports:
    return get_ports()


PortsDep = Annotated[Ports, Depends(get_app_ports)]


async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_sessionmaker()
    async with factory() as session:
        yield session


async def get_services(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: SettingsDep,
    ports: PortsDep,
) -> AsyncIterator[Services]:
    """Request-scoped service graph and transaction boundary.

    Commits on success and runs post-commit side effects; rolls back on any exception
    so a partially applied workflow step can never be persisted.
    """
    services = build_services(session, settings=settings, ports=ports)
    try:
        yield services
    except Exception:
        await services.uow.rollback()
        raise
    else:
        await services.uow.commit()


ServicesDep = Annotated[Services, Depends(get_services)]


async def get_principal(
    request: Request,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> Principal:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("An access token is required.")
    principal = decode_token(credentials.credentials, settings)
    request.state.actor_id = str(principal.actor_id)
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def get_request_context(
    request: Request,
    principal: PrincipalDep,
    on_behalf_of: Annotated[
        str | None,
        Header(
            alias="X-On-Behalf-Of",
            description="Admin-only: the party id the action is performed for.",
        ),
    ] = None,
) -> RequestContext:
    if on_behalf_of is not None:
        if principal.role is not ActorRole.ADMIN:
            raise AuthorizationError("Only administrators may act on behalf of a party.")
        try:
            uuid.UUID(on_behalf_of)
        except ValueError as exc:
            raise AuthorizationError("X-On-Behalf-Of must be a valid actor id.") from exc

    client_host = request.client.host if request.client else None
    return RequestContext(
        principal=principal,
        request_id=getattr(request.state, "request_id", None),
        ip_address=client_host,
        user_agent=request.headers.get("user-agent"),
        on_behalf_of=on_behalf_of,
    )


ContextDep = Annotated[RequestContext, Depends(get_request_context)]


def require_roles(*roles: ActorRole) -> Callable[[RequestContext], RequestContext]:
    """Coarse role gate. Per-workflow party checks live in the service layer."""
    allowed = frozenset(roles)

    def _dependency(ctx: ContextDep) -> RequestContext:
        if ctx.principal.role is not ActorRole.ADMIN and ctx.principal.role not in allowed:
            raise AuthorizationError(
                "Your role is not permitted to perform this action.",
                details={"allowed_roles": sorted(r.value for r in allowed)},
            )
        return ctx

    return _dependency


async def get_workflow(
    workflow_id: Annotated[uuid.UUID, Path(description="Exit workflow id")],
    services: ServicesDep,
    ctx: ContextDep,
) -> ExitWorkflow:
    """Load a workflow the caller is a party to (read paths)."""
    return await services.workflows.get(workflow_id, ctx)


WorkflowDep = Annotated[ExitWorkflow, Depends(get_workflow)]


async def get_locked_workflow(
    workflow_id: Annotated[uuid.UUID, Path(description="Exit workflow id")],
    services: ServicesDep,
    ctx: ContextDep,
) -> ExitWorkflow:
    """Load a workflow under a row lock (command paths)."""
    workflow = await services.workflows_repo.get_for_update(workflow_id)
    inspection = await services.inspections_repo.get_for_workflow(workflow.id)
    services.engine.authorise_party(
        workflow, ctx, agency_id=inspection.agency_id if inspection else None
    )
    return workflow


LockedWorkflowDep = Annotated[ExitWorkflow, Depends(get_locked_workflow)]


def get_pagination(
    cursor: Annotated[str | None, Query(description="Opaque cursor from a prior page")] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> tuple[Cursor | None, int]:
    return (Cursor.decode(cursor) if cursor else None), clamp_limit(limit)


PaginationDep = Annotated[tuple[Cursor | None, int], Depends(get_pagination)]


def get_idempotency_key(
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description=(
                "Client-generated key making an unsafe request safe to retry. "
                "Required on money-moving endpoints."
            ),
            max_length=128,
        ),
    ] = None,
) -> str | None:
    return idempotency_key


IdempotencyKeyDep = Annotated[str | None, Depends(get_idempotency_key)]
