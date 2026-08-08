"""Engine, session factory and declarative base."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from exit_workflow.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base for the module's tables."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings | None = None, **kwargs: Any) -> AsyncEngine:
    """Build an async engine from settings."""
    settings = settings or get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
        **kwargs,
    )


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    """Initialise the process-wide engine and session factory."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(settings)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    """Tear down the process-wide engine (application shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None  # noqa: S101 - set by init_engine
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session, committing on success and rolling back on failure.

    Services take an :class:`AsyncSession` rather than opening their own, so
    that "IN ONE TRANSACTION" steps (algorithm.md steps 4 and 13) are a single
    unit the caller cannot accidentally split.
    """
    async with session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
