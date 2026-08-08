"""The module's copies of the kit must be the kit, and the code must agree with them.

AGENTS.md: "The spec files in this folder are the contract. Every line you write must
trace to an item in them."
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from exit_workflow.domain.states import state_machine
from exit_workflow.enums import Actor, WorkflowState
from exit_workflow.errors import ERROR_CODES
from exit_workflow.spec import SPEC_DIR

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "exit_workflow"
COPIED = ["states.yaml", "rules.yaml", "edges.yaml", "api.yaml"]


@pytest.mark.parametrize("name", COPIED)
def test_packaged_spec_is_byte_identical_to_the_kit(name):
    assert (SPEC_DIR / name).read_bytes() == (ROOT / name).read_bytes()


def test_states_match_states_yaml():
    machine = yaml.safe_load((ROOT / "states.yaml").read_text())["exit_workflow"]
    assert {s.value for s in WorkflowState} == set(machine["states"])
    assert state_machine().initial is WorkflowState(machine["initial"])
    assert len(state_machine().transitions) == len(machine["transitions"])
    assert len(state_machine().forbidden) == len(machine["forbidden"])


def test_error_codes_match_api_yaml():
    from exit_workflow.spec import error_codes

    declared = error_codes.__wrapped__(ROOT / "api.yaml")
    assert set(ERROR_CODES) == set(declared)
    assert len(declared) == 8


def test_every_rule_id_is_cited_somewhere_in_the_source():
    """AGENTS.md, Definition of done: "every function implementing a rule cites its ID"."""
    rules = yaml.safe_load((ROOT / "rules.yaml").read_text())
    source = "\n".join(path.read_text() for path in SRC.rglob("*.py"))
    for rule in rules:
        assert rule["id"] in source, f"{rule['id']} is not cited anywhere in the module"


def test_every_edge_case_id_is_cited_somewhere():
    edges = yaml.safe_load((ROOT / "edges.yaml").read_text())
    source = "\n".join(path.read_text() for path in SRC.rglob("*.py"))
    tests = "\n".join(path.read_text() for path in (ROOT / "tests").rglob("*.py"))
    for edge in edges:
        assert edge["id"] in source or edge["id"] in tests, f"{edge['id']} is not cited"


def test_every_edge_case_has_the_test_named_in_the_kit():
    edges = yaml.safe_load((ROOT / "edges.yaml").read_text())
    tests = "\n".join(path.read_text() for path in (ROOT / "tests").rglob("*.py"))
    for edge in edges:
        assert f"def {edge['test']}(" in tests, f"missing {edge['test']} for {edge['id']}"


def test_no_todo_fixme_or_assumed_markers():
    """AGENTS.md, Definition of done: "zero TODO / FIXME / assumed markers"."""
    pattern = re.compile(r"\b(TODO|FIXME|XXX|HACK|assumed|ASSUMED)\b")
    offenders = []
    for path in list(SRC.rglob("*.py")) + list((ROOT / "migrations").rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, offenders


def test_actor_roles_cover_states_yaml_and_api_yaml():
    machine = yaml.safe_load((ROOT / "states.yaml").read_text())["exit_workflow"]
    for transition in machine["transitions"]:
        assert Actor(transition["actor"])
    # api.yaml spells the inspector role differently; the alias is explicit.
    assert Actor.normalize("inspection_agency") is Actor.INSPECTOR


def test_forbidden_rules_all_parsed():
    machine = yaml.safe_load((ROOT / "states.yaml").read_text())["exit_workflow"]
    parsed = {rule.raw for rule in state_machine().forbidden}
    assert parsed == {entry.strip() for entry in machine["forbidden"]}
