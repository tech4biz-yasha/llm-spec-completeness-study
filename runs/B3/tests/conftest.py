"""Test harness.

Runs against a real PostgreSQL: the module depends on row locks, a sequence, unique
constraints and append-only triggers, and none of those can be exercised against a
substitute engine. Point ``EXIT_TEST_DATABASE_URL`` at any database the test user may
drop and recreate the ``public`` schema in.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "postgresql+psycopg2:///exit_workflow_b_test"
TEST_DATABASE_URL = os.environ.get("EXIT_TEST_DATABASE_URL", DEFAULT_URL)

# Settings are read from the environment; set this before anything imports the app.
os.environ["EXIT_DATABASE_URL"] = TEST_DATABASE_URL

from exit_workflow.adapters.events import InMemoryEventPublisher
from exit_workflow.adapters.noc_pdf import SimpleNocPdfRenderer
from exit_workflow.adapters.payments import ScriptedPaymentGateway
from exit_workflow.adapters.storage import InMemoryObjectStorage
from exit_workflow.app import build_app
from exit_workflow.clock import UTC, FrozenClock
from exit_workflow.config import Settings
from exit_workflow.db.models import Contract, Property
from exit_workflow.db.session import build_engine, build_session_factory
from exit_workflow.enums import Actor
from exit_workflow.money import to_minor
from exit_workflow.ports.reference import StaticExitReasonReference
from exit_workflow.services.identity import Principal
from exit_workflow.services.workflow import ExitWorkflowService

#: The exit reason reference list does not exist in the kit (risks.md, Appendix A
#: carry-over; blockers.md#B-2). These values are TEST FIXTURE DATA supplied by the test
#: deployment, exactly as a real deployment must supply the approved list. They are not a
#: guess at the real list, and no module-under-test contains them.
TEST_EXIT_REASONS = frozenset({"END_OF_TENANCY", "RELOCATION", "MUTUAL_TERMINATION"})

TENANT_ID = "USR-TENANT-1"
OWNER_ID = "USR-OWNER-1"
AGENCY_ID = "USR-AGENCY-1"
PROPERTY_ID = "PROP-1"
CONTRACT_ID = "CON-1"
DEPOSIT = Decimal("10000.00")

#: A fixed instant so date arithmetic is deterministic. 2026-03-01 12:00 Asia/Dubai.
FIXED_NOW = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)


def _ensure_database(url: str) -> None:
    target = sa.engine.make_url(url)
    admin = target.set(database="postgres")
    engine = sa.create_engine(admin, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            ).scalar()
            if not exists:
                connection.execute(sa.text(f'CREATE DATABASE "{target.database}"'))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def engine() -> Iterator[sa.Engine]:
    try:
        _ensure_database(TEST_DATABASE_URL)
    except sa.exc.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL is not reachable at {TEST_DATABASE_URL}: {exc}")
    engine = build_engine(Settings(database_url=TEST_DATABASE_URL))
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def schema(engine: sa.Engine) -> Iterator[None]:
    """A migrated, empty schema per test.

    Rebuilt rather than truncated: the audit and NOC tables carry append-only triggers
    that (correctly) refuse TRUNCATE.
    """
    from alembic import command
    from alembic.config import Config

    with engine.begin() as connection:
        connection.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield


@pytest.fixture
def session_factory(engine: sa.Engine) -> sessionmaker[Session]:
    return build_session_factory(engine)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(FIXED_NOW)


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url=TEST_DATABASE_URL)


@pytest.fixture
def gateway() -> ScriptedPaymentGateway:
    return ScriptedPaymentGateway()


@pytest.fixture
def publisher() -> InMemoryEventPublisher:
    return InMemoryEventPublisher()


@pytest.fixture
def storage() -> InMemoryObjectStorage:
    return InMemoryObjectStorage(region="me-central-1")


@pytest.fixture
def reasons() -> StaticExitReasonReference:
    return StaticExitReasonReference(TEST_EXIT_REASONS)


@pytest.fixture
def seed(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        session.add(Property(id=PROPERTY_ID, owner_id=OWNER_ID, exit_lock=False))
        session.add(
            Contract(
                id=CONTRACT_ID,
                property_id=PROPERTY_ID,
                tenant_id=TENANT_ID,
                owner_id=OWNER_ID,
                status="ACTIVE",
                security_deposit_minor=to_minor(DEPOSIT),
                currency="AED",
            )
        )
        session.commit()


@pytest.fixture
def service(
    session_factory: sessionmaker[Session],
    settings: Settings,
    clock: FrozenClock,
    reasons: StaticExitReasonReference,
    gateway: ScriptedPaymentGateway,
    storage: InMemoryObjectStorage,
    publisher: InMemoryEventPublisher,
    seed: None,
) -> ExitWorkflowService:
    return ExitWorkflowService(
        session_factory=session_factory,
        settings=settings,
        clock=clock,
        reasons=reasons,
        gateway=gateway,
        storage=storage,
        renderer=SimpleNocPdfRenderer(),
        publisher=publisher,
    )


@pytest.fixture
def app(
    session_factory: sessionmaker[Session],
    engine: sa.Engine,
    settings: Settings,
    clock: FrozenClock,
    reasons: StaticExitReasonReference,
    gateway: ScriptedPaymentGateway,
    storage: InMemoryObjectStorage,
    publisher: InMemoryEventPublisher,
    seed: None,
):
    return build_app(
        reasons=reasons,
        gateway=gateway,
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        clock=clock,
        storage=storage,
        publisher=publisher,
    )


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def headers(role: str, user_id: str) -> dict[str, str]:
    return {"X-Actor-Id": user_id, "X-Actor-Role": role}


@pytest.fixture
def tenant_headers() -> dict[str, str]:
    return headers("tenant", TENANT_ID)


@pytest.fixture
def owner_headers() -> dict[str, str]:
    return headers("owner", OWNER_ID)


@pytest.fixture
def agency_headers() -> dict[str, str]:
    # api.yaml spells this role ``inspection_agency``.
    return headers("inspection_agency", AGENCY_ID)


@pytest.fixture
def system_headers() -> dict[str, str]:
    return headers("system", "SYSTEM")


@pytest.fixture
def tenant() -> Principal:
    return Principal(user_id=TENANT_ID, role=Actor.TENANT)


@pytest.fixture
def owner() -> Principal:
    return Principal(user_id=OWNER_ID, role=Actor.OWNER)


@pytest.fixture
def agency() -> Principal:
    return Principal(user_id=AGENCY_ID, role=Actor.INSPECTOR)


@pytest.fixture
def system() -> Principal:
    return Principal(user_id="SYSTEM", role=Actor.SYSTEM)


@pytest.fixture
def move_out_date(clock: FrozenClock) -> date:
    from exit_workflow.clock import business_today

    return business_today(clock) + timedelta(days=7)
