"""Shared schema primitives."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
)

from app.core.money import MAX_AMOUNT, quantize


def _to_decimal(value: Any) -> Any:
    if isinstance(value, float):
        # A JSON float has already lost precision; reject rather than launder it.
        raise ValueError(
            "monetary amounts must be sent as a string or an integer, not a float"
        )
    if isinstance(value, (str, int)):
        return Decimal(value)
    return value


#: Money on the wire is always a 2dp string ("1500.00"): JSON numbers are IEEE-754
#: doubles, and a deposit that round-trips through one is no longer exact.
Money = Annotated[
    Decimal,
    BeforeValidator(_to_decimal),
    Field(ge=Decimal("0"), le=MAX_AMOUNT),
    PlainSerializer(lambda v: f"{quantize(v):.2f}", return_type=str, when_used="json"),
]

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]


class ApiModel(BaseModel):
    """Base for response models read off ORM objects."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class CommandModel(BaseModel):
    """Base for request bodies: unknown fields are rejected, not silently dropped."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class AcceptedResponse(BaseModel):
    """Returned by asynchronous commands (payout initiation)."""

    status: str
    message: str
    workflow_id: str
