"""Business policy checks.

Pure functions over plain values -- no ORM, no I/O -- so the rules that the SRS states
loosely (notice period, document completeness, dispute window, BR-1 lock) are directly
unit-testable and reviewable by the business.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.core.errors import BusinessRuleViolationError, ValidationFailedError
from app.core.money import ZERO, quantize
from app.domain.enums import (
    NOTICE_EXEMPT_REASONS,
    DocumentType,
    ExitReason,
    ExitWorkflowState,
)


@dataclass(frozen=True, slots=True)
class NoticePolicy:
    min_notice_days: int
    max_horizon_days: int


@dataclass(frozen=True, slots=True)
class NoticeAssessment:
    notice_days: int
    required_days: int
    is_short_notice: bool
    exempt: bool


def assess_move_out_date(
    *,
    move_out_date: date,
    today: date,
    reason: ExitReason,
    policy: NoticePolicy,
    admin_override: bool = False,
) -> NoticeAssessment:
    """Validate the requested move-out date (SRS T13 step 2).

    A short-notice request is rejected rather than silently accepted, because the notice
    period changes the commercial terms of the exit. ADMIN may override; the override is
    recorded in the audit trail by the caller.
    """
    if move_out_date < today:
        raise ValidationFailedError(
            "The move-out date cannot be in the past.",
            details={"field": "move_out_date", "value": move_out_date.isoformat()},
        )

    horizon = today + timedelta(days=policy.max_horizon_days)
    if move_out_date > horizon:
        raise ValidationFailedError(
            f"The move-out date cannot be more than {policy.max_horizon_days} days away.",
            details={"field": "move_out_date", "max_date": horizon.isoformat()},
        )

    notice_days = (move_out_date - today).days
    exempt = reason in NOTICE_EXEMPT_REASONS or admin_override
    is_short = notice_days < policy.min_notice_days and not exempt

    if is_short:
        raise BusinessRuleViolationError(
            rule="MIN_NOTICE_PERIOD",
            message=(
                f"A minimum of {policy.min_notice_days} days' notice is required. "
                f"The selected move-out date gives {notice_days} days."
            ),
            details={
                "field": "move_out_date",
                "notice_days": notice_days,
                "required_days": policy.min_notice_days,
                "earliest_allowed": (today + timedelta(days=policy.min_notice_days)).isoformat(),
            },
        )

    return NoticeAssessment(
        notice_days=notice_days,
        required_days=policy.min_notice_days,
        is_short_notice=notice_days < policy.min_notice_days,
        exempt=exempt,
    )


def assert_reason_details(reason: ExitReason, details: str | None) -> str | None:
    """Free-text detail is mandatory when the reason is OTHER."""
    cleaned = (details or "").strip() or None
    if reason is ExitReason.OTHER and not cleaned:
        raise ValidationFailedError(
            "Please describe your reason for leaving.",
            details={"field": "reason_details"},
        )
    if cleaned and len(cleaned) > 2000:
        raise ValidationFailedError(
            "Reason details must be 2000 characters or fewer.",
            details={"field": "reason_details", "max_length": 2000},
        )
    return cleaned


def assert_documents_complete(
    *,
    present: set[DocumentType],
    min_documents: int,
    required_types: set[DocumentType],
) -> None:
    """SRS T13 step 4: documents must be uploaded before submission."""
    missing = sorted(t.value for t in required_types - present)
    if missing:
        raise BusinessRuleViolationError(
            rule="REQUIRED_EXIT_DOCUMENTS",
            message="Required documents are missing: " + ", ".join(missing),
            details={"missing_document_types": missing},
        )
    if len(present) < min_documents:
        raise BusinessRuleViolationError(
            rule="REQUIRED_EXIT_DOCUMENTS",
            message=(
                f"At least {min_documents} supporting document(s) must be uploaded "
                "before submitting the exit request."
            ),
            details={"uploaded": len(present), "required": min_documents},
        )


@dataclass(frozen=True, slots=True)
class DisputeWindow:
    opened_at: datetime
    closes_at: datetime

    def is_open(self, now: datetime) -> bool:
        return now < self.closes_at

    @property
    def days(self) -> int:
        return (self.closes_at - self.opened_at).days


def dispute_window(opened_at: datetime, window_days: int) -> DisputeWindow:
    return DisputeWindow(opened_at, opened_at + timedelta(days=window_days))


def assert_dispute_window_open(window: DisputeWindow, now: datetime) -> None:
    if not window.is_open(now):
        raise BusinessRuleViolationError(
            rule="DISPUTE_WINDOW_CLOSED",
            message=(
                "The window for disputing the damage assessment has closed "
                f"({window.closes_at.isoformat()})."
            ),
            details={"closed_at": window.closes_at.isoformat()},
        )


def assert_no_open_disputes(open_dispute_count: int) -> None:
    """The owner cannot finalise a settlement while a tenant dispute is unresolved."""
    if open_dispute_count > 0:
        raise BusinessRuleViolationError(
            rule="UNRESOLVED_DISPUTES",
            message=(
                f"{open_dispute_count} disputed damage item(s) must be resolved before "
                "the settlement can be finalised."
            ),
            details={"open_disputes": open_dispute_count},
        )


def assert_deduction_amount(amount: Decimal, field: str = "amount") -> Decimal:
    value = quantize(amount)
    if value < ZERO:
        raise ValidationFailedError(
            "A deduction cannot be negative.", details={"field": field}
        )
    return value


def assert_settlement_finalisable(
    *,
    deposit: Decimal,
    total_deductions: Decimal,
    has_inspection_report: bool,
) -> None:
    """Guards on SRS O16 -- deductions are calculated *from* the inspection report."""
    if not has_inspection_report:
        raise BusinessRuleViolationError(
            rule="INSPECTION_REPORT_REQUIRED",
            message="An inspection report must be on file before the settlement is finalised.",
        )
    if quantize(deposit) < ZERO:
        raise ValidationFailedError("The security deposit on record is invalid.")
    assert_deduction_amount(total_deductions, "total_deductions")


@dataclass(frozen=True, slots=True)
class ContractBlock:
    """Why a new contract is blocked, per BR-1."""

    workflow_id: str
    reference: str
    state: ExitWorkflowState
    property_id: str
    tenant_id: str
    scope: str  # "PROPERTY" | "TENANT"

    @property
    def message(self) -> str:
        if self.scope == "PROPERTY":
            return (
                f"A new contract cannot be created for this property: exit workflow "
                f"{self.reference} is still in progress (status: {self.state.value}). "
                "The property is released once that workflow is marked COMPLETE."
            )
        return (
            f"This tenant cannot enter into a new contract: their exit workflow "
            f"{self.reference} is still in progress (status: {self.state.value}). "
            "The tenant is released once that workflow is fully completed."
        )


def assert_contract_creation_allowed(blocks: list[ContractBlock]) -> None:
    """BR-1 enforcement point (SRS §4.7, Owner BRD 3.17)."""
    if not blocks:
        return
    raise BusinessRuleViolationError(
        rule="BR-1",
        message=" ".join(b.message for b in blocks),
        details={
            "blocking_workflows": [
                {
                    "workflow_id": b.workflow_id,
                    "reference": b.reference,
                    "state": b.state.value,
                    "scope": b.scope,
                    "message": b.message,
                }
                for b in blocks
            ]
        },
    )
