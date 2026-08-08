"""SQLAlchemy models. Importing this package registers every table on ``Base.metadata``."""

from app.models.audit import AuditAction, AuditLogEntry, OutboxEvent
from app.models.base import Base, utcnow
from app.models.catalog import (
    Contract,
    ContractStatus,
    InspectionAgency,
    Owner,
    Property,
    Tenant,
)
from app.models.inspection import (
    AssignmentStatus,
    DamageLineItem,
    DamageReport,
    DamageSeverity,
    InspectionAssignment,
    InspectionSlot,
)
from app.models.noc import ExitNOC
from app.models.sequences import NOC_NUMBER_SEQ, WORKFLOW_REFERENCE_SEQ
from app.models.settlement import (
    DepositSettlement,
    PaymentLeg,
    PaymentStatus,
    PaymentTransaction,
    SettlementStatus,
)
from app.models.workflow import (
    ActorType,
    ExitDocument,
    ExitDocumentKind,
    ExitReasonCode,
    ExitWorkflow,
    ExitWorkflowTransition,
)

__all__ = [
    "NOC_NUMBER_SEQ",
    "WORKFLOW_REFERENCE_SEQ",
    "ActorType",
    "AssignmentStatus",
    "AuditAction",
    "AuditLogEntry",
    "Base",
    "Contract",
    "ContractStatus",
    "DamageLineItem",
    "DamageReport",
    "DamageSeverity",
    "DepositSettlement",
    "ExitDocument",
    "ExitDocumentKind",
    "ExitNOC",
    "ExitReasonCode",
    "ExitWorkflow",
    "ExitWorkflowTransition",
    "InspectionAgency",
    "InspectionAssignment",
    "InspectionSlot",
    "OutboxEvent",
    "Owner",
    "PaymentLeg",
    "PaymentStatus",
    "PaymentTransaction",
    "Property",
    "SettlementStatus",
    "Tenant",
    "utcnow",
]
