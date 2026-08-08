"""Verbatim copies of the specification kit, loaded at runtime.

The state machine and the error-code vocabulary read from these files rather than from
hand-transcribed constants, so that behaviour cannot drift from the contract.
``tests/test_spec_conformance.py`` asserts the copies are byte-identical to the kit files
at the repository root.

``api.yaml`` carries shorthand that is not valid YAML (``documents[]`` inside a flow
mapping), so it is read as text; ``error_codes`` extracts the ``_error_codes`` list from
it. The other three files parse normally.
"""

from __future__ import annotations

import re
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import yaml

SPEC_DIR = Path(__file__).resolve().parent

_ERROR_CODES_RE = re.compile(r"^_error_codes:\s*\[(?P<body>[^\]]*)\]", re.MULTILINE | re.DOTALL)


@cache
def load(name: str) -> Any:
    """Load and cache a spec document by file name, e.g. ``states.yaml``."""
    path = SPEC_DIR / name
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@lru_cache(maxsize=1)
def error_codes(path: Path | None = None) -> tuple[str, ...]:
    """api.yaml#_error_codes, in declaration order."""
    text = (path or SPEC_DIR / "api.yaml").read_text(encoding="utf-8")
    match = _ERROR_CODES_RE.search(text)
    if match is None:  # pragma: no cover - guards a spec edit
        raise RuntimeError("api.yaml no longer declares _error_codes")
    return tuple(code.strip() for code in match.group("body").split(",") if code.strip())


__all__ = ["SPEC_DIR", "error_codes", "load"]
