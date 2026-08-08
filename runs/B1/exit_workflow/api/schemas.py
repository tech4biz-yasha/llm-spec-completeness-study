"""Request and response bodies, as declared in api.yaml.

Field-level validation is deliberately thin. api.yaml assigns specific error
codes to specific failures — ``MOVE_OUT_DATE_IN_PAST``, ``DOCUMENTS_REQUIRED``,
``REASON_INVALID`` — and a Pydantic constraint would reject the request first,
with FastAPI's generic 422 body and none of those codes. So these models check
shape and type only; the rules are enforced in the service, which raises the
error api.yaml names.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class InitiateExitRequest(BaseModel):
    """api.yaml POST /exit-workflows: ``{contract_id, move_out_date, reason, documents[]}``."""

    model_config = ConfigDict(extra="forbid")

    contract_id: uuid.UUID
    #: edges.yaml#X-007 — a calendar day in Asia/Dubai, not an instant.
    move_out_date: date
    reason: str = Field(max_length=64)
    #: References to documents already uploaded to the document service. The kit
    #: does not define the element shape beyond "documents[]", so this stays a
    #: list of opaque references rather than a structure invented here.
    documents: list[str] = Field(default_factory=list)


class InitiateExitResponse(BaseModel):
    """api.yaml ``201: {workflow_id, status: INITIATED}``.

    The status reported here is the one api.yaml declares. The workflow is
    persisted as DOCS_SUBMITTED, because algorithm.md step 4 moves it there
    inside the initiation transaction. The two statements conflict; the
    conflict is recorded as blockers.md#B-8 and this response follows api.yaml,
    which is the contract clients are written against.
    """

    workflow_id: str
    status: str


class InspectionReportRequest(BaseModel):
    """api.yaml POST /exit-workflows/{id}/inspection-report: ``{damage_amount, photos[]}``."""

    model_config = ConfigDict(extra="forbid")

    damage_amount: Decimal
    photos: list[str] = Field(default_factory=list)

    @field_validator("damage_amount", mode="before")
    @classmethod
    def _no_binary_float(cls, value: Any) -> Any:
        """Convert through the decimal representation of the number as written.

        A JSON number reaches Python as a float, and ``Decimal(0.1)`` is not
        ``Decimal("0.1")``. Going via ``str`` keeps the figure the inspector
        typed (AGENTS.md: never float).
        """
        if isinstance(value, float):
            try:
                return Decimal(str(value))
            except InvalidOperation:  # pragma: no cover - str(float) is always parseable
                return value
        return value


class WorkflowStateResponse(BaseModel):
    """Body for the endpoints api.yaml records only as ``200: ok``.

    api.yaml does not give these three responses a schema. Returning the
    workflow's identifier and its current state reports what the call did
    without asserting anything the kit has not decided; see blockers.md#B-11.
    """

    workflow_id: str
    status: str


class SettlementResponse(BaseModel):
    """api.yaml ``200: {refund_amount, payment_id, status}``."""

    refund_amount: Decimal
    payment_id: str
    status: str

    @field_serializer("refund_amount")
    def _serialise_amount(self, value: Decimal) -> str:
        """Emit money as a JSON string.

        A JSON number would be read back as a binary float by most clients,
        which is exactly what AGENTS.md rules out for money.
        """
        return f"{value:.2f}"


class ErrorBody(BaseModel):
    """The ``error`` object carried by every failure response."""

    code: str | None
    message: str
    details: dict[str, Any] | None = None
    blocker: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
