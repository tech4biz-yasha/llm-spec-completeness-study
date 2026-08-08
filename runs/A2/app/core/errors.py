"""Domain and API error taxonomy.

Every failure surfaced to a client carries a stable machine-readable ``code`` so the
tenant app / owner portal can render the SRS-mandated warning messages (BR-1) without
string-matching on prose.
"""

from __future__ import annotations

from typing import Any


class ExitWorkflowError(Exception):
    """Base class for all module errors."""

    status_code: int = 400
    code: str = "exit_workflow_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
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


class NotFoundError(ExitWorkflowError):
    status_code = 404
    code = "not_found"


class ValidationFailedError(ExitWorkflowError):
    status_code = 422
    code = "validation_failed"


class AuthenticationError(ExitWorkflowError):
    status_code = 401
    code = "unauthenticated"


class AuthorizationError(ExitWorkflowError):
    status_code = 403
    code = "forbidden"


class ConflictError(ExitWorkflowError):
    status_code = 409
    code = "conflict"


class IllegalTransitionError(ConflictError):
    code = "illegal_state_transition"

    def __init__(self, current: str, requested: str, allowed: list[str]) -> None:
        super().__init__(
            f"Cannot move exit workflow from {current} to {requested}.",
            details={"current_state": current, "requested_state": requested, "allowed": allowed},
        )


class ConcurrentModificationError(ConflictError):
    code = "concurrent_modification"

    def __init__(self, message: str = "The record was modified concurrently. Retry.") -> None:
        super().__init__(message)


class BusinessRuleViolationError(ConflictError):
    """A named business rule from SRS §4.7 was violated."""

    code = "business_rule_violation"

    def __init__(self, rule: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details={**(details or {}), "rule": rule})
        self.rule = rule


class IdempotencyConflictError(ConflictError):
    code = "idempotency_key_reuse"

    def __init__(self) -> None:
        super().__init__(
            "This Idempotency-Key was already used with a different request payload.",
        )


class PayloadTooLargeError(ExitWorkflowError):
    status_code = 413
    code = "payload_too_large"


class UnsupportedMediaTypeError(ExitWorkflowError):
    status_code = 415
    code = "unsupported_media_type"


class RateLimitedError(ExitWorkflowError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, retry_after_seconds: int = 60) -> None:
        super().__init__(
            "Too many requests.", details={"retry_after_seconds": retry_after_seconds}
        )
        self.retry_after_seconds = retry_after_seconds


class DependencyFailureError(ExitWorkflowError):
    """An outbound port (payments, storage, notifications) failed."""

    status_code = 502
    code = "dependency_failure"
