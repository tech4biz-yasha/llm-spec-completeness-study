"""SQLAlchemy models.

Importing this package registers every mapper with :class:`app.db.base.Base`, which is
what Alembic's ``target_metadata`` and ``Base.metadata.create_all`` rely on.
"""

from app.models.audit import AuditLogEntry
from app.models.document import ExitDocument
from app.models.exit_workflow import ExitWorkflow, StateTransition
from app.models.idempotency import IdempotencyRecord
from app.models.inspection import DamageItem, Inspection, InspectionSlot
from app.models.noc import ExitNoc
from app.models.outbox import OutboxMessage
from app.models.settlement import Settlement, SettlementDeduction

__all__ = [
    "AuditLogEntry",
    "DamageItem",
    "ExitDocument",
    "ExitNoc",
    "ExitWorkflow",
    "IdempotencyRecord",
    "Inspection",
    "InspectionSlot",
    "OutboxMessage",
    "Settlement",
    "SettlementDeduction",
    "StateTransition",
]
