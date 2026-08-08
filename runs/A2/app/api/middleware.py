"""HTTP middleware: request correlation, access logging, security headers."""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import actor_id_ctx, get_logger, request_id_ctx

log = get_logger("api.access")

REQUEST_ID_HEADER = "X-Request-ID"

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # The API serves JSON and file downloads only; a restrictive CSP costs nothing here
    # and blunts content-sniffing tricks against browsers that open a download inline.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds it to the log context and echoes it back."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and len(incoming) <= 64 else uuid.uuid4().hex
        request.state.request_id = request_id

        token = request_id_ctx.set(request_id)
        actor_token = actor_id_ctx.set(None)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            request_id_ctx.reset(token)
            actor_id_ctx.reset(actor_token)

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Structured access log. Query strings are dropped -- they can carry identifiers."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                request_id=getattr(request.state, "request_id", None),
                actor_id=getattr(request.state, "actor_id", None),
            )
