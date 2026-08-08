"""Per-request context threaded through the service layer."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from exit_workflow.core.security import Principal, Role
from exit_workflow.domain.enums import ActorType

_ROLE_TO_ACTOR: dict[Role, ActorType] = {
    Role.TENANT: ActorType.TENANT,
    Role.OWNER: ActorType.OWNER,
    Role.INSPECTION_AGENCY: ActorType.INSPECTION_AGENCY,
    Role.ADMIN: ActorType.ADMIN,
    Role.SERVICE: ActorType.SYSTEM,
}


@dataclass(frozen=True, slots=True)
class ServiceContext:
    """Who is acting, and the request metadata the audit trail needs."""

    principal: Principal | None = None
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def actor_type(self) -> ActorType:
        if self.principal is None:
            return ActorType.SYSTEM
        return _ROLE_TO_ACTOR[self.principal.role]

    @property
    def actor_id(self) -> uuid.UUID | None:
        return self.principal.subject_id if self.principal else None

    @property
    def actor_email(self) -> str | None:
        return self.principal.email if self.principal else None

    @property
    def role(self) -> Role | None:
        """``None`` marks a system-driven action for the state machine."""

        return self.principal.role if self.principal else None

    def require_principal(self) -> Principal:
        if self.principal is None:  # pragma: no cover - defensive
            raise RuntimeError("This operation requires an authenticated principal.")
        return self.principal

    @classmethod
    def system(cls, request_id: str | None = None) -> ServiceContext:
        return cls(principal=None, request_id=request_id)
