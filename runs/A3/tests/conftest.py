"""Test harness.

Tests run against a real PostgreSQL database with the real Alembic migration
applied — partial unique indexes, check constraints and the append-only audit
triggers are all part of what is under test, and none of them exist in SQLite.

Set ``EXITWF_TEST_DATABASE_URL`` to point at your own database; the default is
``postgresql+asyncpg://localhost:5432/meridian_exit_test``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exit_workflow.container import AppContainer
from exit_workflow.core import db as db_module
from exit_workflow.core.config import Settings
from exit_workflow.core.security import Role, issue_token
from exit_workflow.integrations.agencies import AgencySnapshot, StaticAgencyDirectory
from exit_workflow.integrations.contracts import ContractSnapshot, StaticContractDirectory
from exit_workflow.integrations.payments import SimulatedPaymentGateway
from exit_workflow.main import create_app
from exit_workflow.models import Base
from exit_workflow.services.events import LoggingEventPublisher
from exit_workflow.services.notifications import LoggingEmailSender
from exit_workflow.services.storage import InMemoryStorage

TEST_DATABASE_URL = os.environ.get(
    "EXITWF_TEST_DATABASE_URL", "postgresql+asyncpg://localhost:5432/meridian_exit_test"
)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        environment="test",
        debug=False,
        database_url=TEST_DATABASE_URL,
        jwt_secret="test-secret-not-for-production",
        background_worker_enabled=False,
        storage_root="/tmp/exit-workflow-tests",
        min_notice_days=0,
        inspection_slot_min_lead_hours=12,
    )


@pytest.fixture(scope="session", autouse=True)
def migrated_database(settings: Settings) -> Iterator[None]:
    """Rebuild the test schema once per session using the real migration."""

    config = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(PROJECT_ROOT, "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def engine(settings: Settings, migrated_database: None):
    engine = create_async_engine(settings.database_url, poolclass=None)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker:
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(engine) -> AsyncIterator[None]:
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    async with engine.begin() as conn:
        # The audit tables are append-only for the application; the retention
        # escape hatch is what a cleanup job would use.
        await conn.execute(text("SET LOCAL exit_workflow.retention_job = 'on'"))
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def contracts() -> StaticContractDirectory:
    return StaticContractDirectory()


@pytest.fixture
def agencies() -> StaticAgencyDirectory:
    return StaticAgencyDirectory()


@pytest.fixture
def gateway() -> SimulatedPaymentGateway:
    return SimulatedPaymentGateway()


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def container(
    settings: Settings,
    contracts: StaticContractDirectory,
    agencies: StaticAgencyDirectory,
    gateway: SimulatedPaymentGateway,
    storage: InMemoryStorage,
) -> AppContainer:
    return AppContainer(
        settings=settings,
        storage=storage,
        contracts=contracts,
        agencies=agencies,
        gateway=gateway,
        publisher=LoggingEventPublisher(),
        email_sender=LoggingEmailSender(),
    )


@pytest.fixture
def app(settings: Settings, container: AppContainer, session_factory, engine):
    application = create_app(settings)
    application.state.container = container
    application.state.session_factory = session_factory
    application.state.owns_engine = False
    db_module.configure(engine)
    return application


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as http_client:
        yield http_client


# --------------------------------------------------------------------------
# Actors and fixtures for a ready-to-exit tenancy
# --------------------------------------------------------------------------
@pytest.fixture
def ids() -> dict[str, uuid.UUID]:
    return {
        "contract": uuid.uuid4(),
        "property": uuid.uuid4(),
        "tenant": uuid.uuid4(),
        "owner": uuid.uuid4(),
        "agency": uuid.uuid4(),
        "agency_user": uuid.uuid4(),
        "admin": uuid.uuid4(),
    }


@pytest.fixture
def contract(
    contracts: StaticContractDirectory, ids: dict[str, uuid.UUID]
) -> ContractSnapshot:
    return contracts.add(
        ContractSnapshot(
            contract_id=ids["contract"],
            property_id=ids["property"],
            tenant_id=ids["tenant"],
            owner_id=ids["owner"],
            security_deposit_amount=Decimal("10000.00"),
            currency="AED",
            status="ACTIVE",
            end_date=date.today() + timedelta(days=60),
            property_reference="DXB-MRN-1204",
            property_address="Apt 1204, Marina Heights, Dubai Marina, Dubai",
            tenant_name="Aisha Rahman",
            tenant_email="aisha@example.ae",
            owner_name="Khalid Al Mansoori",
            owner_email="khalid@example.ae",
        )
    )


@pytest.fixture
def agency(agencies: StaticAgencyDirectory, ids: dict[str, uuid.UUID]) -> AgencySnapshot:
    return agencies.add(
        AgencySnapshot(
            agency_id=ids["agency"],
            name="Emirates Property Inspections LLC",
            email="ops@epi.example.ae",
            is_active=True,
        )
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def tenant_auth(settings: Settings, ids: dict[str, uuid.UUID]) -> dict[str, str]:
    return _auth(
        issue_token(
            subject_id=ids["tenant"],
            role=Role.TENANT,
            email="aisha@example.ae",
            settings=settings,
        )
    )


@pytest.fixture
def owner_auth(settings: Settings, ids: dict[str, uuid.UUID]) -> dict[str, str]:
    return _auth(
        issue_token(
            subject_id=ids["owner"],
            role=Role.OWNER,
            email="khalid@example.ae",
            settings=settings,
        )
    )


@pytest.fixture
def agency_auth(settings: Settings, ids: dict[str, uuid.UUID]) -> dict[str, str]:
    return _auth(
        issue_token(
            subject_id=ids["agency_user"],
            role=Role.INSPECTION_AGENCY,
            org_id=ids["agency"],
            email="inspector@epi.example.ae",
            settings=settings,
        )
    )


@pytest.fixture
def admin_auth(settings: Settings, ids: dict[str, uuid.UUID]) -> dict[str, str]:
    return _auth(issue_token(subject_id=ids["admin"], role=Role.ADMIN, settings=settings))


@pytest.fixture
def move_out_date() -> str:
    return (date.today() + timedelta(days=30)).isoformat()


def future(days: int = 3, hours: int = 0) -> datetime:
    return datetime.now(UTC) + timedelta(days=days, hours=hours)
