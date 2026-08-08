"""Helpers shared by the v1 routers."""

from __future__ import annotations

import uuid

WORKFLOW_PATH_DESCRIPTION = "Workflow id (UUID) or human reference such as EXW-2026-7K3M9Q"


def workflow_identifier(value: str) -> uuid.UUID | str:
    """Accept either the UUID or the tenant-facing reference in the path."""

    try:
        return uuid.UUID(value)
    except ValueError:
        return value
