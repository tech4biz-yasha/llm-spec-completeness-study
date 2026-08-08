"""Standalone worker entrypoint: ``python -m app.workers.run``."""

from __future__ import annotations

import asyncio
import signal

from app.container import get_ports
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine
from app.workers.outbox_dispatcher import OutboxDispatcher
from app.workers.reconciler import Reconciler

log = get_logger("workers")


async def main() -> None:
    settings = get_settings()
    configure_logging(debug=settings.debug, json_output=settings.environment != "local")
    ports = get_ports()

    dispatcher = OutboxDispatcher(ports.events, settings=settings, clock=ports.clock)
    reconciler = Reconciler(settings=settings, ports=ports, clock=ports.clock)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: (dispatcher.stop(), reconciler.stop()))

    log.info("workers.starting")
    try:
        await asyncio.gather(dispatcher.run_forever(), reconciler.run_forever())
    finally:
        await dispose_engine()
        log.info("workers.stopped")


if __name__ == "__main__":
    asyncio.run(main())
