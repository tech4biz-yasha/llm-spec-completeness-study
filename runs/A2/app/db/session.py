"""Async engine / session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _connect_args(settings: Settings) -> dict[str, object]:
    # asyncpg has no DSN-level statement_timeout; set it per connection instead so a
    # pathological query can never eat a worker slot (SRS §5.1: p95 < 200ms).
    server_settings = {
        "application_name": settings.app_name,
        "statement_timeout": str(settings.db_statement_timeout_ms),
        "idle_in_transaction_session_timeout": "30000",
        "timezone": "UTC",
    }
    return {"server_settings": server_settings}


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    kwargs: dict[str, object] = {
        "echo": settings.db_echo,
        "future": True,
        "pool_pre_ping": True,
        "connect_args": _connect_args(settings),
    }
    if settings.environment == "test":
        # A shared pool across event loops is a common source of flaky async tests.
        kwargs["poolclass"] = NullPool
    else:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_recycle=settings.db_pool_recycle_seconds,
        )
    return create_async_engine(settings.database_url, **kwargs)  # type: ignore[arg-type]


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
            class_=AsyncSession,
        )
    return _sessionmaker


def configure(engine: AsyncEngine) -> None:
    """Install a pre-built engine (used by tests and by the worker entrypoints)."""
    global _engine, _sessionmaker
    _engine = engine
    _sessionmaker = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False, class_=AsyncSession
    )


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for background workers and scripts.

    Commits on success, rolls back on any exception, always closes.
    """
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
