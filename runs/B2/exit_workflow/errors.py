"""Error taxonomy for the exit workflow module.

Error codes are taken VERBATIM from api.yaml#_error_codes. No code outside that
list is ever emitted (AGENTS.md, "Error codes come from api.yaml verbatim").

Two spec branches have an HTTP status but no error code in api.yaml:
  * initiation on a non-ACTIVE contract (algorithm.md step 1 says 422, no code)
  * a refund payment that comes back FAILED (algorithm.md step 11 says hold, no code)
Both are recorded in blockers.md (B-004, B-005). Where no code exists, the
response carries `code: null` rather than an invented string.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """api.yaml#_error_codes — verbatim, complete, closed."""

    EXIT_ALREADY_IN_PROGRESS = "EXIT_ALREADY_IN_PROGRESS"
    EXIT_WORKFLOW_INCOMPLETE = "EXIT_WORKFLOW_INCOMPLETE"
    WRONG_STATE = "WRONG_STATE"
    MOVE_OUT_DATE_IN_PAST = "MOVE_OUT_DATE_IN_PAST"
    REASON_INVALID = "REASON_INVALID"
    DOCUMENTS_REQUIRED = "DOCUMENTS_REQUIRED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    SPEC_UNRESOLVED_R8 = "SPEC_UNRESOLVED_R8"


class ExitWorkflowError(Exception):
    """Base class for every fault this module reports to a caller."""

    http_status: int = 500
    code: ErrorCode | None = None

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class SpecUnresolved(ExitWorkflowError):
    """A branch the specification does not decide.

    AGENTS.md: "If the spec does not answer a question, STOP ... mark that branch
    BLOCKED in code by raising SpecUnresolved. Do NOT invent an answer."

    `item` identifies the open question (risks.md id, or a blockers.md id).
    Only R8 has an error code in api.yaml; every other item reports `code: null`.
    """

    http_status = 501

    def __init__(self, item: str, message: str | None = None, *, details: dict | None = None) -> None:
        super().__init__(message or f"Specification unresolved: {item}", details=details)
        self.item = item
        self.code = ErrorCode.SPEC_UNRESOLVED_R8 if item == "R8" else None


class ExitAlreadyInProgress(ExitWorkflowError):
    """rules.yaml#EXIT-01, edges.yaml#X-001 — one workflow per contract, ever concurrent."""

    http_status = 409
    code = ErrorCode.EXIT_ALREADY_IN_PROGRESS

    def __init__(self, existing_workflow_id: str) -> None:
        super().__init__(
            f"An exit workflow is already in progress for this contract: {existing_workflow_id}",
            details={"workflow_id": existing_workflow_id},
        )
        self.existing_workflow_id = existing_workflow_id


class ExitWorkflowIncomplete(ExitWorkflowError):
    """rules.yaml#EXIT-03 / BR-1, edges.yaml#X-006 — property held by an exit lock."""

    http_status = 409
    code = ErrorCode.EXIT_WORKFLOW_INCOMPLETE


class WrongState(ExitWorkflowError):
    """api.yaml 409 WRONG_STATE — the requested step is not legal from the current state."""

    http_status = 409
    code = ErrorCode.WRONG_STATE

    def __init__(self, message: str, *, from_state: str | None = None, to_state: str | None = None) -> None:
        super().__init__(message, details={"from": from_state, "to": to_state})
        self.from_state = from_state
        self.to_state = to_state


class ForbiddenTransition(WrongState):
    """states.yaml#exit_workflow.forbidden — an explicitly forbidden transition.

    AGENTS.md: "A forbidden transition raises, never silently no-ops."
    Kept distinct from WrongState so it can be alerted on: reaching one means a
    caller (or a bug) attempted a move the spec names as illegal, not merely
    out-of-order.
    """


class MoveOutDateInPast(ExitWorkflowError):
    """rules.yaml#EXIT-02, edges.yaml#X-007 — Asia/Dubai calendar day."""

    http_status = 422
    code = ErrorCode.MOVE_OUT_DATE_IN_PAST


class ReasonInvalid(ExitWorkflowError):
    """rules.yaml#EXIT-02 — reason must come from the reference list."""

    http_status = 422
    code = ErrorCode.REASON_INVALID


class DocumentsRequired(ExitWorkflowError):
    """rules.yaml#EXIT-02 — at least one document."""

    http_status = 422
    code = ErrorCode.DOCUMENTS_REQUIRED


class PaymentPending(ExitWorkflowError):
    """rules.yaml#EXIT-08, edges.yaml#X-004 — refund not confirmed SUCCEEDED; hold."""

    http_status = 409
    code = ErrorCode.PAYMENT_PENDING


class ContractNotActive(ExitWorkflowError):
    """algorithm.md step 1 / rules.yaml#EXIT-01 — 422, no code defined (blockers.md#B-004)."""

    http_status = 422
    code = None


class NotAuthorized(ExitWorkflowError):
    """api.yaml `authz` lines. No error code is defined for authz failures in
    api.yaml, so none is emitted (blockers.md#B-006)."""

    http_status = 403
    code = None


class WorkflowNotFound(ExitWorkflowError):
    """No workflow with the given id. api.yaml defines no 404 code."""

    http_status = 404
    code = None
