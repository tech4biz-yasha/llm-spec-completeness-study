"""Tenant exit workflow module.

Every behaviour in this package traces to a file in the specification kit at the
repository root: AGENTS.md, algorithm.md, api.yaml, edges.yaml, rules.yaml,
states.yaml, risks.md, blockers.md.

Branches carry the rule ID that decides them, e.g. ``# rules.yaml#EXIT-03``.
Questions the kit does not answer are recorded in blockers.md and raise
:class:`exit_workflow.domain.errors.SpecUnresolved` at the point of decision.
They are never guessed.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
