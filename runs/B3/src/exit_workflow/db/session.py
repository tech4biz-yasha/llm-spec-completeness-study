"""Engine and session factory.

Sessions are synchronous (psycopg2). The FastAPI routes are declared ``def``, so Starlette
runs them in a worker thread and the blocking driver never occupies the event loop.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings


def build_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        echo=settings.sql_echo,
        future=True,
        pool_pre_ping=True,
        # REPEATABLE READ would turn the SELECT ... FOR UPDATE in settlement into a
        # serialization failure rather than a wait; READ COMMITTED plus explicit row
        # locks is what edges.yaml#X-005 needs.
        isolation_level="READ COMMITTED",
    )


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def transaction(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """One unit of work. Commits on success, rolls back on any exception.

    rules.yaml#EXIT-03 and #EXIT-09 both demand "IN ONE TRANSACTION"; this is that
    boundary, and nothing inside it performs network I/O.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
