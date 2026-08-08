"""Principal resolution.

api.yaml states the *authorization* for each endpoint. Authentication — session
scope, token revocation, what a role toggle does to the JWT — is risks.md#R3 and
is explicitly undecided, so this module does not implement it. It defines the
seam the host application plugs its authenticated session into, and fails closed
until something is plugged in.

An unauthenticated request must never be treated as a tenant, an owner, or the
system. :class:`UnconfiguredPrincipalResolver` therefore raises rather than
returning a default identity.
"""

from __future__ import annotations

import logging
from typing import Protocol

from fastapi import Request

from exit_workflow.domain.enums import ActorRole
from exit_workflow.domain.errors import AuthorizationError
from exit_workflow.domain.principal import Principal

logger = logging.getLogger(__name__)


class PrincipalResolver(Protocol):
    """Turns an authenticated request into a :class:`Principal`."""

    async def resolve(self, request: Request) -> Principal:
        """Return the caller, or raise :class:`AuthorizationError`."""


class UnconfiguredPrincipalResolver:
    """Default binding: refuses every request."""

    async def resolve(self, request: Request) -> Principal:
        raise AuthorizationError(
            "No principal resolver is configured for the exit workflow module; the platform "
            "session layer must supply one (risks.md#R3)."
        )


class HeaderPrincipalResolver:
    """Reads the principal from request headers. Development and tests only.

    Trusting headers is only safe behind a gateway that sets them itself; this
    resolver has no way to check that, so it refuses to be constructed for a
    production deployment.
    """

    ID_HEADER = "X-Actor-Id"
    ROLE_HEADER = "X-Actor-Role"

    def __init__(self, *, allow: bool) -> None:
        if not allow:
            raise RuntimeError(
                "HeaderPrincipalResolver must not be used in production; supply the platform "
                "session resolver instead"
            )

    async def resolve(self, request: Request) -> Principal:
        subject = request.headers.get(self.ID_HEADER)
        role = request.headers.get(self.ROLE_HEADER)
        if not subject or not role:
            raise AuthorizationError("Missing actor headers.")
        try:
            return Principal(subject_id=subject, role=ActorRole(role))
        except ValueError as exc:
            raise AuthorizationError(f"Unknown actor role {role!r}.") from exc
