"""Domain enumerations.

Enum *values* are the wire contract and the stored representation; they are treated as
append-only. Renaming a value is a breaking change requiring a data migration.
"""

from __future__ import annotations

from enum import StrEnum


class ExitWorkflowState(StrEnum):
    """States of the exit workflow aggregate.

    Mapped to SRS T13's ten steps and Appendix B's O15/O16 narratives:

    ==============================  =====================================================
    State                           SRS step
    ==============================  =====================================================
    DRAFT                           1-4  exit section, move-out date, reason, documents
    SUBMITTED                       5-6  workflow ID generated, owner notified
    OWNER_APPROVED                  O15  owner approves, agency emailed with property info
    INSPECTION_SLOTS_PROPOSED       O15  agency responds with available dates
    INSPECTION_SCHEDULED            7    owner/tenant select date
    INSPECTION_COMPLETED            O15  inspection occurs, report + photos uploaded
    DAMAGE_REVIEW                   8    damage review (owner adjusts, tenant may dispute)
    SETTLEMENT_PENDING              9    deduction computed, awaiting owner 'Pay Deposit'
    SETTLEMENT_PROCESSING           9    payout submitted to the provider, awaiting result
    SETTLEMENT_COMPLETED            9    refund confirmed (deposit minus damage)
    NOC_ISSUED                      10   digital Exit NOC auto-generated, downloadable
    COMPLETED                       11   workflow completion (releases the BR-1 lock)
    ==============================  =====================================================

    ``REJECTED``, ``CANCELLED`` and ``EXPIRED`` are terminal off-ramps that the SRS does
    not describe but that any real deployment needs; like ``COMPLETED`` they release the
    BR-1 contract lock, because an abandoned request must not block a property forever.
    """

    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    OWNER_APPROVED = "OWNER_APPROVED"
    INSPECTION_SLOTS_PROPOSED = "INSPECTION_SLOTS_PROPOSED"
    INSPECTION_SCHEDULED = "INSPECTION_SCHEDULED"
    INSPECTION_COMPLETED = "INSPECTION_COMPLETED"
    DAMAGE_REVIEW = "DAMAGE_REVIEW"
    SETTLEMENT_PENDING = "SETTLEMENT_PENDING"
    SETTLEMENT_PROCESSING = "SETTLEMENT_PROCESSING"
    SETTLEMENT_COMPLETED = "SETTLEMENT_COMPLETED"
    NOC_ISSUED = "NOC_ISSUED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


#: States after which the workflow can no longer change.
TERMINAL_STATES: frozenset[ExitWorkflowState] = frozenset(
    {
        ExitWorkflowState.COMPLETED,
        ExitWorkflowState.REJECTED,
        ExitWorkflowState.CANCELLED,
        ExitWorkflowState.EXPIRED,
    }
)

#: States in which BR-1 blocks new contracts for the property / tenant.
#: Everything that is not terminal is blocking: the SRS says the lock lifts only when the
#: workflow is "marked COMPLETE", and the other terminal states mean "no exit in flight".
BLOCKING_STATES: frozenset[ExitWorkflowState] = frozenset(
    set(ExitWorkflowState) - TERMINAL_STATES
)


class ExitReason(StrEnum):
    """SRS T13 step 3: 'reason entry'."""

    END_OF_TERM = "END_OF_TERM"
    EARLY_TERMINATION = "EARLY_TERMINATION"
    RELOCATION = "RELOCATION"
    JOB_CHANGE = "JOB_CHANGE"
    PROPERTY_PURCHASE = "PROPERTY_PURCHASE"
    FINANCIAL_HARDSHIP = "FINANCIAL_HARDSHIP"
    LANDLORD_REQUEST = "LANDLORD_REQUEST"
    PROPERTY_CONDITION = "PROPERTY_CONDITION"
    OTHER = "OTHER"


#: Reasons for which the tenant-side minimum notice period does not apply.
NOTICE_EXEMPT_REASONS: frozenset[ExitReason] = frozenset(
    {ExitReason.LANDLORD_REQUEST, ExitReason.PROPERTY_CONDITION}
)


class ActorRole(StrEnum):
    TENANT = "TENANT"
    OWNER = "OWNER"
    INSPECTION_AGENCY = "INSPECTION_AGENCY"
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"


class DocumentType(StrEnum):
    # Tenant-supplied at initiation (T13 step 4)
    EMIRATES_ID = "EMIRATES_ID"
    PASSPORT_COPY = "PASSPORT_COPY"
    VISA_COPY = "VISA_COPY"
    TENANCY_CONTRACT = "TENANCY_CONTRACT"
    DEWA_CLEARANCE = "DEWA_CLEARANCE"
    CHILLER_CLEARANCE = "CHILLER_CLEARANCE"
    TELECOM_CLEARANCE = "TELECOM_CLEARANCE"
    KEY_HANDOVER_ACKNOWLEDGEMENT = "KEY_HANDOVER_ACKNOWLEDGEMENT"
    BANK_ACCOUNT_PROOF = "BANK_ACCOUNT_PROOF"
    # Agency-supplied
    INSPECTION_REPORT = "INSPECTION_REPORT"
    DAMAGE_PHOTO = "DAMAGE_PHOTO"
    # System-generated
    EXIT_NOC = "EXIT_NOC"
    OTHER = "OTHER"


#: Document types only the inspection agency may attach.
AGENCY_DOCUMENT_TYPES: frozenset[DocumentType] = frozenset(
    {DocumentType.INSPECTION_REPORT, DocumentType.DAMAGE_PHOTO}
)

#: Document types the system generates; no client may upload these.
SYSTEM_DOCUMENT_TYPES: frozenset[DocumentType] = frozenset({DocumentType.EXIT_NOC})


class ScanStatus(StrEnum):
    """Anti-malware scan state for uploaded documents."""

    PENDING = "PENDING"
    CLEAN = "CLEAN"
    INFECTED = "INFECTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class InspectionStatus(StrEnum):
    REQUESTED = "REQUESTED"
    SLOTS_PROPOSED = "SLOTS_PROPOSED"
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class PropertyCondition(StrEnum):
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    FAIR = "FAIR"
    POOR = "POOR"


class DamageSeverity(StrEnum):
    NONE = "NONE"
    MINOR = "MINOR"
    MODERATE = "MODERATE"
    MAJOR = "MAJOR"


class DeductionCategory(StrEnum):
    DAMAGE = "DAMAGE"
    CLEANING = "CLEANING"
    MISSING_ITEMS = "MISSING_ITEMS"
    UNPAID_RENT = "UNPAID_RENT"
    UNPAID_UTILITIES = "UNPAID_UTILITIES"
    MAINTENANCE = "MAINTENANCE"
    ADMINISTRATIVE = "ADMINISTRATIVE"
    OTHER = "OTHER"


class DisputeStatus(StrEnum):
    NONE = "NONE"
    RAISED = "RAISED"
    UPHELD = "UPHELD"  # tenant won; charge removed or reduced
    REJECTED = "REJECTED"  # owner's assessment stands


class SettlementStatus(StrEnum):
    DRAFT = "DRAFT"  # computed, still mutable during damage review
    PENDING_APPROVAL = "PENDING_APPROVAL"  # finalised, awaiting owner 'Pay Deposit'
    PROCESSING = "PROCESSING"  # payout submitted to the provider
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PayoutMethod(StrEnum):
    BANK_TRANSFER = "BANK_TRANSFER"
    CHEQUE = "CHEQUE"
    WALLET_CREDIT = "WALLET_CREDIT"
    OFFSET_ONLY = "OFFSET_ONLY"  # nothing to pay out (net refund is zero)


class NocStatus(StrEnum):
    ISSUED = "ISSUED"
    REVOKED = "REVOKED"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"
