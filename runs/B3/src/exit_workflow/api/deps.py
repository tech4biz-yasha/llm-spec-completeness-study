"""Request-scoped dependencies.

Authentication is not this module's decision and cannot be: risks.md#R3 leaves session
scope, token revocation and role-toggle semantics open, and nothing in the kit describes a
token format. So the principal arrives through a resolver the deployment supplies. The
default resolver trusts identity headers set by the edge gateway — appropriate only where
the gateway is the authentication boundary. See blockers.md#B-7.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from fastapi import Request

from ..enums import Actor
from ..errors import NotAuthorized
from ..services.identity import Principal
from ..services.workflow import ExitWorkflowService

ACTOR_ID_HEADER = "X-Actor-Id"
ACTOR_ROLE_HEADER = "X-Actor-Role"


@runtime_checkable
class PrincipalResolver(Protocol):
    def __call__(self, request: Request) -> Principal: ...


class HeaderPrincipalResolver:
    """Reads the identity the edge gateway asserted. No token verification here."""

    def __call__(self, request: Request) -> Principal:
        user_id = request.headers.get(ACTOR_ID_HEADER)
        raw_role = request.headers.get(ACTOR_ROLE_HEADER)
        if not user_id or not raw_role:
            raise NotAuthorized("caller identity was not asserted by the gateway")
        try:
            # api.yaml spells the inspector role ``inspection_agency``; states.yaml spells
            # it ``inspector``. Actor.normalize reconciles them. blockers.md#B-9
            role = Actor.normalize(raw_role)
        except ValueError as exc:
            raise NotAuthorized(f"unknown role {raw_role!r}") from exc
        return Principal(user_id=user_id, role=role)


def get_service(request: Request) -> ExitWorkflowService:
    return request.app.state.exit_workflow_service


def get_principal(request: Request) -> Principal:
    resolver: PrincipalResolver = request.app.state.principal_resolver
    return resolver(request)
