"""Per-request context threaded through the service layer."""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.core.security import SYSTEM_PRINCIPAL, Principal
from app.domain.enums import ActorRole


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Who is acting, and the forensic breadcrumbs the audit trail needs (SRS A3)."""

    principal: Principal
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    #: Set when an ADMIN acts on a party's behalf; recorded in the audit trail.
    on_behalf_of: str | None = None

    @property
    def role(self) -> ActorRole:
        return self.principal.role

    @property
    def actor_id(self) -> object:
        return self.principal.actor_id

    def as_system(self) -> RequestContext:
        """Derive a system context for automated follow-on steps.

        NOC issuance and settlement confirmation are triggered by the system in reaction
        to an event, not by the user whose click started the chain; attributing them to
        that user would misreport who did what.
        """
        return replace(self, principal=SYSTEM_PRINCIPAL)


def system_context(request_id: str | None = None) -> RequestContext:
    return RequestContext(principal=SYSTEM_PRINCIPAL, request_id=request_id)
