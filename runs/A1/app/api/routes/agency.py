"""Agency-facing endpoints (SRS O15).

Authenticated with ``X-Agency-Key``. An agency only ever sees its own assignments.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import ContextDep, SessionDep, SettingsDep
from app.models.inspection import AssignmentStatus
from app.schemas.inspection import (
    AssignmentListOut,
    AssignmentOut,
    DamageReportOut,
    DamageReportRequest,
    ProposeSlotsRequest,
)
from app.services.inspection_service import (
    DamageLineInput,
    DamageReportInput,
    InspectionService,
    SlotProposal,
)

router = APIRouter(prefix="/agency", tags=["inspection-agency"])


@router.get(
    "/assignments",
    response_model=AssignmentListOut,
    summary="List inspection assignments for the calling agency",
)
async def list_assignments(
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
    assignment_status: Annotated[AssignmentStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AssignmentListOut:
    service = InspectionService(session, ctx, settings)
    items = await service.list_agency_assignments(status=assignment_status, limit=limit)
    return AssignmentListOut(items=[AssignmentOut.of(a) for a in items], count=len(items))


@router.post(
    "/assignments/{assignment_id}/slots",
    response_model=AssignmentOut,
    summary="Respond with available inspection dates (Appendix B)",
)
async def propose_slots(
    assignment_id: uuid.UUID,
    payload: ProposeSlotsRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> AssignmentOut:
    service = InspectionService(session, ctx, settings)
    assignment = await service.propose_slots(
        assignment_id,
        [SlotProposal(starts_at=s.starts_at, ends_at=s.ends_at) for s in payload.slots],
    )
    return AssignmentOut.of(assignment)


@router.post(
    "/assignments/{assignment_id}/complete",
    response_model=AssignmentOut,
    summary="Mark the inspection visit as having taken place",
)
async def complete_inspection(
    assignment_id: uuid.UUID, session: SessionDep, ctx: ContextDep, settings: SettingsDep
) -> AssignmentOut:
    service = InspectionService(session, ctx, settings)
    assignment = await service.complete_inspection(assignment_id)
    return AssignmentOut.of(assignment)


@router.post(
    "/assignments/{assignment_id}/report",
    response_model=DamageReportOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload the damage report with photos; deductions are computed (O16)",
)
async def submit_damage_report(
    assignment_id: uuid.UUID,
    payload: DamageReportRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> DamageReportOut:
    service = InspectionService(session, ctx, settings)
    report = await service.submit_damage_report(
        assignment_id,
        DamageReportInput(
            summary=payload.summary,
            inspected_at=payload.inspected_at,
            inspector_name=payload.inspector_name,
            photos=[p.model_dump() for p in payload.photos],
            line_items=[
                DamageLineInput(
                    code=item.code,
                    description=item.description,
                    severity=item.severity,
                    amount_fils=item.amount_fils,
                    location=item.location,
                    photos=[p.model_dump() for p in item.photos],
                )
                for item in payload.line_items
            ],
        ),
    )
    await session.refresh(report, ["line_items"])
    return DamageReportOut.of(report)
