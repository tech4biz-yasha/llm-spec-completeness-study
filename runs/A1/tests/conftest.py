"""Test harness.

Runs against a real PostgreSQL database — the module leans on partial unique indexes, check
constraints, native enums, sequences and ``FOR UPDATE``, none of which a SQLite stand-in would
exercise. Set ``TEST_DATABASE_URL`` to point somewhere other than the default.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

# Configure the environment before anything imports Settings.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://yasha@localhost:5432/exit_workflow_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ENVIRONMENT"] = "test"
os.environ["JWT_SECRET"] = "test-secret-not-for-production"
os.environ["OUTBOX_RELAY_ENABLED"] = "false"
os.environ["DEBUG"] = "false"

import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app import db as db_module  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    Contract,
    ContractStatus,
    InspectionAgency,
    Owner,
    Property,
    Tenant,
)
from app.security import PrincipalRole, encode_token, generate_api_key  # noqa: E402

get_settings.cache_clear()
SETTINGS = get_settings()


#: The schema is built once per test session. The engine itself is function-scoped so every
#: test owns its connections on its own event loop — a session-scoped async fixture would be
#: bound to a loop the tests no longer run on.
_schema_ready = False


@pytest.fixture
async def engine():
    global _schema_ready
    engine = db_module.create_engine(SETTINGS)
    if not _schema_ready:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        _schema_ready = True
    db_module.configure(engine)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_database(engine) -> AsyncIterator[None]:
    """Truncate every table and reset sequences so each test starts from a known state."""
    table_names = ", ".join(f'"{t}"' for t in Base.metadata.tables)
    async with engine.begin() as conn:
        await conn.execute(sa.text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
        await conn.execute(sa.text("ALTER SEQUENCE exit_workflow_reference_seq RESTART WITH 1"))
        await conn.execute(sa.text("ALTER SEQUENCE exit_noc_number_seq RESTART WITH 1"))
    yield


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
def app(engine):
    return create_app(SETTINGS)


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# --- fixtures for the domain -----------------------------------------------------------


class Actors:
    """The cast for a test: an owner, a tenant, a property, a contract and an agency."""

    def __init__(
        self,
        owner: Owner,
        tenant: Tenant,
        prop: Property,
        contract: Contract,
        agency: InspectionAgency,
        agency_key: str,
    ) -> None:
        self.owner = owner
        self.tenant = tenant
        self.property = prop
        self.contract = contract
        self.agency = agency
        self.agency_key = agency_key

    def token(self, role: PrincipalRole, subject: uuid.UUID) -> str:
        return encode_token(
            {"sub": str(subject), "role": role.value},
            SETTINGS.jwt_secret,
            expires_in=timedelta(hours=1),
        )

    @property
    def tenant_auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(PrincipalRole.TENANT, self.tenant.id)}"}

    @property
    def owner_auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(PrincipalRole.OWNER, self.owner.id)}"}

    @property
    def agency_auth(self) -> dict[str, str]:
        return {"X-Agency-Key": self.agency_key}

    @property
    def admin_auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(PrincipalRole.ADMIN, uuid.uuid4())}"}


DEPOSIT_FILS = 500_000  # 5,000.00 AED


@pytest.fixture
async def actors(session: AsyncSession) -> Actors:
    return await seed_actors(session)


async def seed_actors(
    session: AsyncSession,
    *,
    suffix: str | None = None,
    deposit_fils: int = DEPOSIT_FILS,
) -> Actors:
    tag = suffix or uuid.uuid4().hex[:8]
    owner = Owner(full_name="Khalid Al Falasi", email=f"owner-{tag}@example.ae", phone="+97150000")
    tenant = Tenant(
        full_name="Aisha Al-Mansouri",
        email=f"tenant-{tag}@example.ae",
        emirates_id="784-1990-1234567-1",
    )
    session.add_all([owner, tenant])
    await session.flush()

    prop = Property(
        owner_id=owner.id,
        reference=f"PROP-{tag}",
        address_line="Apt 1204, Marina Heights Tower",
        community="Dubai Marina",
        city="Dubai",
        emirate="Dubai",
    )
    session.add(prop)
    await session.flush()

    contract = Contract(
        contract_number=f"CTR-{tag}",
        property_id=prop.id,
        tenant_id=tenant.id,
        owner_id=owner.id,
        status=ContractStatus.ACTIVE,
        start_date=date.today() - timedelta(days=300),
        end_date=date.today() + timedelta(days=65),
        security_deposit_fils=deposit_fils,
        annual_rent_fils=12_000_000,
    )
    api_key, api_key_hash = generate_api_key()
    agency = InspectionAgency(
        name=f"Falcon Property Inspections {tag}",
        email=f"agency-{tag}@example.ae",
        trade_license_number="DED-118822",
        api_key_hash=api_key_hash,
        is_active=True,
    )
    session.add_all([contract, agency])
    await session.commit()

    return Actors(owner, tenant, prop, contract, agency, api_key)


def future_slot(days: int = 3, hours: int = 2) -> tuple[str, str]:
    start = datetime.now(UTC) + timedelta(days=days)
    return start.isoformat(), (start + timedelta(hours=hours)).isoformat()


def move_out_date(days: int = 30) -> str:
    return (date.today() + timedelta(days=days)).isoformat()
