"""Error vocabulary.

Every ``code`` below is read from ``api.yaml#_error_codes`` at import time. Constructing
an error with a code that is not in that list raises — the module cannot invent one
(AGENTS.md, "Error codes come from api.yaml verbatim").
"""

from __future__ import annotations

from typing import Any, Final

from .spec import error_codes

#: api.yaml#_error_codes, verbatim.
ERROR_CODES: Final[frozenset[str]] = frozenset(error_codes())

EXIT_ALREADY_IN_PROGRESS: Final = "EXIT_ALREADY_IN_PROGRESS"
EXIT_WORKFLOW_INCOMPLETE: Final = "EXIT_WORKFLOW_INCOMPLETE"
WRONG_STATE: Final = "WRONG_STATE"
MOVE_OUT_DATE_IN_PAST: Final = "MOVE_OUT_DATE_IN_PAST"
REASON_INVALID: Final = "REASON_INVALID"
DOCUMENTS_REQUIRED: Final = "DOCUMENTS_REQUIRED"
PAYMENT_PENDING: Final = "PAYMENT_PENDING"
SPEC_UNRESOLVED_R8: Final = "SPEC_UNRESOLVED_R8"

# Fail loudly at import if the kit and this module ever disagree.
_DECLARED = {
    EXIT_ALREADY_IN_PROGRESS,
    EXIT_WORKFLOW_INCOMPLETE,
    WRONG_STATE,
    MOVE_OUT_DATE_IN_PAST,
    REASON_INVALID,
    DOCUMENTS_REQUIRED,
    PAYMENT_PENDING,
    SPEC_UNRESOLVED_R8,
}
if set(ERROR_CODES) != _DECLARED:  # pragma: no cover - guards a spec edit
    raise RuntimeError(
        "error code vocabulary drifted from api.yaml#_error_codes: "
        f"missing={sorted(set(ERROR_CODES) - _DECLARED)} extra={sorted(_DECLARED - ERROR_CODES)}"
    )


class ExitWorkflowError(Exception):
    """Base class for every error this module reports over HTTP.

    ``code`` must be a member of api.yaml#_error_codes, or None when api.yaml defines no
    code for the condition (see blockers.md#B-6, #B-7). ``http_status`` is taken from the
    endpoint's declared responses in api.yaml.
    """

    code: str | None = None
    http_status: int = 400

    def __init__(self, message: str, /, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details
        if self.code is not None and self.code not in ERROR_CODES:  # pragma: no cover
            raise RuntimeError(f"{self.code!r} is not an api.yaml error code")

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ExitAlreadyInProgress(ExitWorkflowError):
    """rules.yaml#EXIT-01, edges.yaml#X-001 — carries the existing workflow ID."""

    code = EXIT_ALREADY_IN_PROGRESS
    http_status = 409


class ExitWorkflowIncomplete(ExitWorkflowError):
    """rules.yaml#EXIT-03 (BR-1), edges.yaml#X-006 — property is exit-locked."""

    code = EXIT_WORKFLOW_INCOMPLETE
    http_status = 409


class WrongState(ExitWorkflowError):
    """api.yaml 409 WRONG_STATE — the transition is not permitted from the current state."""

    code = WRONG_STATE
    http_status = 409


class ForbiddenTransition(WrongState):
    """states.yaml#exit_workflow.forbidden — an explicitly forbidden transition.

    A forbidden transition raises; it is never a silent no-op (AGENTS.md, Conventions).
    """


class ContractNotActive(WrongState):
    """algorithm.md#1 — contract must be ACTIVE. Reported as 422 per the algorithm.

    api.yaml declares no dedicated code for this condition; WRONG_STATE is the only
    verbatim code that fits. See blockers.md#B-6.
    """

    http_status = 422


class MoveOutDateInPast(ExitWorkflowError):
    """rules.yaml#EXIT-02, edges.yaml#X-007 — Asia/Dubai calendar day."""

    code = MOVE_OUT_DATE_IN_PAST
    http_status = 422


class ReasonInvalid(ExitWorkflowError):
    """rules.yaml#EXIT-02 — reason must come from the reference list."""

    code = REASON_INVALID
    http_status = 422


class DocumentsRequired(ExitWorkflowError):
    """rules.yaml#EXIT-02 — at least one document."""

    code = DOCUMENTS_REQUIRED
    http_status = 422


class PaymentPendingError(ExitWorkflowError):
    """rules.yaml#EXIT-08, edges.yaml#X-004 — refund not SUCCEEDED, NOC refused."""

    code = PAYMENT_PENDING
    http_status = 409


class NotAuthorized(ExitWorkflowError):
    """api.yaml ``authz`` line for the endpoint.

    api.yaml defines no error code for an authorization failure, so ``code`` stays None
    rather than inventing one. See blockers.md#B-7.
    """

    code = None
    http_status = 403


class WorkflowNotFound(ExitWorkflowError):
    """api.yaml declares no 404 code; ``code`` stays None. See blockers.md#B-7."""

    code = None
    http_status = 404


class SpecUnresolved(Exception):
    """A branch the specification does not decide. Raised, never guessed.

    AGENTS.md: "If the spec does not answer a question, STOP ... mark that branch BLOCKED
    in code by raising SpecUnresolved. Do NOT invent an answer."

    ``blocker`` is the ID in blockers.md. ``R8`` is the only blocker api.yaml gives a code
    and status for: 501 SPEC_UNRESOLVED_R8 on ``/exit-workflows/{id}/settle``.
    """

    def __init__(self, blocker: str, message: str = "", /, **details: Any) -> None:
        self.blocker = blocker
        self.message = message or f"blocked on blockers.md#{blocker}"
        self.details = details
        super().__init__(f"[{blocker}] {self.message}")

    @property
    def code(self) -> str | None:
        return SPEC_UNRESOLVED_R8 if self.blocker == "R8" else None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "blocker": self.blocker,
        }
        if self.details:
            payload["details"] = self.details
        return payload
