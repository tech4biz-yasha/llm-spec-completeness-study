"""Exit reason reference list — rules.yaml#EXIT-02.

risks.md, Appendix A: "Reference data dictionary, specifically **exit reasons**
(blocks the ExitWorkflow enum)". The list is not defined, so this module ships
no values. The default source returns ``None``, which makes initiation raise
SpecUnresolved (blockers.md#B-001) instead of accepting or rejecting a reason on
a guess.

Once the reference data dictionary exists, point ``StaticExitReasonReference`` at
it (or add a source that reads the reference-data service) and nothing else in
the module changes.
"""

from __future__ import annotations

from collections.abc import Collection


class UndefinedExitReasonReference:
    """The current, honest state of the world: no list exists."""

    async def exit_reasons(self) -> Collection[str] | None:
        return None  # blockers.md#B-001


class StaticExitReasonReference:
    """A list supplied by configuration or by the reference-data module."""

    def __init__(self, reasons: Collection[str]) -> None:
        if not reasons:
            raise ValueError("exit reason reference list cannot be empty")
        self._reasons = frozenset(reasons)

    async def exit_reasons(self) -> Collection[str] | None:
        return self._reasons
