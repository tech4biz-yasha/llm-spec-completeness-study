"""Declarative base, naming conventions and shared column mixins."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import TIMESTAMP, Enum, MetaData, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from exit_workflow.core.clock import utcnow

#: Deterministic constraint names so Alembic autogenerate produces stable diffs.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Money is always NUMERIC(14, 2) — never float, never int fils.
MoneyType = Numeric(14, 2)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        uuid.UUID: PGUUID(as_uuid=True),
        datetime: TIMESTAMP(timezone=True),
        Decimal: MoneyType,
        dict[str, Any]: JSONB,
        list[str]: JSONB,
        str: String(255),
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} id={getattr(self, 'id', None)}>"


def pg_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Native PostgreSQL enum carrying the enum's *values*."""

    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        create_constraint=False,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


class UUIDPrimaryKeyMixin:
    """Client-side UUID4 primary key.

    Assigned in ``__init__`` rather than at flush time so services can put the
    id into audit rows and event payloads before the INSERT — and so ids never
    leak insertion order.
    """

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    def __init__(self, **kw: Any) -> None:
        kw.setdefault("id", uuid.uuid4())
        super().__init__(**kw)


class TimestampMixin:
    """Row timestamps.

    Defaults are evaluated in Python (from the application clock) so the value
    is known without a round-trip: a SQL-side ``onupdate`` would expire the
    attribute after every flush and force a lazy refresh, which is illegal in
    async context. The server defaults remain for rows written outside the ORM.
    """

    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), default=utcnow, onupdate=utcnow, nullable=False
    )


#: Convenience aliases used across the model modules.
LongText = Text
