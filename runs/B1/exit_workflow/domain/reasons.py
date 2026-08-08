"""Exit reason reference data.

rules.yaml#EXIT-02 requires "a reason from the reference list" and api.yaml
defines ``REASON_INVALID`` for a reason outside it. The list itself is *not in
the kit*: risks.md, "Open items also flagged in the SRS itself", records
"Reference data dictionary, specifically **exit reasons** (blocks the
ExitWorkflow enum)" as pending.

So the validation mechanism is implemented here in full and the vocabulary is
not. Writing a plausible list ("END_OF_TENANCY", "RELOCATION", ...) would be
inventing the answer AGENTS.md forbids, and would silently accept or reject real
tenants' submissions on made-up grounds. Until the dictionary is published, the
reference list is supplied by configuration; when it is absent, initiation
raises :class:`SpecUnresolved` for blockers.md#B-1 rather than guessing.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from exit_workflow.domain.errors import ReasonInvalid, SpecUnresolved


@runtime_checkable
class ExitReasonReference(Protocol):
    """Source of the permitted exit reason codes."""

    def codes(self) -> frozenset[str] | None:
        """Return the permitted codes, or ``None`` when not yet published."""


class ConfiguredExitReasons:
    """Reference list supplied by deployment configuration.

    Ships empty. The operator populates it from the reference data dictionary
    once that dictionary exists.
    """

    __slots__ = ("_codes",)

    def __init__(self, codes: object | None = None) -> None:
        if codes is None:
            self._codes: frozenset[str] | None = None
        else:
            normalised = frozenset(str(code).strip() for code in codes if str(code).strip())
            self._codes = normalised or None

    def codes(self) -> frozenset[str] | None:
        return self._codes


def validate_reason(reason: str, reference: ExitReasonReference) -> str:
    """Check ``reason`` against the reference list (rules.yaml#EXIT-02).

    :raises SpecUnresolved: the reference list has not been published (B-1).
    :raises ReasonInvalid: the reason is not in the published list.
    """
    permitted = reference.codes()
    if permitted is None:
        # risks.md, Appendix A open items — exit reason vocabulary undecided.
        raise SpecUnresolved(
            "B-1",
            "The exit reason reference list is not published, so 'reason from the reference "
            "list' (rules.yaml#EXIT-02) cannot be evaluated. See risks.md open items and "
            "blockers.md#B-1.",
            submitted_reason=reason,
        )

    candidate = reason.strip()
    if candidate not in permitted:
        raise ReasonInvalid(
            "Reason is not in the exit reason reference list.",
            details={"submitted_reason": candidate, "permitted_reasons": sorted(permitted)},
        )
    return candidate
