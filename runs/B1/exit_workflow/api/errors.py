"""Exception handlers.

Every failure leaves this module as ``{"error": {"code", "message", ...}}`` with
``code`` taken from api.yaml, or ``null`` where api.yaml defines no code for the
branch (the blocker ID travels instead — see blockers.md).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from exit_workflow.domain.errors import ExitWorkflowError, ForbiddenTransition, SpecUnresolved

logger = logging.getLogger(__name__)


async def exit_workflow_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ExitWorkflowError)  # noqa: S101 - registered for this type only

    if isinstance(exc, ForbiddenTransition):
        # AGENTS.md: a forbidden transition raises, never silently no-ops. It
        # also means something tried to skip a mandatory step, which is worth
        # waking somebody for.
        logger.critical(
            "forbidden transition attempted on %s %s: %s",
            request.method,
            request.url.path,
            exc.message,
            extra={"details": exc.details},
        )
    elif isinstance(exc, SpecUnresolved):
        logger.error(
            "request blocked on unresolved spec item %s at %s %s",
            exc.blocker_id,
            request.method,
            request.url.path,
        )
    elif exc.http_status >= 500:
        logger.error("%s %s failed: %s", request.method, request.url.path, exc.message)

    return JSONResponse(status_code=exc.http_status, content=exc.to_payload())


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ExitWorkflowError, exit_workflow_error_handler)
