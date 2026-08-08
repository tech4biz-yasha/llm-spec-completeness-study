"""The acting principal.

api.yaml states the authorization for each endpoint (``authz: tenant, own active contract
only``, ``owner``, ``inspection_agency``, ``system|owner``). It does not state how a
caller is authenticated — and session/token design is explicitly blocked by risks.md#R3.
So this module models only what the authorization rules need (an identity and a role) and
takes it from an injected resolver; it implements no token verification of its own.
See blockers.md#B-7.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..enums import Actor
from ..errors import NotAuthorized


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    role: Actor

    def require(self, *allowed: Actor) -> None:
        """Enforce an api.yaml ``authz`` line."""
        if self.role not in allowed:
            raise NotAuthorized(
                f"role {self.role} may not perform this action",
                required_roles=[str(role) for role in allowed],
            )
