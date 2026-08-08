"""Concurrency guarantees, the transactional outbox, and payment failure handling."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import OutboxEvent, PaymentStatus, SettlementStatus
from app.models.settlement import DepositSettlement, PaymentLeg
from app.ports.notifications import Notification
from app.ports.outbox import OutboxRelay
from app.ports.payments import GatewayOutcome, PaymentResult
from app.security import Principal, PrincipalRole
from app.services.context import RequestContext
from app.services.settlement_service import SettlementService
from tests.conftest import SETTINGS, Actors, move_out_date
from tests.test_exit_workflow import API, advance_to_damage_review


# --- concurrency --------------------------------------------------------------------------


async def test_concurrent_initiation_yields_exactly_one_workflow(
    app, actors: Actors, session
) -> None:
    """The partial unique index is the real BR-1 backstop when two requests race.

    Both requests can pass the application-level "is there an active workflow?" check, so
    correctness has to come from the database.
    """
    payload = {
        "contract_id": str(actors.contract.id),
        "move_out_date": move_out_date(30),
        "reason_code": "LEASE_EXPIRY",
    }

    async def attempt() -> int:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                f"{API}/exit-workflows", json=payload, headers=actors.tenant_auth
            )
            return response.status_code

    results = await asyncio.gather(*(attempt() for _ in range(6)))

    assert results.count(201) == 1, f"expected exactly one winner, got {results}"
    assert all(code in (201, 409) for code in results), results

    stored = await session.scalar(
        sa.text("SELECT COUNT(*) FROM exit_workflows WHERE contract_id = :c"),
        {"c": actors.contract.id},
    )
    assert stored == 1


async def test_unique_index_blocks_a_second_workflow_even_without_the_precheck(
    client, actors: Actors, monkeypatch, session
) -> None:
    """Prove the storage-level guarantee independently of the application-level check.

    The pre-check normally wins the race, which would leave the partial unique index — the
    thing that actually holds under concurrency — untested. Disabling the pre-check forces
    the second request all the way down to the INSERT.
    """
    from app.services.workflow_service import WorkflowService

    payload = {
        "contract_id": str(actors.contract.id),
        "move_out_date": move_out_date(30),
        "reason_code": "LEASE_EXPIRY",
    }
    first = await client.post(f"{API}/exit-workflows", json=payload, headers=actors.tenant_auth)
    assert first.status_code == 201

    async def no_precheck(self, **kwargs) -> None:
        return None

    monkeypatch.setattr(WorkflowService, "_assert_no_active_workflow", no_precheck)

    second = await client.post(f"{API}/exit-workflows", json=payload, headers=actors.tenant_auth)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "workflow_already_active"

    stored = await session.scalar(
        sa.text("SELECT COUNT(*) FROM exit_workflows WHERE contract_id = :c"),
        {"c": actors.contract.id},
    )
    assert stored == 1


async def test_concurrent_payment_attempts_move_money_once(app, actors: Actors, session) -> None:
    """Two clicks on "Pay Deposit" with the same key produce one payment."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        workflow_id = await advance_to_damage_review(client, actors)
        await client.post(
            f"{API}/exit-workflows/{workflow_id}/settlement/approve", headers=actors.owner_auth
        )

    body = {"leg": "OWNER_REFUND", "idempotency_key": f"race-{workflow_id}"}

    async def attempt() -> int:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                f"{API}/exit-workflows/{workflow_id}/settlement/pay",
                json=body,
                headers=actors.owner_auth,
            )
            return response.status_code

    results = await asyncio.gather(*(attempt() for _ in range(4)))
    assert all(code in (200, 409) for code in results), results

    succeeded = await session.scalar(
        sa.text(
            "SELECT COUNT(*) FROM payment_transactions "
            "WHERE workflow_id = :w AND status = 'SUCCEEDED'"
        ),
        {"w": uuid.UUID(workflow_id)},
    )
    assert succeeded == 1


# --- transactional outbox ------------------------------------------------------------------


class RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def publish(self, events) -> None:
        self.events.extend(e.event_type for e in events)

    async def close(self) -> None:
        return None


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


async def test_outbox_relay_publishes_and_marks_rows(client, actors: Actors, engine, session):
    """Events and notifications are staged in the state transaction, then relayed."""
    await advance_to_damage_review(client, actors)

    unpublished = await session.scalar(
        sa.select(sa.func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None))
    )
    assert unpublished > 0

    publisher, notifier = RecordingPublisher(), RecordingNotifier()
    relay = OutboxRelay(
        async_sessionmaker(bind=engine, expire_on_commit=False),
        publisher=publisher,
        notifier=notifier,
    )
    delivered = await relay.drain_all()
    assert delivered == unpublished

    assert "exit.workflow.initiated" in publisher.events
    assert "exit.inspection.requested" in publisher.events
    assert "exit.damage_report.submitted" in publisher.events

    # The agency really is emailed the property details (Appendix B).
    templates = {n.template.value for n in notifier.sent}
    assert "agency.inspection_requested" in templates
    assert any(
        n.recipient == actors.agency.email and "property_address" in n.context
        for n in notifier.sent
    )

    remaining = await session.scalar(
        sa.select(sa.func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None))
    )
    assert remaining == 0

    # A second drain is a no-op — rows are not redelivered.
    assert await relay.drain_all() == 0


async def test_rolled_back_request_leaves_no_events(client, actors: Actors, session) -> None:
    """A failed request must not emit events for a state change that never happened."""
    before = await session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent))

    response = await client.post(
        f"{API}/exit-workflows",
        json={
            "contract_id": str(uuid.uuid4()),
            "move_out_date": move_out_date(30),
            "reason_code": "LEASE_EXPIRY",
        },
        headers=actors.tenant_auth,
    )
    assert response.status_code == 404

    after = await session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent))
    assert after == before


async def test_relay_survives_a_failing_adapter(client, actors: Actors, engine, session) -> None:
    """One bad row must not stall the queue; it is retried, counted and logged."""
    await advance_to_damage_review(client, actors)

    class BrokenPublisher:
        async def publish(self, events) -> None:
            raise RuntimeError("kafka is unreachable")

        async def close(self) -> None:
            return None

    relay = OutboxRelay(
        async_sessionmaker(bind=engine, expire_on_commit=False),
        publisher=BrokenPublisher(),
        notifier=RecordingNotifier(),
    )
    await relay.drain_once()

    failed = (
        await session.execute(
            sa.select(OutboxEvent.attempts, OutboxEvent.last_error).where(
                OutboxEvent.published_at.is_(None), OutboxEvent.attempts > 0
            )
        )
    ).all()
    assert failed, "expected failed rows to be retained for retry"
    assert all(row.attempts == 1 for row in failed)
    assert all("kafka is unreachable" in row.last_error for row in failed)

    # Notifications still went out — one broken adapter does not block the other.
    published = await session.scalar(
        sa.select(sa.func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.published_at.is_not(None))
    )
    assert published > 0


# --- payment failure -------------------------------------------------------------------------


async def test_declined_payment_leaves_the_settlement_open(
    client, actors: Actors, session
) -> None:
    """A declined payment is recorded as FAILED and nothing advances."""
    workflow_id = await advance_to_damage_review(client, actors)
    await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/approve", headers=actors.owner_auth
    )

    class DecliningGateway:
        async def execute(self, request):
            return PaymentResult(
                outcome=GatewayOutcome.FAILED,
                provider="test",
                failure_reason="insufficient funds",
            )

    ctx = RequestContext(
        principal=Principal(id=actors.owner.id, role=PrincipalRole.OWNER),
        request_id="test-declined",
    )
    service = SettlementService(session, ctx, SETTINGS, gateway=DecliningGateway())

    from app.errors import ConflictError

    with pytest.raises(ConflictError, match="not accepted"):
        await service.pay(
            uuid.UUID(workflow_id), leg=PaymentLeg.OWNER_REFUND, idempotency_key="declined-1"
        )
    await session.commit()

    settlement = await session.scalar(
        sa.select(DepositSettlement).where(
            DepositSettlement.workflow_id == uuid.UUID(workflow_id)
        )
    )
    assert settlement is not None
    assert settlement.status is SettlementStatus.PAYABLE
    assert settlement.closed_at is None
    assert settlement.refund_settled_at is None

    attempts = (
        await session.execute(
            sa.text(
                "SELECT status, failure_reason FROM payment_transactions WHERE workflow_id=:w"
            ),
            {"w": uuid.UUID(workflow_id)},
        )
    ).all()
    assert len(attempts) == 1
    assert attempts[0].status == PaymentStatus.FAILED.value
    assert attempts[0].failure_reason == "insufficient funds"

    # And no NOC was issued.
    noc = await client.get(f"{API}/exit-workflows/{workflow_id}/noc", headers=actors.tenant_auth)
    assert noc.status_code == 404
