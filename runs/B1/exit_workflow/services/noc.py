"""NOC issuance — algorithm.md step 12.

    12. Generate NOC PDF, store UAE bucket, immutable, link to workflow. (EXIT-09)

The kit fixes the format (PDF), the location (UAE region bucket), the
immutability and the link to the workflow. It does not specify the document's
wording, layout, language, or whether it carries a signature or seal — recorded
as blockers.md#B-10. What is rendered here is strictly the facts already held on
the workflow, so nothing in the document asserts anything the kit has not
already decided. It needs legal sign-off before it goes to tenants.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import ExitWorkflow, NocDocument
from exit_workflow.domain import clock as clock_module
from exit_workflow.domain.clock import DUBAI, Clock, DEFAULT_CLOCK
from exit_workflow.domain.money import format_aed
from exit_workflow.repositories.noc import NocRepository
from exit_workflow.storage.noc import ImmutableObjectExists, NocStorage, digest
from exit_workflow.storage.pdf import render_pdf

logger = logging.getLogger(__name__)

TITLE = "NO OBJECTION CERTIFICATE"


def object_key(workflow_id: str) -> str:
    """Deterministic key, so a retried issuance addresses the same object."""
    return f"exit-workflows/{workflow_id}/noc.pdf"


class NocIssuanceService:
    def __init__(
        self,
        session: AsyncSession,
        storage: NocStorage,
        *,
        clock: Clock = DEFAULT_CLOCK,
    ) -> None:
        self._session = session
        self._storage = storage
        self._clock = clock

    def render(self, workflow: ExitWorkflow) -> bytes:
        """Render the NOC PDF from facts already on the workflow."""
        issued_at_dubai = clock_module.now_utc(self._clock).astimezone(DUBAI)
        damage_minor = workflow.damage_amount_minor or 0
        refund_minor = workflow.refund_amount_minor or 0

        lines = [
            "This certificate confirms that the tenancy identified below has been",
            "concluded and that the security deposit has been settled in full.",
            "",
            f"Exit workflow reference : {workflow.id}",
            f"Contract reference      : {workflow.contract_id}",
            f"Property reference      : {workflow.property_id}",
            f"Tenant reference        : {workflow.tenant_id}",
            f"Owner reference         : {workflow.owner_id}",
            "",
            f"Move-out date           : {workflow.move_out_date:%d %B %Y} (Asia/Dubai)",
            f"Security deposit        : {format_aed(workflow.security_deposit_minor)}",
            f"Confirmed damages       : {format_aed(damage_minor)}",
            f"Deposit refunded        : {format_aed(refund_minor)}",
            f"Refund payment          : {workflow.payment_id}",
            "",
            f"Issued                  : {issued_at_dubai:%d %B %Y %H:%M} (Asia/Dubai)",
        ]
        return render_pdf(
            title=TITLE,
            lines=lines,
            subject=f"No Objection Certificate for exit workflow {workflow.id}",
            created_at=issued_at_dubai,
        )

    async def issue(self, workflow: ExitWorkflow) -> NocDocument:
        """Store the NOC and record it against the workflow (rules.yaml#EXIT-09).

        Idempotent. If a previous attempt stored the object but its transaction
        did not commit, the object is still there and immutable; this reads it
        back and records the existing bytes rather than failing forever.
        """
        repository = NocRepository(self._session)
        existing = await repository.get_for_workflow(workflow.id)
        if existing is not None:
            return existing

        key = object_key(workflow.id)
        content = self.render(workflow)
        try:
            stored = await self._storage.put_immutable(key, content)
        except ImmutableObjectExists:
            previous = await self._storage.get(key)
            if previous is None:  # pragma: no cover - storage contradicting itself
                raise
            logger.warning(
                "NOC object for workflow %s already existed; recording the stored copy", workflow.id
            )
            document = repository.record(
                workflow_id=workflow.id,
                bucket=self._storage.bucket,
                region=self._storage.region,
                object_key=key,
                content_sha256=digest(previous),
                byte_size=len(previous),
            )
        else:
            document = repository.record(
                workflow_id=workflow.id,
                bucket=stored.bucket,
                region=stored.region,
                object_key=stored.key,
                content_sha256=stored.sha256,
                byte_size=stored.byte_size,
            )

        await self._session.flush()
        # rules.yaml#EXIT-09 — "linked on the workflow".
        workflow.noc_document_id = document.id
        workflow.noc_issued_at = clock_module.now_utc(self._clock)
        return document
