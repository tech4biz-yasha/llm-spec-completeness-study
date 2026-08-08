"""Typed application errors rendered as RFC 9457 problem details."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all errors this module raises deliberately."""

    status_code: int = 500
    code: str = "internal_error"
    title: str = "Internal server error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.detail = detail or self.title
        self.extra = extra or {}
        self.headers = headers or {}
        super().__init__(self.detail)

    def to_problem(self, instance: str | None = None) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": f"https://errors.meridian.ae/exit-workflow/{self.code}",
            "title": self.title,
            "status": self.status_code,
            "code": self.code,
            "detail": self.detail,
        }
        if instance:
            problem["instance"] = instance
        problem.update(self.extra)
        return problem


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    title = "Resource not found"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"
    title = "Authentication required"

    def __init__(self, detail: str | None = None, **kw: Any) -> None:
        super().__init__(detail, **kw)
        self.headers.setdefault("WWW-Authenticate", "Bearer")


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"
    title = "Not permitted"


class ValidationError(AppError):
    status_code = 422
    code = "validation_failed"
    title = "Request failed validation"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    title = "Conflicting state"


class InvalidTransitionError(ConflictError):
    code = "invalid_state_transition"
    title = "Invalid workflow state transition"

    def __init__(self, current: str, target: str, detail: str | None = None) -> None:
        super().__init__(
            detail or f"Cannot move exit workflow from {current} to {target}.",
            extra={"current_status": current, "requested_status": target},
        )


class BusinessRuleViolation(ConflictError):
    """A named SRS business rule blocked the action (e.g. BR-1)."""

    code = "business_rule_violation"
    title = "Business rule violation"

    def __init__(self, rule: str, detail: str, *, extra: dict[str, Any] | None = None) -> None:
        payload = {"rule": rule}
        payload.update(extra or {})
        super().__init__(detail, extra=payload)
        self.rule = rule


class IdempotencyConflictError(ConflictError):
    code = "idempotency_conflict"
    title = "Idempotent request already in progress"


class IdempotencyKeyReuseError(ValidationError):
    code = "idempotency_key_reuse"
    title = "Idempotency key reused with a different payload"


class UpstreamServiceError(AppError):
    """A dependency (Property service, agency directory, gateway) failed."""

    status_code = 502
    code = "upstream_unavailable"
    title = "Upstream service unavailable"

    def __init__(self, service: str, detail: str | None = None) -> None:
        super().__init__(
            detail or f"The {service} service is unavailable; please retry.",
            extra={"service": service},
        )
        self.service = service


class PaymentFailedError(AppError):
    status_code = 502
    code = "payment_failed"
    title = "Deposit payout could not be processed"


class StorageError(AppError):
    status_code = 500
    code = "storage_error"
    title = "Document storage failure"


class ConcurrencyError(ConflictError):
    code = "concurrent_modification"
    title = "Resource was modified concurrently"
