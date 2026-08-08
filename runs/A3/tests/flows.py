"""Helpers that drive an exit workflow to a given stage."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.conftest import future

API = "/api/v1"


def expect(response, *codes: int) -> Any:
    assert response.status_code in codes, (
        f"{response.request.method} {response.request.url} -> {response.status_code}: "
        f"{response.text}"
    )
    return response.json() if response.content else None


async def initiate(client: AsyncClient, auth, contract_id, move_out_date, **kw) -> dict:
    payload = {
        "contract_id": str(contract_id),
        "move_out_date": move_out_date,
        "reason": kw.pop("reason", "END_OF_TERM"),
        **kw,
    }
    return expect(await client.post(f"{API}/exit-workflows", json=payload, headers=auth), 201)


async def submit(client: AsyncClient, auth, ref: str) -> dict:
    return expect(await client.post(f"{API}/exit-workflows/{ref}/submit", headers=auth), 200)


async def approve(client: AsyncClient, auth, ref: str) -> dict:
    return expect(
        await client.post(
            f"{API}/exit-workflows/{ref}/owner-decision",
            json={"decision": "APPROVE"},
            headers=auth,
        ),
        200,
    )


async def request_inspection(client: AsyncClient, auth, ref: str, agency_id) -> dict:
    return expect(
        await client.post(
            f"{API}/exit-workflows/{ref}/inspections",
            json={"agency_id": str(agency_id), "notes": "Tenant available after 16:00"},
            headers=auth,
        ),
        201,
    )


async def propose_slots(client: AsyncClient, auth, inspection_id: str) -> dict:
    return expect(
        await client.post(
            f"{API}/inspections/{inspection_id}/slots",
            json={
                "slots": [
                    {
                        "starts_at": future(days=3).isoformat(),
                        "ends_at": future(days=3, hours=2).isoformat(),
                    },
                    {
                        "starts_at": future(days=4).isoformat(),
                        "ends_at": future(days=4, hours=2).isoformat(),
                    },
                ]
            },
            headers=auth,
        ),
        200,
    )


async def schedule(client: AsyncClient, auth, inspection_id: str, slot_id: str) -> dict:
    return expect(
        await client.post(
            f"{API}/inspections/{inspection_id}/schedule",
            json={"slot_id": slot_id},
            headers=auth,
        ),
        200,
    )


async def submit_report(
    client: AsyncClient, auth, inspection_id: str, line_items: list[dict] | None = None
) -> dict:
    body = {
        "inspected_at": future(days=0, hours=-1).isoformat(),
        "summary": "Two-bedroom apartment, generally well maintained.",
        "inspector_name": "R. Fernandes",
        "line_items": line_items
        if line_items is not None
        else [
            {
                "category": "PAINT_AND_WALLS",
                "severity": "MODERATE",
                "description": "Nail holes and scuffing across the living room wall",
                "assessed_amount": "850.00",
                "location": "Living room",
            },
            {
                "category": "CLEANING",
                "severity": "MINOR",
                "description": "Deep clean required in kitchen",
                "assessed_amount": "400.00",
                "location": "Kitchen",
            },
            {
                "category": "FLOORING",
                "severity": "MINOR",
                "description": "Fair wear on hallway skirting",
                "assessed_amount": "300.00",
                "tenant_liable": False,
            },
        ],
    }
    return expect(
        await client.post(
            f"{API}/inspections/{inspection_id}/damage-report", json=body, headers=auth
        ),
        201,
    )


async def finalize(client: AsyncClient, auth, ref: str, **body) -> dict:
    return expect(
        await client.post(
            f"{API}/exit-workflows/{ref}/settlement/finalize", json=body, headers=auth
        ),
        200,
    )


async def pay(client: AsyncClient, auth, ref: str, key: str, **body) -> Any:
    return await client.post(
        f"{API}/exit-workflows/{ref}/settlement/pay",
        json=body,
        headers={**auth, "Idempotency-Key": key},
    )


async def drive_to_damage_review(
    client: AsyncClient, *, tenant_auth, owner_auth, agency_auth, contract, agency, move_out_date
) -> dict:
    """Initiate → submit → approve → inspect → report. Returns useful ids."""

    workflow = await initiate(client, tenant_auth, contract.contract_id, move_out_date)
    ref = workflow["reference"]
    await submit(client, tenant_auth, ref)
    await approve(client, owner_auth, ref)
    inspection = await request_inspection(client, owner_auth, ref, agency.agency_id)
    proposed = await propose_slots(client, agency_auth, inspection["id"])
    slot_id = proposed["slots"][0]["id"]
    await schedule(client, owner_auth, inspection["id"], slot_id)
    report = await submit_report(client, agency_auth, inspection["id"])
    return {
        "workflow": workflow,
        "reference": ref,
        "inspection_id": inspection["id"],
        "slot_id": slot_id,
        "report": report,
    }
