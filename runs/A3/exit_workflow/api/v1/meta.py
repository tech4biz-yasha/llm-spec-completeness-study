"""Introspection endpoints: the state machine and the step catalogue."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from exit_workflow.domain.enums import STEP_NUMBERS, ExitReason, ExitWorkflowStatus
from exit_workflow.domain.state_machine import describe_machine
from exit_workflow.domain.steps import STEP_LABELS

router = APIRouter(prefix="/meta", tags=["meta"])


@router.get(
    "/state-machine",
    summary="The exit workflow transition table",
    description="Lets clients render valid actions without hard-coding the lifecycle.",
)
async def get_state_machine() -> dict[str, Any]:
    return {
        "statuses": [s.value for s in ExitWorkflowStatus],
        "transitions": describe_machine(),
    }


@router.get("/steps", summary="The ten T13 steps")
async def get_steps_catalogue() -> list[dict[str, Any]]:
    return [
        {"number": number, "step": step.value, "label": STEP_LABELS[step]}
        for step, number in STEP_NUMBERS.items()
    ]


@router.get("/exit-reasons", summary="Accepted exit reasons")
async def get_exit_reasons() -> list[str]:
    return [r.value for r in ExitReason]
