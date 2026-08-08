"""Liveness and readiness probes."""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Response, status

from app import __version__
from app.api.deps import SessionDep

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Process is up. Deliberately does no I/O so it cannot fail on a slow dependency."""
    return {"status": "ok", "version": __version__}


@router.get("/health/ready", summary="Readiness probe")
async def readiness(session: SessionDep, response: Response) -> dict[str, object]:
    """Reports whether the database is reachable; returns 503 when it is not."""
    checks: dict[str, object] = {"database": "ok"}
    try:
        await session.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - the probe must report, not raise
        checks["database"] = f"error: {type(exc).__name__}"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "checks": checks}
    return {"status": "ok", "version": __version__, "checks": checks}
