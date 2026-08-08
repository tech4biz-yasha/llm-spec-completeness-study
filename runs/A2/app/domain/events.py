"""Domain events.

SRS §7 puts Kafka on the stack. Events are written to a transactional outbox in the same
database transaction as the state change, then relayed by a dispatcher -- so an event is
never emitted for a transaction that rolled back, and never lost for one that committed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

AGGREGATE_TYPE = "exit_workflow"

#: Event type constants. Consumers subscribe on these; treat as append-only.
EXIT_INITIATED = "exit_workflow.initiated"
EXIT_SUBMITTED = "exit_workflow.submitted"
EXIT_WITHDRAWN = "exit_workflow.withdrawn"
EXIT_OWNER_APPROVED = "exit_workflow.owner_approved"
EXIT_OWNER_REJECTED = "exit_workflow.owner_rejected"
EXIT_CANCELLED = "exit_workflow.cancelled"
EXIT_EXPIRED = "exit_workflow.expired"
DOCUMENT_UPLOADED = "exit_workflow.document_uploaded"
DOCUMENT_REMOVED = "exit_workflow.document_removed"
INSPECTION_REQUESTED = "exit_workflow.inspection_requested"
INSPECTION_SLOTS_PROPOSED = "exit_workflow.inspection_slots_proposed"
INSPECTION_SCHEDULED = "exit_workflow.inspection_scheduled"
INSPECTION_RESCHEDULED = "exit_workflow.inspection_rescheduled"
INSPECTION_REPORT_SUBMITTED = "exit_workflow.inspection_report_submitted"
DAMAGE_REVIEW_OPENED = "exit_workflow.damage_review_opened"
DAMAGE_ITEM_ADJUSTED = "exit_workflow.damage_item_adjusted"
DAMAGE_DISPUTE_RAISED = "exit_workflow.damage_dispute_raised"
DAMAGE_DISPUTE_RESOLVED = "exit_workflow.damage_dispute_resolved"
SETTLEMENT_FINALISED = "exit_workflow.settlement_finalised"
SETTLEMENT_PAYMENT_INITIATED = "exit_workflow.settlement_payment_initiated"
SETTLEMENT_PAYMENT_SUCCEEDED = "exit_workflow.settlement_payment_succeeded"
SETTLEMENT_PAYMENT_FAILED = "exit_workflow.settlement_payment_failed"
NOC_ISSUED = "exit_workflow.noc_issued"
NOC_DOWNLOADED = "exit_workflow.noc_downloaded"
NOC_REVOKED = "exit_workflow.noc_revoked"
WORKFLOW_COMPLETED = "exit_workflow.completed"
STATE_CHANGED = "exit_workflow.state_changed"

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """An event awaiting outbox persistence."""

    event_type: str
    workflow_id: UUID
    payload: dict[str, Any] = field(default_factory=dict)
    #: Kafka partition key. Defaults to the workflow id so a single workflow's events
    #: are strictly ordered for consumers.
    partition_key: str | None = None
    occurred_at: datetime | None = None

    def key(self) -> str:
        return self.partition_key or str(self.workflow_id)

    def envelope(self, *, occurred_at: datetime) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "event_type": self.event_type,
            "aggregate_type": AGGREGATE_TYPE,
            "aggregate_id": str(self.workflow_id),
            "occurred_at": (self.occurred_at or occurred_at).isoformat(),
            "data": self.payload,
        }
