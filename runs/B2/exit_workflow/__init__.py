"""Tenant exit workflow module.

Implements rules.yaml EXIT-01..EXIT-10 over the states.yaml state machine and the
api.yaml HTTP contract. Every open question the specification leaves is listed in
blockers.md and raises SpecUnresolved at the point of use (AGENTS.md).
"""

from .errors import ErrorCode, ExitWorkflowError, SpecUnresolved
from .domain.states import Actor, State

__all__ = ["Actor", "ErrorCode", "ExitWorkflowError", "SpecUnresolved", "State"]
