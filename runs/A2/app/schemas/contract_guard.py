"""BR-1 contract-eligibility schemas (SRS §4.7)."""

from __future__ import annotations

import uuid

from pydantic import Field

from app.domain.enums import ExitWorkflowState
from app.schemas.common import ApiModel


class BlockingWorkflow(ApiModel):
    workflow_id: uuid.UUID
    reference: str | None
    state: ExitWorkflowState
    scope: str = Field(description="PROPERTY or TENANT")
    property_id: uuid.UUID
    tenant_id: uuid.UUID
    message: str


class ContractEligibilityResponse(ApiModel):
    """Answers "may a new contract be created?" for the Property service.

    ``allowed`` is the machine-readable verdict; ``warnings`` carries the SRS-mandated
    text for the owner portal / tenant app to display when the action is blocked.
    """

    allowed: bool
    property_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    blocking_workflows: list[BlockingWorkflow] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
