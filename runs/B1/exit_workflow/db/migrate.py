"""Schema migration runner.

The SQL under ``migrations/`` is the source of truth for the deployed schema;
``exit_workflow.db.models`` mirrors it for the ORM, and
``tests/test_schema_drift.py`` fails if the two diverge.

The scripts are executed through asyncpg's simple query protocol so that a file
containing several statements and its own ``BEGIN``/``COMMIT`` runs as written.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from exit_workflow.config import Settings
from exit_workflow.db.base import create_engine

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def migration_files() -> list[Path]:
    """Return the migration scripts in lexicographic (i.e. numeric) order."""
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


async def apply_migrations(engine: AsyncEngine) -> list[str]:
    """Apply every migration script. Scripts are idempotent."""
    applied: list[str] = []
    async with engine.connect() as connection:
        raw = await connection.get_raw_connection()
        driver_connection = raw.driver_connection  # asyncpg.Connection
        for path in migration_files():
            logger.info("applying migration %s", path.name)
            await driver_connection.execute(path.read_text(encoding="utf-8"))
            applied.append(path.name)
    return applied


async def migrate(settings: Settings | None = None) -> list[str]:
    """Apply migrations using a dedicated autocommit engine."""
    engine = create_engine(settings, isolation_level="AUTOCOMMIT")
    try:
        return await apply_migrations(engine)
    finally:
        await engine.dispose()


if __name__ == "__main__":  # pragma: no cover - operational entry point
    import asyncio

    logging.basicConfig(level=logging.INFO)
    for name in asyncio.run(migrate()):
        print(f"applied {name}")
