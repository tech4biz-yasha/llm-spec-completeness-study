"""Loader for the specification kit.

The kit files are the contract (AGENTS.md). They are read at import time rather
than transcribed into Python so that the code cannot drift from them: if
states.yaml gains a transition or api.yaml gains an error code, this module sees
it. If a kit file is missing or malformed the module refuses to import — a
half-loaded contract is worse than no module.

Location resolution order:
1. ``EXIT_SPEC_DIR`` environment variable (deployment: ship the kit alongside the
   service and point at it).
2. The repository root, i.e. the parent of the ``exit_workflow`` package.
"""

from __future__ import annotations

import os
import re
from functools import cache
from pathlib import Path
from typing import Any

import yaml

#: Files that must be present for the module to operate.
REQUIRED_SPEC_FILES = ("states.yaml", "rules.yaml", "api.yaml", "edges.yaml")

#: api.yaml is written in a YAML-like shorthand rather than strict YAML — a flow
#: mapping such as ``{contract_id, move_out_date, reason, documents[]}`` does not
#: parse, because ``documents[]`` is not a YAML token. The file is the contract
#: and is not edited to suit this module, so the one machine-readable thing it
#: needs to give up — the error code list — is extracted directly instead.
_ERROR_CODES_BLOCK = re.compile(r"^_error_codes:\s*\[(?P<body>[^\]]*)\]", re.MULTILINE)
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class SpecLoadError(RuntimeError):
    """The specification kit is absent, unreadable, or malformed."""


@cache
def spec_dir() -> Path:
    """Return the directory holding the specification kit."""
    override = os.environ.get("EXIT_SPEC_DIR")
    candidate = Path(override) if override else Path(__file__).resolve().parents[2]

    missing = [name for name in REQUIRED_SPEC_FILES if not (candidate / name).is_file()]
    if missing:
        raise SpecLoadError(
            f"specification kit incomplete at {candidate}: missing {', '.join(missing)}. "
            "Set EXIT_SPEC_DIR to the directory containing the kit."
        )
    return candidate


@cache
def load(filename: str) -> Any:
    """Parse and return one kit file."""
    path = spec_dir() / filename
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except OSError as exc:  # pragma: no cover - filesystem failure
        raise SpecLoadError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SpecLoadError(f"cannot parse {path}: {exc}") from exc


@cache
def api_error_codes() -> tuple[str, ...]:
    """Return ``api.yaml#_error_codes`` in declaration order.

    Read from the raw text because api.yaml is not strict YAML (see
    :data:`_ERROR_CODES_BLOCK`). Anything that does not look like an error code
    is a parse failure rather than a silently dropped entry: a code missing from
    this list becomes an import-time error in
    :mod:`exit_workflow.domain.errors`, which is the mechanism that stops codes
    being invented.
    """
    text = (spec_dir() / "api.yaml").read_text(encoding="utf-8")
    match = _ERROR_CODES_BLOCK.search(text)
    if match is None:
        raise SpecLoadError("api.yaml does not declare an _error_codes list")

    codes = tuple(token.strip() for token in match.group("body").split(",") if token.strip())
    if not codes:
        raise SpecLoadError("api.yaml#_error_codes is empty")

    malformed = [code for code in codes if not _ERROR_CODE.match(code)]
    if malformed:
        raise SpecLoadError(f"api.yaml#_error_codes contains unparseable entries: {malformed}")
    return codes


@cache
def rules() -> dict[str, str]:
    """Return ``{rule_id: rule_text}`` from rules.yaml."""
    raw = load("rules.yaml")
    if not isinstance(raw, list):
        raise SpecLoadError("rules.yaml must be a list of rule objects")
    return {entry["id"]: entry["rule"] for entry in raw}


@cache
def edges() -> dict[str, dict[str, Any]]:
    """Return ``{edge_id: edge}`` from edges.yaml."""
    raw = load("edges.yaml")
    if not isinstance(raw, list):
        raise SpecLoadError("edges.yaml must be a list of edge cases")
    return {entry["id"]: entry for entry in raw}


def rule_text(rule_id: str) -> str:
    """Return the text of a rule, for embedding in operator-facing messages."""
    try:
        return " ".join(rules()[rule_id].split())
    except KeyError:
        raise SpecLoadError(f"unknown rule id {rule_id!r}; not present in rules.yaml") from None
