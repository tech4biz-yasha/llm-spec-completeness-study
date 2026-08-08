"""Background loops the specification requires.

* Owner notification retries — rules.yaml#EXIT-04 (5 attempts, exponential
  backoff, then dead-letter + admin alert). The first attempt happens inline
  after the initiation commit; this loop owns everything after that.
* Stall sweep — rules.yaml#EXIT-05 (30 days past move_out_date -> STALLED +
  admin task).

Both are idempotent and safe to run in more than one process.
"""

from __future__ import annotations

import asyncio
import logging

from .api.deps import Container

logger = logging.getLogger(__name__)

#: The retry loop only needs to wake often enough to honour the shortest backoff.
OUTBOX_POLL_SECONDS = 5.0
#: rules.yaml#EXIT-05 works on calendar days; hourly is far finer than required.
STALL_SWEEP_SECONDS = 3600.0


async def run_outbox_dispatcher(
    container: Container, *, poll_seconds: float = OUTBOX_POLL_SECONDS
) -> None:
    while True:
        try:
            published = await container.notifications.dispatch_pending()
            if published:
                logger.info("owner notifications published", extra={"count": published})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must survive a bad iteration
            logger.exception("outbox dispatch sweep failed")
        await asyncio.sleep(poll_seconds)


async def run_stall_sweeper(
    container: Container, *, poll_seconds: float = STALL_SWEEP_SECONDS
) -> None:
    while True:
        try:
            stalled = await container.stall.sweep()
            if stalled:
                logger.warning("workflows stalled", extra={"workflow_ids": stalled})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("stall sweep failed")
        await asyncio.sleep(poll_seconds)
