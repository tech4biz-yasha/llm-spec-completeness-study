"""Error taxonomy.

Error codes are taken verbatim from ``api.yaml#_error_codes`` (AGENTS.md:
"Error codes come from api.yaml verbatim. Never invent one."). The allowed set
is loaded from the kit at import time and every code used below is checked
against it, so a typo or an invented code is an import-time failure rather than
a wrong payload in production.

Two situations produce an error with ``code=None``:

* The kit specifies an HTTP status for a branch but defines no code string for
  it (blockers.md#B-4, #B-6). Emitting a plausible-looking code would be
  inventing one, so the envelope carries the blocker ID instead.
* :class:`SpecUnresolved` for any blocker other than R8, which is the only
  unresolved item api.yaml gives a code for.
"""

from __future__ import annotations

from typing import Any, Final

from exit_workflow.domain import spec


ALLOWED_ERROR_CODES: Final[frozenset[str]] = frozenset(spec.api_error_codes())


def _code(value: str) -> str:
    """Return ``value`` if api.yaml defines it, else fail loudly at import time."""
    if value not in ALLOWED_ERROR_CODES:
        raise spec.SpecLoadError(
            f"error code {value!r} is not declared in api.yaml#_error_codes "
            f"({sorted(ALLOWED_ERROR_CODES)}); codes may not be invented"
        )
    return value


# api.yaml#_error_codes, verbatim.
EXIT_ALREADY_IN_PROGRESS: Final = _code("EXIT_ALREADY_IN_PROGRESS")
EXIT_WORKFLOW_INCOMPLETE: Final = _code("EXIT_WORKFLOW_INCOMPLETE")
WRONG_STATE: Final = _code("WRONG_STATE")
MOVE_OUT_DATE_IN_PAST: Final = _code("MOVE_OUT_DATE_IN_PAST")
REASON_INVALID: Final = _code("REASON_INVALID")
DOCUMENTS_REQUIRED: Final = _code("DOCUMENTS_REQUIRED")
PAYMENT_PENDING: Final = _code("PAYMENT_PENDING")
SPEC_UNRESOLVED_R8: Final = _code("SPEC_UNRESOLVED_R8")


class ExitWorkflowError(Exception):
    """Base class for every error this module surfaces over HTTP."""

    http_status: int = 500
    code: str | None = None

    #: True when the work done before the error is worth keeping.
    #:
    #: Settlement can end in a *hold* rather than a failure: the refund has been
    #: submitted to the gateway and the answer is not SUCCEEDED yet
    #: (algorithm.md step 11). Discarding the transaction would throw away the
    #: payment row for a refund the gateway has already accepted. Handlers
    #: commit before re-raising these.
    preserves_transaction: bool = False

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        blocker: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.blocker = blocker

    def to_payload(self) -> dict[str, Any]:
        """Render the wire envelope."""
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        if self.blocker:
            error["blocker"] = self.blocker
        return {"error": error}


class ExitAlreadyInProgress(ExitWorkflowError):
    """rules.yaml#EXIT-01, edges.yaml#X-001 — one workflow per contract, ever."""

    http_status = 409
    code = EXIT_ALREADY_IN_PROGRESS

    def __init__(self, workflow_id: str) -> None:
        super().__init__(
            "An exit workflow already exists for this contract.",
            details={"workflow_id": workflow_id},
        )
        self.workflow_id = workflow_id


class ExitWorkflowIncomplete(ExitWorkflowError):
    """rules.yaml#EXIT-03 (BR-1), edges.yaml#X-006 — exit lock blocks new contracts."""

    http_status = 409
    code = EXIT_WORKFLOW_INCOMPLETE


class WrongState(ExitWorkflowError):
    """api.yaml — the workflow is not in a state that permits this call."""

    http_status = 409
    code = WRONG_STATE

    def __init__(
        self,
        message: str,
        *,
        current: str | None = None,
        expected: str | list[str] | None = None,
    ) -> None:
        details: dict[str, Any] = {}
        if current is not None:
            details["current_status"] = current
        if expected is not None:
            details["expected_status"] = expected
        super().__init__(message, details=details)


class ForbiddenTransition(WrongState):
    """states.yaml#forbidden.

    AGENTS.md: "A forbidden transition raises, never silently no-ops." Reaching
    one means a caller or a code path tried to skip a mandatory step, so it is
    logged at CRITICAL by the API layer in addition to being refused.
    """


class MoveOutDateInPast(ExitWorkflowError):
    """rules.yaml#EXIT-02, edges.yaml#X-007 — Asia/Dubai calendar day."""

    http_status = 422
    code = MOVE_OUT_DATE_IN_PAST


class ReasonInvalid(ExitWorkflowError):
    """rules.yaml#EXIT-02 — reason must come from the reference list."""

    http_status = 422
    code = REASON_INVALID


class DocumentsRequired(ExitWorkflowError):
    """rules.yaml#EXIT-02 — at least one document."""

    http_status = 422
    code = DOCUMENTS_REQUIRED


class PaymentPending(ExitWorkflowError):
    """rules.yaml#EXIT-08, edges.yaml#X-004 — hold until the gateway says SUCCEEDED."""

    http_status = 409
    code = PAYMENT_PENDING
    preserves_transaction = True


class UndefinedErrorCode(ExitWorkflowError):
    """A branch whose HTTP status the kit fixes but whose code string it omits.

    Used only where the behaviour is specified and the code is not. The blocker
    ID travels in the envelope so the caller can see exactly which decision is
    outstanding. See blockers.md#B-4 and #B-6.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        blocker: str,
        preserves_transaction: bool = False,
        **details: Any,
    ) -> None:
        super().__init__(message, details=details or None, blocker=blocker)
        self.http_status = http_status
        self.preserves_transaction = preserves_transaction


class SpecUnresolved(ExitWorkflowError):
    """The specification does not decide this branch — refuse rather than guess.

    AGENTS.md: "If the spec does not answer a question, STOP. ... Do NOT invent
    an answer." Raised with the blocker ID from blockers.md, e.g.
    ``SpecUnresolved("R8")``.
    """

    http_status = 501

    def __init__(self, blocker_id: str, message: str | None = None, **details: Any) -> None:
        # api.yaml defines a code for R8 only; other blockers have none, and one
        # may not be invented for them.
        self.code = SPEC_UNRESOLVED_R8 if blocker_id == "R8" else None
        super().__init__(
            message or f"Blocked on unresolved specification item {blocker_id}; see blockers.md.",
            details=details or None,
            blocker=blocker_id,
        )
        self.blocker_id = blocker_id


class AuthorizationError(ExitWorkflowError):
    """api.yaml ``authz`` line not satisfied. No code defined in the kit."""

    http_status = 403


class WorkflowNotFound(ExitWorkflowError):
    """Unknown workflow ID. No code defined in the kit."""

    http_status = 404
