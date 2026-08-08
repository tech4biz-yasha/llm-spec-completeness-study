"""Derivation of the T13 ten-step progress view.

Step state is *derived*, never stored as an independent field, so it can never
drift from the workflow's actual state. Every input is a column on the
workflow row itself, which keeps ``GET`` responses to a single-row read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from exit_workflow.domain.enums import (
    STEP_NUMBERS,
    ExitStep,
    ExitWorkflowStatus,
    StepState,
)

if TYPE_CHECKING:  # pragma: no cover
    from exit_workflow.models.workflow import ExitWorkflow


STEP_LABELS: dict[ExitStep, str] = {
    ExitStep.MOVE_OUT_DATE: "Move-out date",
    ExitStep.REASON_ENTRY: "Reason for exit",
    ExitStep.DOCUMENT_UPLOAD: "Supporting documents",
    ExitStep.WORKFLOW_ID_GENERATION: "Workflow ID generated",
    ExitStep.OWNER_NOTIFICATION: "Owner notified",
    ExitStep.INSPECTION_SCHEDULING: "Inspection scheduled",
    ExitStep.DAMAGE_REVIEW: "Damage review",
    ExitStep.DEPOSIT_REFUND: "Deposit refund",
    ExitStep.NOC_DOWNLOAD: "Exit NOC",
    ExitStep.WORKFLOW_COMPLETION: "Workflow complete",
}


@dataclass(frozen=True, slots=True)
class StepProgress:
    step: ExitStep
    number: int
    label: str
    state: StepState
    completed_at: datetime | None = None
    detail: str | None = None


def _state(complete_at: datetime | None, in_progress: bool) -> StepState:
    if complete_at is not None:
        return StepState.COMPLETE
    return StepState.IN_PROGRESS if in_progress else StepState.PENDING


def compute_steps(wf: ExitWorkflow) -> list[StepProgress]:
    status = wf.status
    halted = status in (ExitWorkflowStatus.CANCELLED, ExitWorkflowStatus.REJECTED)

    # Steps 1, 2 and 4 are satisfied by the act of initiating: the request
    # cannot exist without a move-out date, a reason and a reference.
    raw: list[tuple[ExitStep, datetime | None, bool, str | None]] = [
        (ExitStep.MOVE_OUT_DATE, wf.initiated_at, False, wf.move_out_date.isoformat()),
        (ExitStep.REASON_ENTRY, wf.initiated_at, False, wf.reason.value),
        (
            ExitStep.DOCUMENT_UPLOAD,
            wf.initiated_at if (wf.documents_uploaded_count or wf.submitted_at) else None,
            wf.submitted_at is None,
            f"{wf.documents_uploaded_count} document(s) attached",
        ),
        (ExitStep.WORKFLOW_ID_GENERATION, wf.initiated_at, False, wf.reference),
        (
            ExitStep.OWNER_NOTIFICATION,
            wf.submitted_at,
            status is ExitWorkflowStatus.INITIATED,
            None,
        ),
        (
            ExitStep.INSPECTION_SCHEDULING,
            wf.inspection_scheduled_at,
            status
            in (
                ExitWorkflowStatus.OWNER_APPROVED,
                ExitWorkflowStatus.INSPECTION_REQUESTED,
            ),
            None,
        ),
        (
            ExitStep.DAMAGE_REVIEW,
            wf.damage_review_completed_at,
            status
            in (
                ExitWorkflowStatus.INSPECTION_SCHEDULED,
                ExitWorkflowStatus.INSPECTION_COMPLETED,
                ExitWorkflowStatus.DAMAGE_REVIEW,
            ),
            None,
        ),
        (
            ExitStep.DEPOSIT_REFUND,
            wf.settlement_completed_at,
            status is ExitWorkflowStatus.SETTLEMENT_PENDING,
            None,
        ),
        (
            ExitStep.NOC_DOWNLOAD,
            wf.noc_first_downloaded_at,
            wf.noc_issued_at is not None,
            "Available for download" if wf.noc_issued_at and not wf.noc_first_downloaded_at else None,
        ),
        (ExitStep.WORKFLOW_COMPLETION, wf.completed_at, False, None),
    ]

    steps: list[StepProgress] = []
    for step, completed_at, in_progress, detail in raw:
        state = _state(completed_at, in_progress)
        if halted and state is not StepState.COMPLETE:
            state = StepState.BLOCKED
            detail = f"Workflow {status.value.lower()}"
        steps.append(
            StepProgress(
                step=step,
                number=STEP_NUMBERS[step],
                label=STEP_LABELS[step],
                state=state,
                completed_at=completed_at,
                detail=detail,
            )
        )
    return steps


def current_step(wf: ExitWorkflow) -> StepProgress:
    """The step a UI should focus on: first non-complete, else the last."""

    steps = compute_steps(wf)
    for progress in steps:
        if progress.state is not StepState.COMPLETE:
            return progress
    return steps[-1]


def completed_step_count(wf: ExitWorkflow) -> int:
    return sum(1 for s in compute_steps(wf) if s.state is StepState.COMPLETE)
