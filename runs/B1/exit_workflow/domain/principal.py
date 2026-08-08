"""The authenticated caller.

api.yaml gives each endpoint an ``authz`` line ("tenant, own active contract
only"; "owner"; "inspection_agency"; "system|owner"). This module carries the
identity those lines are checked against.

Authentication itself belongs to the platform's session layer, which risks.md#R3
records as undecided (session scope, refresh-token revocation, role toggle). This
module therefore consumes an already-authenticated principal and never mints or
validates one; see :mod:`exit_workflow.api.security`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from exit_workflow.domain.enums import ActorRole


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller acting in one role."""

    subject_id: str
    role: ActorRole

    @property
    def uuid(self) -> uuid.UUID | None:
        """The subject as a UUID, or ``None`` for non-UUID subjects (e.g. system)."""
        try:
            return uuid.UUID(self.subject_id)
        except (ValueError, AttributeError):
            return None

    def is_role(self, *roles: ActorRole) -> bool:
        return self.role in roles


SYSTEM_PRINCIPAL = Principal(subject_id="system", role=ActorRole.SYSTEM)
