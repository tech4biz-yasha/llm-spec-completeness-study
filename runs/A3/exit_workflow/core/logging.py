"""Structured JSON logging with request correlation."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
principal_ctx: ContextVar[str | None] = ContextVar("principal", default=None)


def _inject_context(_logger, _name, event_dict):  # noqa: ANN001, ANN202
    if rid := request_id_ctx.get():
        event_dict.setdefault("request_id", rid)
    if principal := principal_ctx.get():
        event_dict.setdefault("principal", principal)
    return event_dict


def configure_logging(*, debug: bool = False) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.DEBUG if debug else logging.INFO,
    )
    logging.getLogger("uvicorn.access").handlers = []
    renderer = (
        structlog.dev.ConsoleRenderer() if debug else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _inject_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if debug else logging.INFO
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "exit_workflow") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
