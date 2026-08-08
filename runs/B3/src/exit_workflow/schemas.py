"""Request and response models. Shapes come from api.yaml.

Business validation is NOT done here. ``move_out_date`` in the past, an unknown reason and
an empty document list must be reported with the api.yaml codes MOVE_OUT_DATE_IN_PAST,
REASON_INVALID and DOCUMENTS_REQUIRED — a schema-level constraint would instead produce
FastAPI's generic 422 and lose the code. So the schemas stay permissive and the service
decides.

The one thing enforced here is the money convention (AGENTS.md): AED, Decimal, 2 decimal
places, never float.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import WorkflowState
from .money import CURRENCY


def _as_decimal(value: Any) -> Any:
    """Coerce to Decimal without ever routing through binary float arithmetic."""
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, bool):  # bool is an int subclass; never a money value
        raise ValueError("amount must be a number")
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        # repr() round-trips the shortest exact decimal for the float, so "1500.75"
        # stays 1500.75 rather than 1500.7499999999999.
        candidate = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            candidate = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError("amount is not a valid decimal") from exc
    else:
        raise ValueError("amount must be a number or a decimal string")

    if not candidate.is_finite():
        raise ValueError("amount must be finite")
    if candidate < 0:
        raise ValueError("amount must not be negative")
    if -candidate.as_tuple().exponent > 2:
        raise ValueError(f"{CURRENCY} amounts carry at most 2 decimal places")
    return candidate


Money = Annotated[Decimal, Field(description=f"{CURRENCY} amount, 2 decimal places")]


class InitiateExitRequest(BaseModel):
    """api.yaml POST /exit-workflows request: {contract_id, move_out_date, reason, documents[]}."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str = Field(min_length=1, max_length=64)
    #: edges.yaml#X-007 — a calendar day in Asia/Dubai, a date and not a datetime.
    move_out_date: date
    reason: str = Field(min_length=1, max_length=64)
    #: api.yaml gives no item schema for documents, so none is imposed. The "at least
    #: one" rule (rules.yaml#EXIT-02) is enforced in the service so it can report
    #: DOCUMENTS_REQUIRED.
    documents: list[Any] = Field(default_factory=list)


class InitiateExitResponse(BaseModel):
    """api.yaml 201: {workflow_id, status}.

    api.yaml writes ``status: INITIATED`` while algorithm.md#4 has the same transaction
    end at DOCS_SUBMITTED. This returns the status the workflow actually holds, so that a
    client polling on it is never told something untrue. See blockers.md#B-6.
    """

    workflow_id: str
    status: WorkflowState


class InspectionReportRequest(BaseModel):
    """api.yaml POST /{id}/inspection-report request: {damage_amount, photos[]}."""

    model_config = ConfigDict(extra="forbid")

    damage_amount: Money
    photos: list[Any] = Field(default_factory=list)

    _coerce = field_validator("damage_amount", mode="before")(_as_decimal)


class WorkflowStatusResponse(BaseModel):
    """api.yaml ``200: ok`` for schedule-inspection, inspection-report and confirm-damage."""

    workflow_id: str
    status: WorkflowState


class SettleResponse(BaseModel):
    """api.yaml 200: {refund_amount, payment_id, status}."""

    workflow_id: str
    refund_amount: Money
    payment_id: str
    status: WorkflowState


class ErrorResponse(BaseModel):
    """api.yaml fixes the codes but not the envelope; this is the envelope.

    ``code`` is null only where api.yaml declares no code for the condition
    (authorization failure, unknown workflow, a non-R8 unresolved branch).
    """

    code: str | None
    message: str
    details: dict[str, Any] | None = None
    blocker: str | None = None
