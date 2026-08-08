"""Database engine, session lifecycle and unit-of-work dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _connect_args(settings: Settings) -> dict[str, Any]:
    server_settings: dict[str, str] = {"application_name": "exit-workflow"}
    if settings.db_statement_timeout_ms > 0:
        # Bounds any pathological query so a stuck statement cannot exhaust the pool.
        server_settings["statement_timeout"] = str(settings.db_statement_timeout_ms)
    return {"server_settings": server_settings}


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=True,
        connect_args=_connect_args(settings),
    )


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


def configure(engine: AsyncEngine) -> None:
    """Bind the module to a caller-supplied engine (used by tests and by the app lifespan)."""
    global _engine, _sessionmaker
    _engine = engine
    _sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transaction per request.

    The transaction commits only if the handler returns cleanly, so a failed request can
    never leave a half-applied state change or an orphaned audit row behind.
    """
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
