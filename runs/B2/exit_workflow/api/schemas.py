"""Request/response models — api.yaml.

Pydantic v2. Money crosses the wire as a JSON string in major units (AED, 2 dp)
and is parsed straight into Decimal; float never appears (AGENTS.md).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from ..domain.states import State
from ..errors import ErrorCode
from ..money import CURRENCY, from_minor

MoneyStr = Annotated[Decimal, Field(ge=Decimal("0"), max_digits=14, decimal_places=2)]


class Document(BaseModel):
    """An initiation document. rules.yaml#EXIT-02 requires at least one; the
    document schema itself is not specified, so the payload is carried as given
    apart from the reference needed to fetch it."""

    model_config = ConfigDict(extra="allow")

    document_id: str = Field(min_length=1, max_length=200)
    type: str | None = Field(default=None, max_length=100)


class Photo(BaseModel):
    """An inspection photo. rules.yaml#EXIT-06 requires photos with the report."""

    model_config = ConfigDict(extra="allow")

    photo_id: str = Field(min_length=1, max_length=200)
    url: str | None = None


class InitiateExitRequest(BaseModel):
    """api.yaml POST /exit-workflows request: {contract_id, move_out_date, reason, documents[]}."""

    model_config = ConfigDict(extra="forbid")

    contract_id: uuid.UUID
    # edges.yaml#X-007 — a calendar day in Asia/Dubai, not a timestamp.
    move_out_date: date
    reason: str = Field(min_length=1, max_length=200)
    # rules.yaml#EXIT-02 — at least one document. Enforced again in the domain
    # layer so a non-HTTP caller cannot bypass it.
    documents: list[Document] = Field(min_length=1)


class InitiateExitResponse(BaseModel):
    """api.yaml POST /exit-workflows 201: {workflow_id, status}.

    blockers.md#B-007: api.yaml shows ``status: INITIATED`` while algorithm.md
    step 4 moves the workflow to DOCS_SUBMITTED inside the same transaction. The
    persisted status is returned, because reporting a state the row does not
    hold would break any client that polls it.
    """

    workflow_id: str
    status: State


class ScheduleInspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # api.yaml declares no body for this endpoint. The date is optional and
    # recorded for the audit trail only; rules.yaml#EXIT-05 measures its window
    # from move_out_date, not from this value.
    scheduled_for: date | None = None


class InspectionReportRequest(BaseModel):
    """api.yaml POST /inspection-report request: {damage_amount, photos[]}."""

    model_config = ConfigDict(extra="forbid")

    damage_amount: MoneyStr
    # rules.yaml#EXIT-06 — the assessment is entered "with photos".
    photos: list[Photo] = Field(min_length=1)


class WorkflowStatusResponse(BaseModel):
    workflow_id: str
    status: State


class SettlementResponse(BaseModel):
    """api.yaml POST /settle 200: {refund_amount, payment_id, status}."""

    refund_amount: Decimal
    currency: str = CURRENCY
    payment_id: uuid.UUID
    status: State

    @field_serializer("refund_amount")
    def _serialise_amount(self, value: Decimal) -> str:
        # String on the wire: AED with exactly 2 dp, never a float.
        return str(value)

    @classmethod
    def from_minor_units(
        cls, *, refund_amount_minor: int, payment_id: uuid.UUID, status: State
    ) -> "SettlementResponse":
        return cls(
            refund_amount=from_minor(refund_amount_minor),
            payment_id=payment_id,
            status=status,
        )


class ErrorResponse(BaseModel):
    """Error envelope.

    ``code`` is always a value from api.yaml#_error_codes, or null where api.yaml
    defines no code for the case (see exit_workflow/errors.py and blockers.md).
    """

    code: ErrorCode | None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
