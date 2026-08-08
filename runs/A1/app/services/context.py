"""Per-request context threaded through the services for authorisation and audit."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.errors import AuthenticationError, AuthorizationError
from app.models.workflow import ActorType
from app.security import Principal, PrincipalRole

_ROLE_TO_ACTOR = {
    PrincipalRole.TENANT: ActorType.TENANT,
    PrincipalRole.OWNER: ActorType.OWNER,
    PrincipalRole.AGENCY: ActorType.AGENCY,
    PrincipalRole.ADMIN: ActorType.ADMIN,
}


@dataclass(frozen=True, slots=True)
class RequestContext:
    principal: Principal | None = None
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None

    @classmethod
    def system(cls, request_id: str | None = None) -> RequestContext:
        """Context for background jobs and internally triggered transitions."""
        return cls(principal=None, request_id=request_id)

    @property
    def actor_type(self) -> ActorType:
        if self.principal is None:
            return ActorType.SYSTEM
        return _ROLE_TO_ACTOR[self.principal.role]

    @property
    def actor_id(self) -> uuid.UUID | None:
        return self.principal.id if self.principal else None

    def require_principal(self) -> Principal:
        if self.principal is None:
            raise AuthenticationError("this operation requires an authenticated caller")
        return self.principal

    def require_role(self, *roles: PrincipalRole) -> Principal:
        principal = self.require_principal()
        if principal.role not in roles and not principal.is_admin:
            raise AuthorizationError(
                "caller role is not permitted to perform this operation",
                details={
                    "required_roles": sorted(r.value for r in roles),
                    "actual_role": principal.role.value,
                },
            )
        return principal
