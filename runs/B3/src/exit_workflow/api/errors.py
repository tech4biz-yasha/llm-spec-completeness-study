"""Exception handlers.

Every handler emits the api.yaml code verbatim, or null where api.yaml declares none.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..errors import ExitWorkflowError, SpecUnresolved

logger = logging.getLogger(__name__)

#: api.yaml gives 501 SPEC_UNRESOLVED_R8 for the damage-exceeds-deposit branch of settle.
SPEC_UNRESOLVED_STATUS = 501


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ExitWorkflowError)
    def _handle_workflow_error(_: Request, exc: ExitWorkflowError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(SpecUnresolved)
    def _handle_spec_unresolved(_: Request, exc: SpecUnresolved) -> JSONResponse:
        # AGENTS.md — a blocked branch stops here rather than guessing an answer.
        logger.error("blocked branch reached: %s", exc)
        return JSONResponse(status_code=SPEC_UNRESOLVED_STATUS, content=exc.to_payload())
