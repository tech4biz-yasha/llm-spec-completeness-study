"""Acceptance test harness.

Runs against a real PostgreSQL: the module's guarantees (partial unique index,
append-only trigger, SELECT ... FOR UPDATE, ON CONFLICT) are database behaviour,
and a substitute engine would test something other than what ships.

Point EXIT_WORKFLOW_TEST_DATABASE_URL at a scratch database. Each test gets a
freshly rebuilt schema, because the audit table refuses UPDATE, DELETE and
TRUNCATE by design (rules.yaml#EXIT-10).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from exit_workflow.adapters.kafka import InMemoryEventPublisher
from exit_workflow.adapters.noc_pdf import NocPdfRenderer
from exit_workflow.adapters.object_store import LocalImmutableObjectStore
from exit_workflow.adapters.reference_data import StaticExitReasonReference
from exit_workflow.api.app import create_app
from exit_workflow.api.deps import build_container
from exit_workflow.clock import DUBAI, UTC, FrozenClock
from exit_workflow.config import Settings
from exit_workflow.db.session import create_session_factory
from exit_workflow.ports import GatewayResult, RefundRequest

MIGRATIONS = Path(__file__).resolve().parents[2] / "exit_workflow" / "migrations"

DEFAULT_TEST_DB = "postgresql+asyncpg://localhost/exit_workflow_test"

#: The exit reason reference list does not exist (risks.md Appendix A,
#: blockers.md#B-001). These are TEST fixtures standing in for whatever the
#: reference data dictionary eventually defines — they are not a proposal and
#: appear nowhere in the module.
TEST_EXIT_REASONS = frozenset({"END_OF_TENANCY", "RELOCATION"})

#: A fixed instant so the Dubai calendar rules (D-001, X-007) are deterministic.
#: 2026-03-01 20:00 UTC is 2026-03-02 00:00 in Asia/Dubai — a case where the UTC
#: day and the Dubai day differ.
FROZEN_INSTANT = datetime(2026, 3, 1, 20, 0, tzinfo=UTC)


def _database_url() -> str:
    return os.environ.get("EXIT_WORKFLOW_TEST_DATABASE_URL", DEFAULT_TEST_DB)


class FakeGateway:
    """Stands in for the payment gateway, which is not chosen yet
    (risks.md Appendix A — finalised payment modes)."""

    def __init__(self, status: str = "SUCCEEDED") -> None:
        self.status = status
        self.calls: list[RefundRequest] = []
        self.status_calls: list[str] = []

    async def initiate_refund(self, request: RefundRequest) -> GatewayResult:
        self.calls.append(request)
        return GatewayResult(
            status=self.status,
            reference=f"gw-{request.idempotency_key}",
            failure_reason="declined" if self.status == "FAILED" else None,
        )

    async def get_status(self, idempotency_key: str) -> GatewayResult:
        self.status_calls.append(idempotency_key)
        return GatewayResult(status=self.status, reference=f"gw-{idempotency_key}")


async def _rebuild_schema() -> None:
    """Apply the migrations to an empty schema.

    Run through raw asyncpg: the migration files are multi-statement scripts and
    asyncpg's prepared-statement path (what SQLAlchemy uses) rejects those.
    """
    import asyncpg

    dsn = _database_url().replace("postgresql+asyncpg://", "postgresql://")
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
        for name in ("0000_external_tables.sql", "0001_exit_workflow.sql"):
            await connection.execute((MIGRATIONS / name).read_text())
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def engine():
    try:
        await _rebuild_schema()
    except (OSError, ConnectionError) as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable at {_database_url()}: {exc}")

    engine = create_async_engine(_database_url())
    yield engine
    await engine.dispose()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(FROZEN_INSTANT)


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url=_database_url(), noc_bucket="meridian-noc-uae-test")


@pytest.fixture
def publisher() -> InMemoryEventPublisher:
    return InMemoryEventPublisher()


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def object_store(tmp_path: Path, settings: Settings) -> LocalImmutableObjectStore:
    return LocalImmutableObjectStore(
        tmp_path / "objects", bucket=settings.noc_bucket, region=settings.noc_region
    )


@pytest_asyncio.fixture
async def container(engine, clock, settings, publisher, gateway, object_store):
    return build_container(
        session_factory=create_session_factory(engine),
        publisher=publisher,
        gateway=gateway,
        renderer=NocPdfRenderer(),
        object_store=object_store,
        reason_reference=StaticExitReasonReference(TEST_EXIT_REASONS),
        settings=settings,
        clock=clock,
    )


@pytest_asyncio.fixture
async def client(container):
    app = create_app(container)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


class Tenancy:
    def __init__(self, *, property_id, owner_id, contract_id, tenant_id, deposit_minor):
        self.property_id = property_id
        self.owner_id = owner_id
        self.contract_id = contract_id
        self.tenant_id = tenant_id
        self.deposit_minor = deposit_minor

    @property
    def tenant_headers(self) -> dict[str, str]:
        return {"X-User-Id": str(self.tenant_id), "X-User-Role": "tenant"}

    @property
    def owner_headers(self) -> dict[str, str]:
        return {"X-User-Id": str(self.owner_id), "X-User-Role": "owner"}

    @property
    def agency_headers(self) -> dict[str, str]:
        return {"X-User-Id": str(uuid.uuid4()), "X-User-Role": "inspection_agency"}


@pytest_asyncio.fixture
async def tenancy(engine):
    async def _seed(
        *, deposit: Decimal = Decimal("5000.00"), status: str = "ACTIVE"
    ) -> Tenancy:
        property_id, owner_id = uuid.uuid4(), uuid.uuid4()
        contract_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        deposit_minor = int(deposit * 100)
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO properties (id, owner_id) VALUES (:id, :owner)"),
                {"id": property_id, "owner": owner_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO contracts (id, property_id, tenant_id, status, "
                    "security_deposit_minor) VALUES (:id, :prop, :tenant, :status, :deposit)"
                ),
                {
                    "id": contract_id,
                    "prop": property_id,
                    "tenant": tenant_id,
                    "status": status,
                    "deposit": deposit_minor,
                },
            )
        return Tenancy(
            property_id=property_id,
            owner_id=owner_id,
            contract_id=contract_id,
            tenant_id=tenant_id,
            deposit_minor=deposit_minor,
        )

    return _seed


@pytest.fixture
def move_out_date(clock: FrozenClock):
    """A valid move-out date: today in Asia/Dubai (rules.yaml#EXIT-02)."""
    return clock.today_dubai()


@pytest.fixture
def initiate_payload(move_out_date):
    def _payload(contract_id, *, reason: str = "END_OF_TENANCY", documents: int = 1, day=None):
        return {
            "contract_id": str(contract_id),
            "move_out_date": (day or move_out_date).isoformat(),
            "reason": reason,
            "documents": [{"document_id": f"doc-{i}"} for i in range(documents)],
        }

    return _payload


async def drive_to_damage_confirmed(client, tenancy_row, payload, *, damage: Decimal):
    """Initiation -> DAMAGE_CONFIRMED, the state settlement starts from."""
    created = await client.post("/exit-workflows", json=payload, headers=tenancy_row.tenant_headers)
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow_id"]

    scheduled = await client.post(
        f"/exit-workflows/{workflow_id}/schedule-inspection",
        json={},
        headers=tenancy_row.owner_headers,
    )
    assert scheduled.status_code == 200, scheduled.text

    reported = await client.post(
        f"/exit-workflows/{workflow_id}/inspection-report",
        json={"damage_amount": str(damage), "photos": [{"photo_id": "p1"}]},
        headers=tenancy_row.agency_headers,
    )
    assert reported.status_code == 200, reported.text

    confirmed = await client.post(
        f"/exit-workflows/{workflow_id}/confirm-damage", headers=tenancy_row.owner_headers
    )
    assert confirmed.status_code == 200, confirmed.text
    return workflow_id


__all__ = [
    "DUBAI",
    "FakeGateway",
    "TEST_EXIT_REASONS",
    "drive_to_damage_confirmed",
    "timedelta",
]
