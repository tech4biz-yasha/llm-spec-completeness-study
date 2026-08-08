"""Async engine / session management.

Transaction boundaries in this module are explicit and load-bearing:
rules.yaml#EXIT-03 and #EXIT-09 both require "IN ONE TRANSACTION". Services take
an ``AsyncSession`` and never commit on their own except at the boundary the
spec names.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        future=True,
        connect_args={
            "server_settings": {
                "application_name": "exit_workflow",
                "statement_timeout": str(settings.db_statement_timeout_ms),
            }
        }
        if settings.database_url.startswith("postgresql+asyncpg")
        else {},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
        class_=AsyncSession,
    )


@asynccontextmanager
async def transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One session, one transaction, commit on success and roll back on any error."""
    async with session_factory() as session:
        async with session.begin():
            yield session
