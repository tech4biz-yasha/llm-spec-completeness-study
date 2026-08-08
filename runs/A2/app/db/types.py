"""Reusable column types."""

from __future__ import annotations

import enum
from typing import TypeVar

from sqlalchemy import Enum as SAEnum

E = TypeVar("E", bound=enum.Enum)


def enum_column(enum_cls: type[E], *, length: int = 48, name: str | None = None) -> SAEnum:
    """A VARCHAR-backed enum with a CHECK constraint.

    Deliberately not a native PostgreSQL ``ENUM`` type: adding a value to a native enum
    takes a lock and cannot be done inside a transaction on older servers, whereas a
    CHECK constraint is swapped with a fast ``NOT VALID`` + ``VALIDATE`` pair. The
    stored representation is the member *value*, which is our published wire format.

    ``name`` feeds the generated constraint name. It defaults to the enum class, which
    is unique enough for a table that uses each enum once; pass the column name where a
    table has two columns of the same enum (constraint names must be unique per table).
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [m.value for m in cls],  # type: ignore[var-annotated]
        validate_strings=True,
        create_constraint=True,
        name=name or _constraint_name(enum_cls),
    )


def _constraint_name(enum_cls: type[enum.Enum]) -> str:
    # Snake-case the class name: ExitWorkflowState -> exit_workflow_state
    out: list[str] = []
    for i, ch in enumerate(enum_cls.__name__):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)
