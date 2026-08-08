"""Model -> response mapping, including the derived 10-step view."""

from __future__ import annotations

from typing import Any

from exit_workflow.api.schemas.common import StepView
from exit_workflow.api.schemas.workflow import ExitWorkflowDetail, ExitWorkflowSummary
from exit_workflow.core.security import Principal
from exit_workflow.domain.state_machine import allowed_targets_for
from exit_workflow.domain.steps import compute_steps, current_step
from exit_workflow.models.workflow import ExitWorkflow


def _step_view(progress: Any) -> StepView:
    return StepView(
        number=progress.number,
        step=progress.step.value,
        label=progress.label,
        state=progress.state.value,
        completed_at=progress.completed_at,
        detail=progress.detail,
    )


def step_views(workflow: ExitWorkflow) -> list[StepView]:
    return [_step_view(p) for p in compute_steps(workflow)]


def workflow_summary(workflow: ExitWorkflow) -> ExitWorkflowSummary:
    return ExitWorkflowSummary.model_validate(workflow)


def workflow_detail(workflow: ExitWorkflow, principal: Principal) -> ExitWorkflowDetail:
    steps = step_views(workflow)
    payload: dict[str, Any] = {
        name: getattr(workflow, name)
        for name in ExitWorkflowDetail.model_fields
        if hasattr(workflow, name)
    }
    payload["steps"] = steps
    payload["completed_step_count"] = sum(1 for s in steps if s.state == "COMPLETE")
    payload["current_step"] = _step_view(current_step(workflow))
    # What *this* caller could do next — drives button state in the clients.
    payload["allowed_transitions"] = sorted(
        allowed_targets_for(workflow.status, principal.role), key=lambda s: s.value
    )
    return ExitWorkflowDetail.model_validate(payload)
