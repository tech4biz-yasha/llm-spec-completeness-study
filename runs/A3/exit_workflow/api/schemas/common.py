"""Shared response primitives."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

T = TypeVar("T")

#: Money always crosses the wire as a fixed-point string, never a float.
Money = Annotated[Decimal, PlainSerializer(lambda v: f"{Decimal(v):.2f}", return_type=str)]

#: Request-side money: non-negative, at most 2 dp.
MoneyIn = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True, extra="forbid")


class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int
    returned: int


class Page(BaseModel, Generic[T]):
    items: list[T]
    meta: PageMeta


class StepView(BaseModel):
    number: int
    step: str
    label: str
    state: str
    completed_at: datetime | None = None
    detail: str | None = None


class ProblemDetail(BaseModel):
    """RFC 9457 problem document (``application/problem+json``)."""

    type: str
    title: str
    status: int
    code: str
    detail: str
    instance: str | None = None
    model_config = ConfigDict(extra="allow")


class MessageResponse(BaseModel):
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
