"""Domain errors and their HTTP representation.

Every error the module raises carries a stable machine-readable ``code`` so clients can
branch on failure without string-matching messages.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for all expected, client-visible failures."""

    status_code: int = 400
    code: str = "domain_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }
        if request_id:
            payload["error"]["request_id"] = request_id
        return payload


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ValidationError(DomainError):
    status_code = 422
    code = "validation_error"


class AuthenticationError(DomainError):
    status_code = 401
    code = "unauthenticated"


class AuthorizationError(DomainError):
    status_code = 403
    code = "forbidden"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class InvalidStateTransition(ConflictError):
    """A workflow operation was attempted from a state that does not permit it."""

    code = "invalid_state_transition"

    def __init__(self, current: str, attempted: str, *, allowed: list[str] | None = None) -> None:
        super().__init__(
            f"cannot transition exit workflow from {current} to {attempted}",
            details={
                "current_state": current,
                "attempted_state": attempted,
                "allowed_next_states": sorted(allowed or []),
            },
        )


class WorkflowAlreadyActive(ConflictError):
    """An active exit workflow already exists for the contract or property."""

    code = "workflow_already_active"


class ContractBlockedError(ConflictError):
    """BR-1: a new contract is blocked by an incomplete exit workflow."""

    code = "contract_blocked_by_exit_workflow"


class SettlementNotPayable(ConflictError):
    code = "settlement_not_payable"


class IdempotencyConflict(ConflictError):
    code = "idempotency_key_reuse"
