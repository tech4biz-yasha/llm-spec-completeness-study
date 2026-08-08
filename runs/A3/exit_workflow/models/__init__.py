"""SQLAlchemy models. Importing this package registers every table."""

from exit_workflow.models.audit import AuditEvent, WorkflowTransition
from exit_workflow.models.base import Base
from exit_workflow.models.document import Document
from exit_workflow.models.idempotency import IdempotencyRecord
from exit_workflow.models.inspection import (
    DamageLineItem,
    DamageReport,
    Inspection,
    InspectionSlot,
)
from exit_workflow.models.noc import ExitNoc
from exit_workflow.models.notification import NotificationLog
from exit_workflow.models.outbox import OutboxEvent
from exit_workflow.models.settlement import PaymentTransaction, Settlement
from exit_workflow.models.workflow import ExitWorkflow

__all__ = [
    "AuditEvent",
    "Base",
    "DamageLineItem",
    "DamageReport",
    "Document",
    "ExitNoc",
    "ExitWorkflow",
    "IdempotencyRecord",
    "Inspection",
    "InspectionSlot",
    "NotificationLog",
    "OutboxEvent",
    "PaymentTransaction",
    "Settlement",
    "WorkflowTransition",
]
