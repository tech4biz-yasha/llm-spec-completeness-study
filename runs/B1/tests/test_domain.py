"""Unit tests for the spec-driven domain layer."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from exit_workflow.domain import spec
from exit_workflow.domain.clock import DUBAI, FixedClock, today_dubai
from exit_workflow.domain.errors import (
    ALLOWED_ERROR_CODES,
    ForbiddenTransition,
    ReasonInvalid,
    SpecUnresolved,
    WrongState,
)
from exit_workflow.domain.ids import MAX_SEQUENCE, format_workflow_id, is_workflow_id
from exit_workflow.domain.money import MoneyError, format_aed, from_minor, quantize, to_minor
from exit_workflow.domain.reasons import ConfiguredExitReasons, validate_reason
from exit_workflow.domain.states import State, exit_workflow_machine


# --- states.yaml --------------------------------------------------------------


def test_state_enum_matches_states_yaml():
    declared = spec.load("states.yaml")["exit_workflow"]["states"]
    assert sorted(declared) == sorted(member.value for member in State)


def test_every_transition_in_states_yaml_is_accepted():
    machine = exit_workflow_machine()
    for transition in machine.transitions:
        assert machine.check(transition.source, transition.target) is transition


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (State.INITIATED, State.COMPLETE),
        (State.DOCS_SUBMITTED, State.REFUND_PROCESSED),
        (State.INSPECTION_DONE, State.REFUND_PROCESSED),
        (State.STALLED, State.COMPLETE),
    ],
)
def test_forbidden_transitions_raise(source, target):
    with pytest.raises(ForbiddenTransition):
        exit_workflow_machine().check(source, target)


@pytest.mark.parametrize(
    "source",
    [s for s in State if s is not State.REFUND_PROCESSED],
)
def test_noc_only_reachable_from_refund_processed(source):
    """states.yaml: "any -> NOC_ISSUED without REFUND_PROCESSED" (T13 order)."""
    with pytest.raises(ForbiddenTransition):
        exit_workflow_machine().check(source, State.NOC_ISSUED)

    assert exit_workflow_machine().check(State.REFUND_PROCESSED, State.NOC_ISSUED)


def test_undefined_transition_raises_wrong_state():
    with pytest.raises(WrongState):
        exit_workflow_machine().check(State.INITIATED, State.INSPECTION_DONE)


def test_stalled_is_terminal_for_want_of_a_defined_exit():
    """blockers.md#B-2 — states.yaml gives STALLED no outgoing edge."""
    machine = exit_workflow_machine()
    assert machine.outgoing(State.STALLED) == ()
    assert machine.is_terminal(State.STALLED)
    assert machine.is_terminal(State.COMPLETE)


def test_forbidden_rule_with_unsupported_grammar_is_rejected():
    """An unparseable prohibition must not silently become a permission."""
    from exit_workflow.domain.states import _parse_forbidden

    with pytest.raises(spec.SpecLoadError):
        _parse_forbidden("INITIATED => COMPLETE")


# --- api.yaml -----------------------------------------------------------------


def test_error_codes_come_from_api_yaml():
    assert ALLOWED_ERROR_CODES == frozenset(spec.api_error_codes())
    assert "SPEC_UNRESOLVED_R8" in ALLOWED_ERROR_CODES


def test_no_invented_error_code_can_be_declared():
    from exit_workflow.domain.errors import _code

    with pytest.raises(spec.SpecLoadError):
        _code("EXIT_SOMETHING_PLAUSIBLE")


# --- Money (AGENTS.md: Decimal, minor units, AED, 2 dp, never float) ----------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("0.00", 0), ("0.01", 1), ("12000.00", 1200000), ("1500.55", 150055), ("0.005", 1)],
)
def test_to_minor_rounds_half_up(amount, expected):
    assert to_minor(Decimal(amount)) == expected


def test_float_is_refused_as_money():
    with pytest.raises(MoneyError):
        to_minor(1500.55)


def test_negative_amounts_are_refused():
    with pytest.raises(MoneyError):
        to_minor(Decimal("-0.01"))


def test_round_trip_through_minor_units():
    for amount in ("0.00", "0.01", "999999.99", "12000.00"):
        assert from_minor(to_minor(Decimal(amount))) == Decimal(amount)


def test_quantize_is_half_up_not_bankers():
    assert quantize(Decimal("2.345")) == Decimal("2.35")
    assert quantize(Decimal("2.355")) == Decimal("2.36")


def test_format_aed():
    assert format_aed(1200000) == "AED 12,000.00"


# --- Identifiers (rules.yaml#EXIT-02) -----------------------------------------


def test_workflow_id_format():
    workflow_id = format_workflow_id(date(2026, 8, 8), 42)
    assert workflow_id == "EX-20260808-00042"
    assert is_workflow_id(workflow_id)


def test_workflow_id_sequence_overflow_is_blocked():
    """blockers.md#B-3 — the five-digit field's exhaustion behaviour is undecided."""
    with pytest.raises(SpecUnresolved) as raised:
        format_workflow_id(date(2026, 8, 8), MAX_SEQUENCE + 1)
    assert raised.value.blocker_id == "B-3"


# --- Time (decision D-001, edges.yaml#X-007) ----------------------------------


def test_today_is_the_dubai_calendar_day():
    # 21:00 UTC is already the next day in Dubai (UTC+4).
    clock = FixedClock(datetime(2026, 8, 8, 21, 0, tzinfo=UTC))
    assert today_dubai(clock) == date(2026, 8, 9)
    assert clock.now_utc().date() == date(2026, 8, 8)


def test_dubai_offset_is_four_hours_year_round():
    """The UAE observes no daylight saving, so the window never shifts."""
    for month in (1, 7):
        moment = datetime(2026, month, 15, 12, 0, tzinfo=DUBAI)
        assert moment.utcoffset().total_seconds() == 4 * 3600


# --- Exit reasons (blockers.md#B-1) -------------------------------------------


def test_unpublished_reason_list_blocks_rather_than_guesses():
    with pytest.raises(SpecUnresolved) as raised:
        validate_reason("ANYTHING", ConfiguredExitReasons(None))
    assert raised.value.blocker_id == "B-1"


def test_reason_outside_a_published_list_is_invalid():
    reference = ConfiguredExitReasons(["A", "B"])
    assert validate_reason("A", reference) == "A"
    with pytest.raises(ReasonInvalid):
        validate_reason("C", reference)


def test_empty_configured_list_counts_as_unpublished():
    """An empty list is a deployment that has not configured the vocabulary."""
    assert ConfiguredExitReasons([]).codes() is None
