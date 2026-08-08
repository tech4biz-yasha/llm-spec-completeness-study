"""ORM -> response-schema mapping.

Kept out of the routers so the wire shape is defined in one place and the models stay
free of presentation concerns.
"""

from __future__ import annotations

from app.core.context import RequestContext
from app.domain.state_machine import available_actions, describe
from app.models.document import ExitDocument
from app.models.exit_workflow import ExitWorkflow, StateTransition
from app.models.inspection import DamageItem, Inspection, InspectionSlot
from app.models.noc import ExitNoc
from app.models.settlement import Settlement
from app.schemas.documents import DocumentResponse
from app.schemas.exit_workflow import (
    ExitWorkflowDetail,
    ExitWorkflowSummary,
    ProgressView,
    TimelineEntry,
)
from app.schemas.inspection import (
    DamageItemResponse,
    InspectionResponse,
    SlotResponse,
)
from app.schemas.noc import NocResponse
from app.schemas.settlement import DeductionLineResponse, SettlementResponse


def workflow_summary(workflow: ExitWorkflow) -> ExitWorkflowSummary:
    return ExitWorkflowSummary.model_validate(workflow)


def workflow_detail(
    workflow: ExitWorkflow, ctx: RequestContext, *, document_count: int
) -> ExitWorkflowDetail:
    descriptor = describe(workflow.state)
    hint = (
        descriptor.tenant_hint
        if ctx.principal.role.value == "TENANT"
        else descriptor.owner_hint or descriptor.tenant_hint
    )
    detail = ExitWorkflowDetail.model_validate(
        workflow,
        update={
            "progress": ProgressView(
                step=descriptor.step,
                label=descriptor.label,
                hint=hint,
                is_terminal=workflow.is_terminal,
                blocks_new_contracts=workflow.is_blocking,
            ),
            "available_actions": available_actions(workflow.state, ctx.principal.role),
            "document_count": document_count,
            "has_inspection": workflow.inspection is not None,
            "has_settlement": workflow.settlement is not None,
            "has_noc": workflow.noc is not None,
        },
    )
    return detail


def timeline_entry(transition: StateTransition) -> TimelineEntry:
    return TimelineEntry.model_validate(transition)


def document(doc: ExitDocument, *, base_path: str) -> DocumentResponse:
    return DocumentResponse.model_validate(
        doc,
        update={
            "download_url": f"{base_path}/{doc.workflow_id}/documents/{doc.id}/content"
        },
    )


def slot(value: InspectionSlot) -> SlotResponse:
    return SlotResponse.model_validate(value)


def damage_item(item: DamageItem) -> DamageItemResponse:
    return DamageItemResponse.model_validate(
        item, update={"chargeable_amount": item.chargeable_amount}
    )


def inspection(value: Inspection) -> InspectionResponse:
    return InspectionResponse.model_validate(
        value,
        update={
            "slots": [slot(s) for s in value.slots],
            "damage_items": [damage_item(d) for d in value.damage_items],
        },
    )


def settlement(value: Settlement) -> SettlementResponse:
    return SettlementResponse.model_validate(
        value,
        update={
            "deductions": [
                DeductionLineResponse(
                    id=line.id,
                    damage_item_id=line.damage_item_id,
                    category=line.category,
                    description=line.description,
                    amount=line.amount,
                )
                for line in value.deductions
            ]
        },
    )


def noc(value: ExitNoc, *, base_path: str) -> NocResponse:
    return NocResponse.model_validate(
        value,
        update={"download_url": f"{base_path}/{value.workflow_id}/noc/content"},
    )
