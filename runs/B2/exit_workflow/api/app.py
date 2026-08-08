"""FastAPI application factory and error handling."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..errors import ExitWorkflowError, ForbiddenTransition, SpecUnresolved
from .deps import Container
from .routes import router
from .schemas import ErrorResponse

logger = logging.getLogger(__name__)


def create_app(container: Container) -> FastAPI:
    app = FastAPI(
        title="Meridian — tenant exit workflow",
        version="1.0.0",
        description=(
            "Tenant exit workflow: initiation through completion, including deposit "
            "settlement and NOC issuance. Implements rules.yaml EXIT-01..EXIT-10 "
            "against states.yaml and api.yaml."
        ),
    )
    app.state.container = container
    app.include_router(router)

    @app.exception_handler(SpecUnresolved)
    async def _spec_unresolved(request: Request, exc: SpecUnresolved) -> JSONResponse:
        # AGENTS.md — a branch the spec does not decide. 501, never a guess.
        # api.yaml maps only R8 to a code (SPEC_UNRESOLVED_R8).
        logger.error(
            "spec unresolved branch reached",
            extra={"item": exc.item, "path": request.url.path},
        )
        return _error_response(exc)

    @app.exception_handler(ForbiddenTransition)
    async def _forbidden_transition(
        request: Request, exc: ForbiddenTransition
    ) -> JSONResponse:
        # states.yaml#forbidden — reaching one of these means a caller tried a
        # move the spec names as illegal. Logged loudly, then reported as
        # 409 WRONG_STATE.
        logger.error(
            "forbidden state transition attempted",
            extra={
                "from": str(exc.from_state),
                "to": str(exc.to_state),
                "path": request.url.path,
            },
        )
        return _error_response(exc)

    @app.exception_handler(ExitWorkflowError)
    async def _domain_error(request: Request, exc: ExitWorkflowError) -> JSONResponse:
        return _error_response(exc)

    return app


def _error_response(exc: ExitWorkflowError) -> JSONResponse:
    body = ErrorResponse(code=exc.code, message=exc.message, details=exc.details)
    return JSONResponse(status_code=exc.http_status, content=body.model_dump(mode="json"))
