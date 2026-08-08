"""Domain enumerations.

Every value is persisted as a native PostgreSQL enum. Adding a value therefore
requires a migration — deliberate, because these drive the state machine.
"""

from __future__ import annotations

from enum import StrEnum


class ExitWorkflowStatus(StrEnum):
    """Lifecycle of a tenant exit (T13 + Appendix B O15/O16)."""

    INITIATED = "INITIATED"
    PENDING_OWNER_APPROVAL = "PENDING_OWNER_APPROVAL"
    OWNER_APPROVED = "OWNER_APPROVED"
    INSPECTION_REQUESTED = "INSPECTION_REQUESTED"
    INSPECTION_SCHEDULED = "INSPECTION_SCHEDULED"
    INSPECTION_COMPLETED = "INSPECTION_COMPLETED"
    DAMAGE_REVIEW = "DAMAGE_REVIEW"
    SETTLEMENT_PENDING = "SETTLEMENT_PENDING"
    SETTLEMENT_COMPLETED = "SETTLEMENT_COMPLETED"
    NOC_ISSUED = "NOC_ISSUED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


#: Statuses after which the property/tenant are released from the BR-1 lock.
TERMINAL_STATUSES: frozenset[ExitWorkflowStatus] = frozenset(
    {
        ExitWorkflowStatus.COMPLETED,
        ExitWorkflowStatus.CANCELLED,
        ExitWorkflowStatus.REJECTED,
    }
)

#: Statuses that hold the BR-1 contract lock (i.e. "not yet COMPLETE").
ACTIVE_STATUSES: frozenset[ExitWorkflowStatus] = frozenset(
    set(ExitWorkflowStatus) - set(TERMINAL_STATUSES)
)


class ExitReason(StrEnum):
    END_OF_TERM = "END_OF_TERM"
    RELOCATION = "RELOCATION"
    JOB_CHANGE = "JOB_CHANGE"
    PROPERTY_PURCHASED = "PROPERTY_PURCHASED"
    RENT_INCREASE = "RENT_INCREASE"
    MAINTENANCE_DISSATISFACTION = "MAINTENANCE_DISSATISFACTION"
    FINANCIAL_HARDSHIP = "FINANCIAL_HARDSHIP"
    LANDLORD_REQUEST = "LANDLORD_REQUEST"
    OTHER = "OTHER"


class ExitStep(StrEnum):
    """The ten T13 steps, in order.

    T13 lists eleven fragments; the first ("Exit section") is navigation into
    the feature rather than a state-bearing step, so the ten persisted steps
    begin at the move-out date. See README "Assumptions and spec gaps".
    """

    MOVE_OUT_DATE = "MOVE_OUT_DATE"
    REASON_ENTRY = "REASON_ENTRY"
    DOCUMENT_UPLOAD = "DOCUMENT_UPLOAD"
    WORKFLOW_ID_GENERATION = "WORKFLOW_ID_GENERATION"
    OWNER_NOTIFICATION = "OWNER_NOTIFICATION"
    INSPECTION_SCHEDULING = "INSPECTION_SCHEDULING"
    DAMAGE_REVIEW = "DAMAGE_REVIEW"
    DEPOSIT_REFUND = "DEPOSIT_REFUND"
    NOC_DOWNLOAD = "NOC_DOWNLOAD"
    WORKFLOW_COMPLETION = "WORKFLOW_COMPLETION"


STEP_NUMBERS: dict[ExitStep, int] = {step: i for i, step in enumerate(ExitStep, start=1)}


class StepState(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"


class DocumentType(StrEnum):
    EXIT_REQUEST_ATTACHMENT = "EXIT_REQUEST_ATTACHMENT"
    EMIRATES_ID = "EMIRATES_ID"
    TENANCY_CONTRACT = "TENANCY_CONTRACT"
    UTILITY_CLEARANCE = "UTILITY_CLEARANCE"
    KEY_HANDOVER = "KEY_HANDOVER"
    DAMAGE_PHOTO = "DAMAGE_PHOTO"
    INSPECTION_REPORT = "INSPECTION_REPORT"
    EXIT_NOC = "EXIT_NOC"
    OTHER = "OTHER"


#: Types a tenant is allowed to attach to their own exit request.
TENANT_UPLOADABLE_TYPES: frozenset[DocumentType] = frozenset(
    {
        DocumentType.EXIT_REQUEST_ATTACHMENT,
        DocumentType.EMIRATES_ID,
        DocumentType.TENANCY_CONTRACT,
        DocumentType.UTILITY_CLEARANCE,
        DocumentType.KEY_HANDOVER,
        DocumentType.OTHER,
    }
)

#: Types an inspection agency may attach.
AGENCY_UPLOADABLE_TYPES: frozenset[DocumentType] = frozenset(
    {DocumentType.DAMAGE_PHOTO, DocumentType.INSPECTION_REPORT}
)


class InspectionStatus(StrEnum):
    REQUESTED = "REQUESTED"
    SLOTS_PROPOSED = "SLOTS_PROPOSED"
    SCHEDULED = "SCHEDULED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


INSPECTION_TERMINAL_STATUSES: frozenset[InspectionStatus] = frozenset(
    {InspectionStatus.COMPLETED, InspectionStatus.CANCELLED}
)


class SlotStatus(StrEnum):
    PROPOSED = "PROPOSED"
    SELECTED = "SELECTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"


class DamageCategory(StrEnum):
    STRUCTURAL = "STRUCTURAL"
    PAINT_AND_WALLS = "PAINT_AND_WALLS"
    FLOORING = "FLOORING"
    PLUMBING = "PLUMBING"
    ELECTRICAL = "ELECTRICAL"
    APPLIANCES = "APPLIANCES"
    FIXTURES = "FIXTURES"
    FURNITURE = "FURNITURE"
    CLEANING = "CLEANING"
    KEYS_AND_ACCESS = "KEYS_AND_ACCESS"
    OTHER = "OTHER"


class DamageSeverity(StrEnum):
    MINOR = "MINOR"
    MODERATE = "MODERATE"
    SEVERE = "SEVERE"


class DamageReportStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    DISPUTED = "DISPUTED"
    DISPUTE_RESOLVED = "DISPUTE_RESOLVED"
    FINALIZED = "FINALIZED"


class TenantReviewDecision(StrEnum):
    ACKNOWLEDGE = "ACKNOWLEDGE"
    DISPUTE = "DISPUTE"


class SettlementStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PAID = "PAID"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PayoutMethod(StrEnum):
    BANK_TRANSFER = "BANK_TRANSFER"
    CHEQUE = "CHEQUE"
    WALLET = "WALLET"
    #: Nothing to pay out — deductions consumed the whole deposit.
    NONE = "NONE"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ActorType(StrEnum):
    TENANT = "TENANT"
    OWNER = "OWNER"
    INSPECTION_AGENCY = "INSPECTION_AGENCY"
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class NotificationChannel(StrEnum):
    EMAIL = "EMAIL"
    PUSH = "PUSH"
    SMS = "SMS"
