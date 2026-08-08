"""Test harness.

The acceptance tests run against a real PostgreSQL. The database name comes from
``EXIT_TEST_DATABASE_URL`` (default ``exit_workflow_b``) and is created and
migrated once per session.

Between tests the tables are truncated. That needs the append-only triggers
temporarily disabled, which is itself a small proof that they are real —
``test_audit_append_only`` shows that an ordinary caller cannot get past them.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg2
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from exit_workflow.app import create_app
from exit_workflow.api.security import HeaderPrincipalResolver
from exit_workflow.config import Settings
from exit_workflow.db.base import create_engine
from exit_workflow.db.migrate import migrate
from exit_workflow.domain.clock import DUBAI, FixedClock
from exit_workflow.domain.enums import ContractStatus, PaymentStatus
from exit_workflow.domain.money import to_minor
from exit_workflow.domain.reasons import ConfiguredExitReasons
from exit_workflow.storage.noc import FilesystemNocStorage
from tests.fakes import FakePaymentGateway, RecordingEventPublisher

DEFAULT_TEST_DB_URL = "postgresql+asyncpg://localhost/exit_workflow_b"

#: Placeholder exit reasons for tests ONLY.
#:
#: The real reference list is unpublished (blockers.md#B-1) and the module ships
#: with none. These names are deliberately not plausible business values, so
#: that nobody can mistake them for the missing reference data and copy them
#: into a deployment.
FIXTURE_REASONS = ("FIXTURE_REASON_A", "FIXTURE_REASON_B")

#: Tables truncated between tests, children first.
_TABLES = (
    "exit_workflow_audit",
    "noc_documents",
    "event_outbox",
    "admin_tasks",
    "exit_workflows",
    "payments",
    "contracts",
    "properties",
)
_APPEND_ONLY_TABLES = ("exit_workflow_audit", "noc_documents")


def _test_database_url() -> str:
    return os.environ.get("EXIT_TEST_DATABASE_URL", DEFAULT_TEST_DB_URL)


def _admin_dsn(url: str) -> tuple[str, str]:
    """Split an async SQLAlchemy URL into a psycopg2 DSN for ``postgres`` and a db name."""
    without_driver = url.replace("postgresql+asyncpg://", "postgresql://")
    database = without_driver.rsplit("/", 1)[1]
    admin = without_driver.rsplit("/", 1)[0] + "/postgres"
    return admin, database


@pytest.fixture(scope="session")
def database_url() -> str:
    """Create and migrate the test database once per session."""
    url = _test_database_url()
    admin_dsn, database = _admin_dsn(url)

    try:
        connection = psycopg2.connect(admin_dsn)
    except psycopg2.OperationalError as exc:  # pragma: no cover - environment issue
        pytest.skip(f"PostgreSQL is not reachable at {admin_dsn}: {exc}")

    # CREATE DATABASE cannot run inside a transaction block, so no context
    # manager around the connection here — psycopg2 opens one implicitly.
    connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
            if cursor.fetchone() is None:
                cursor.execute(f'CREATE DATABASE "{database}"')
    finally:
        connection.close()

    asyncio.run(migrate(Settings(database_url=url)))
    return url


@pytest.fixture
def settings(database_url: str, tmp_path) -> Settings:
    return Settings(
        environment="test",
        database_url=database_url,
        kafka_enabled=False,
        noc_storage_backend="filesystem",
        noc_filesystem_root=str(tmp_path / "noc"),
        noc_bucket="meridian-noc-uae-test",
        noc_region="me-central-1",
        exit_reason_codes=list(FIXTURE_REASONS),
        notification_backoff_base_seconds=0.0,
        notification_backoff_factor=1.0,
        notification_backoff_max_seconds=0.0,
    )


@pytest_asyncio.fixture
async def engine(settings: Settings) -> AsyncIterator:
    engine = create_engine(settings)
    async with engine.begin() as connection:
        for table in _APPEND_ONLY_TABLES:
            await connection.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
        await connection.execute(
            text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
        )
        for table in _APPEND_ONLY_TABLES:
            await connection.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
        await connection.execute(text("ALTER SEQUENCE exit_workflow_number_seq RESTART WITH 1"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def clock() -> FixedClock:
    """Fixed at 2026-08-08 10:00 Asia/Dubai."""
    return FixedClock(datetime(2026, 8, 8, 6, 0, tzinfo=UTC))


@pytest.fixture
def today_dubai(clock: FixedClock) -> date:
    return clock.now_utc().astimezone(DUBAI).date()


@pytest.fixture
def gateway() -> FakePaymentGateway:
    return FakePaymentGateway(PaymentStatus.SUCCEEDED)


@pytest.fixture
def publisher() -> RecordingEventPublisher:
    return RecordingEventPublisher()


@pytest.fixture
def noc_storage(settings: Settings) -> FilesystemNocStorage:
    return FilesystemNocStorage(
        settings.noc_filesystem_root, settings.noc_bucket, settings.noc_region
    )


@pytest.fixture
def app(settings, session_factory, publisher, gateway, noc_storage, clock):
    return create_app(
        settings=settings,
        session_factory=session_factory,
        principal_resolver=HeaderPrincipalResolver(allow=True),
        publisher=publisher,
        payment_gateway=gateway,
        noc_storage=noc_storage,
        exit_reasons=ConfiguredExitReasons(settings.exit_reason_codes),
        clock=clock,
    )


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://exit-workflow.test") as client:
        yield client


# --- Domain fixtures ---------------------------------------------------------


class Tenancy:
    """A property, a contract and the three parties to it."""

    def __init__(self, deposit: Decimal = Decimal("12000.00")) -> None:
        self.property_id = uuid.uuid4()
        self.contract_id = uuid.uuid4()
        self.tenant_id = uuid.uuid4()
        self.owner_id = uuid.uuid4()
        self.agency_id = uuid.uuid4()
        self.deposit = deposit

    def headers(self, role: str, actor_id: uuid.UUID | str | None = None) -> dict[str, str]:
        default = {
            "tenant": self.tenant_id,
            "owner": self.owner_id,
            "inspection_agency": self.agency_id,
            "system": "system",
        }[role]
        return {"X-Actor-Id": str(actor_id or default), "X-Actor-Role": role}


@pytest_asyncio.fixture
async def tenancy(session_factory) -> Tenancy:
    """Insert an ACTIVE contract on an unlocked property."""
    fixture = Tenancy()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO properties (id, owner_id, exit_lock) "
                    "VALUES (:id, :owner_id, false)"
                ),
                {"id": fixture.property_id, "owner_id": fixture.owner_id},
            )
            await session.execute(
                text(
                    "INSERT INTO contracts "
                    "(id, property_id, tenant_id, owner_id, status, security_deposit_minor) "
                    "VALUES (:id, :property_id, :tenant_id, :owner_id, :status, :deposit)"
                ),
                {
                    "id": fixture.contract_id,
                    "property_id": fixture.property_id,
                    "tenant_id": fixture.tenant_id,
                    "owner_id": fixture.owner_id,
                    "status": str(ContractStatus.ACTIVE),
                    "deposit": to_minor(fixture.deposit),
                },
            )
    return fixture


@pytest.fixture
def initiate_body(tenancy: Tenancy, today_dubai: date) -> dict:
    return {
        "contract_id": str(tenancy.contract_id),
        "move_out_date": (today_dubai + timedelta(days=14)).isoformat(),
        "reason": FIXTURE_REASONS[0],
        "documents": ["doc-ref-1"],
    }
