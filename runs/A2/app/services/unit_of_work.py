"""Unit of work with post-commit side effects.

Notifications and other outbound calls must not run inside the database transaction: a
slow SMTP hop would hold row locks, and a rolled-back transaction would still have sent
the email. Services register side effects here; the request boundary commits first and
only then runs them.

Post-commit hooks are best-effort by design. The durable trigger for anything that must
not be lost is the transactional outbox (:mod:`app.services.events`), which is written
inside the same transaction as the state change.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger(__name__)

SideEffect = Callable[[], Awaitable[None]]


class UnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._after_commit: list[tuple[str, SideEffect]] = []

    def after_commit(self, name: str, effect: SideEffect) -> None:
        self._after_commit.append((name, effect))

    async def commit(self) -> None:
        await self.session.commit()
        await self._run_side_effects()

    async def rollback(self) -> None:
        self._after_commit.clear()
        await self.session.rollback()

    async def flush(self) -> None:
        await self.session.flush()

    async def _run_side_effects(self) -> None:
        effects, self._after_commit = self._after_commit, []
        for name, effect in effects:
            try:
                await effect()
            except Exception:  # noqa: BLE001 - a failed notification must not fail the request
                log.exception("side_effect.failed", side_effect=name)

    @property
    def pending_side_effects(self) -> list[str]:
        return [name for name, _ in self._after_commit]
