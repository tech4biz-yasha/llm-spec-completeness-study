"""Exit reason reference data. rules.yaml#EXIT-02 requires "a reason from the reference
list".

The list itself does not exist yet: risks.md carries "Reference data dictionary,
specifically **exit reasons** (blocks the ExitWorkflow enum)" as an open item from SRS
Appendix A. AGENTS.md says gaps listed in risks.md are not ours to resolve, so this module
defines no reason values. The deployment must supply them; ``build_app`` refuses to start
without a reference source rather than defaulting to a guessed list.
See blockers.md#B-2.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ExitReasonReference(Protocol):
    def codes(self) -> frozenset[str]:
        """The currently valid exit reason codes."""

    def is_valid(self, code: str) -> bool:
        """True when ``code`` is in the reference list. rules.yaml#EXIT-02."""


class StaticExitReasonReference:
    """Reference list supplied by the deployment (config, seed file, reference service).

    Constructing it with an empty list raises: an empty reference list would reject every
    initiation with REASON_INVALID and disguise missing reference data as tenant error.
    """

    def __init__(self, codes: frozenset[str] | set[str] | list[str]) -> None:
        normalized = frozenset(codes)
        if not normalized:
            raise ValueError(
                "exit reason reference list is empty; supply the reference data "
                "(risks.md Appendix A, blockers.md#B-2)"
            )
        self._codes = normalized

    def codes(self) -> frozenset[str]:
        return self._codes

    def is_valid(self, code: str) -> bool:
        return code in self._codes
