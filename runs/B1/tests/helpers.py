"""Helpers for driving a workflow through the API in tests."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import Tenancy


async def initiate(client: AsyncClient, tenancy: Tenancy, body: dict[str, Any]) -> str:
    """POST /exit-workflows and return the new workflow ID."""
    response = await client.post("/exit-workflows", json=body, headers=tenancy.headers("tenant"))
    assert response.status_code == 201, response.text
    return response.json()["workflow_id"]


async def schedule_inspection(client: AsyncClient, tenancy: Tenancy, workflow_id: str):
    return await client.post(
        f"/exit-workflows/{workflow_id}/schedule-inspection", headers=tenancy.headers("owner")
    )


async def submit_report(
    client: AsyncClient,
    tenancy: Tenancy,
    workflow_id: str,
    damage: Decimal | str,
    photos: list[str] | None = None,
):
    return await client.post(
        f"/exit-workflows/{workflow_id}/inspection-report",
        json={"damage_amount": str(damage), "photos": photos or ["photo-1"]},
        headers=tenancy.headers("inspection_agency"),
    )


async def confirm_damage(client: AsyncClient, tenancy: Tenancy, workflow_id: str):
    return await client.post(
        f"/exit-workflows/{workflow_id}/confirm-damage", headers=tenancy.headers("owner")
    )


async def settle(client: AsyncClient, tenancy: Tenancy, workflow_id: str, role: str = "owner"):
    return await client.post(
        f"/exit-workflows/{workflow_id}/settle", headers=tenancy.headers(role)
    )


async def drive_to_damage_confirmed(
    client: AsyncClient,
    tenancy: Tenancy,
    body: dict[str, Any],
    damage: Decimal | str = Decimal("0.00"),
) -> str:
    """Initiation through owner confirmation (algorithm.md steps 1 to 8)."""
    workflow_id = await initiate(client, tenancy, body)
    await advance_to_damage_confirmed(client, tenancy, workflow_id, damage)
    return workflow_id


async def advance_to_damage_confirmed(
    client: AsyncClient,
    tenancy: Tenancy,
    workflow_id: str,
    damage: Decimal | str = Decimal("0.00"),
) -> str:
    """Steps 6 to 8 on an already-initiated workflow."""
    response = await schedule_inspection(client, tenancy, workflow_id)
    assert response.status_code == 200, response.text

    response = await submit_report(client, tenancy, workflow_id, damage)
    assert response.status_code == 200, response.text

    response = await confirm_damage(client, tenancy, workflow_id)
    assert response.status_code == 200, response.text
    return workflow_id


async def workflow_row(session_factory: async_sessionmaker[AsyncSession], workflow_id: str) -> dict:
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT * FROM exit_workflows WHERE id = :id"), {"id": workflow_id}
        )
        row = result.mappings().one()
    return dict(row)


async def scalar(session_factory: async_sessionmaker[AsyncSession], sql: str, **params) -> Any:
    async with session_factory() as session:
        return await session.scalar(text(sql), params)


async def rows(session_factory: async_sessionmaker[AsyncSession], sql: str, **params) -> list[dict]:
    async with session_factory() as session:
        result = await session.execute(text(sql), params)
        return [dict(row) for row in result.mappings()]
