"""Pure-domain tests: money, refund arithmetic, ids, the 30-day window and the
state machine. No database, no HTTP."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from exit_workflow.clock import DUBAI, UTC, FrozenClock
from exit_workflow.domain import rules
from exit_workflow.domain.states import Actor, State, load_machine, terminal_states
from exit_workflow.errors import (
    DocumentsRequired,
    ForbiddenTransition,
    MoveOutDateInPast,
    ReasonInvalid,
    SpecUnresolved,
    WrongState,
)
from exit_workflow.money import MoneyError, from_minor, quantize, to_minor


# --- money (AGENTS.md: Decimal, minor units, AED, 2 dp, never float) --------


def test_money_round_trips_in_minor_units():
    assert to_minor(Decimal("5000.00")) == 500000
    assert from_minor(500000) == Decimal("5000.00")
    assert str(from_minor(1)) == "0.01"


def test_money_rounds_half_up():
    # rules.yaml#EXIT-07 — half-up, not banker's rounding.
    assert quantize(Decimal("1.005")) == Decimal("1.01")
    assert quantize(Decimal("1.015")) == Decimal("1.02")
    assert quantize(Decimal("2.675")) == Decimal("2.68")


def test_money_refuses_float():
    with pytest.raises(MoneyError):
        to_minor(1.5)  # type: ignore[arg-type]


# --- rules.yaml#EXIT-07 -----------------------------------------------------


def test_refund_is_deposit_minus_damage():
    assert rules.refund_minor(500000, 123456) == 376544


def test_refund_is_zero_when_damage_equals_deposit():
    assert rules.refund_minor(500000, 500000) == 0


def test_damage_above_deposit_raises_r8():
    # edges.yaml#X-003 / risks.md#R8 — undecided, must not be resolved in code.
    with pytest.raises(SpecUnresolved) as raised:
        rules.refund_minor(500000, 500001)
    assert raised.value.item == "R8"
    assert raised.value.code == "SPEC_UNRESOLVED_R8"
    assert raised.value.http_status == 501


# --- rules.yaml#EXIT-02 -----------------------------------------------------


def test_workflow_id_format():
    assert rules.format_workflow_id(date(2026, 3, 2), 42) == "EX-20260302-00042"
    assert rules.WORKFLOW_ID_PATTERN.match(rules.format_workflow_id(date(2026, 3, 2), 99999))


def test_workflow_id_refuses_to_wrap_past_the_sequence_width():
    with pytest.raises(SpecUnresolved) as raised:
        rules.format_workflow_id(date(2026, 3, 2), 100000)
    assert raised.value.item == "B-008"


def test_move_out_date_must_not_be_in_the_past():
    with pytest.raises(MoveOutDateInPast):
        rules.validate_move_out_date(date(2026, 3, 1), date(2026, 3, 2))
    rules.validate_move_out_date(date(2026, 3, 2), date(2026, 3, 2))  # today is fine


def test_reason_without_a_reference_list_is_blocked():
    # risks.md Appendix A — the exit reason list does not exist yet.
    with pytest.raises(SpecUnresolved) as raised:
        rules.validate_reason("ANYTHING", None)
    assert raised.value.item == "B-001"


def test_reason_must_come_from_the_reference_list():
    with pytest.raises(ReasonInvalid):
        rules.validate_reason("NOT_LISTED", {"LISTED"})
    rules.validate_reason("LISTED", {"LISTED"})


def test_documents_are_required():
    with pytest.raises(DocumentsRequired):
        rules.validate_documents([])
    rules.validate_documents([{"document_id": "d1"}])


# --- rules.yaml#EXIT-05 -----------------------------------------------------


def test_stall_window_is_thirty_days_past_move_out():
    move_out = date(2026, 3, 2)
    assert rules.stall_deadline(move_out) == date(2026, 4, 1)
    assert not rules.is_past_stall_window(move_out, date(2026, 4, 1))
    assert rules.is_past_stall_window(move_out, date(2026, 4, 2))


# --- D-001 / edges.yaml#X-007 ----------------------------------------------


def test_dubai_calendar_day_differs_from_utc_day():
    clock = FrozenClock(datetime(2026, 3, 1, 20, 0, tzinfo=UTC))
    assert clock.now_utc().date() == date(2026, 3, 1)
    assert clock.today_dubai() == date(2026, 3, 2)
    assert clock.now_utc().astimezone(DUBAI).hour == 0


# --- states.yaml ------------------------------------------------------------


def test_machine_matches_the_spec_file():
    machine = load_machine()
    assert machine.initial is State.INITIATED
    assert machine.states == frozenset(State)
    assert len(machine.transitions) == 10


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
    # states.yaml#forbidden — raises, never a silent no-op (AGENTS.md).
    with pytest.raises(ForbiddenTransition):
        load_machine().validate(source, target, Actor.SYSTEM)


@pytest.mark.parametrize(
    "source",
    [State.INITIATED, State.DAMAGE_CONFIRMED, State.INSPECTION_DONE, State.STALLED],
)
def test_noc_requires_refund_processed(source):
    # states.yaml: any -> NOC_ISSUED without REFUND_PROCESSED (T13 order, EXIT-08).
    with pytest.raises(ForbiddenTransition):
        load_machine().validate(source, State.NOC_ISSUED, Actor.SYSTEM)


def test_declared_transitions_are_allowed():
    machine = load_machine()
    for transition in machine.transitions:
        assert machine.validate(transition.source, transition.target, transition.actor)


def test_wrong_actor_is_rejected():
    # states.yaml: DOCS_SUBMITTED -> OWNER_NOTIFIED is actor `system`.
    with pytest.raises(WrongState):
        load_machine().validate(State.DOCS_SUBMITTED, State.OWNER_NOTIFIED, Actor.TENANT)


def test_inspection_agency_is_the_inspector_actor():
    # api.yaml calls it inspection_agency; states.yaml calls it inspector.
    assert load_machine().validate(
        State.INSPECTION_SCHEDULED, State.INSPECTION_DONE, Actor.INSPECTION_AGENCY
    )


def test_stalled_has_no_way_out():
    # blockers.md#B-003 — recorded, not resolved.
    assert State.STALLED in terminal_states()
    assert State.COMPLETE in terminal_states()


def test_undeclared_transition_is_wrong_state():
    with pytest.raises(WrongState):
        load_machine().validate(State.OWNER_NOTIFIED, State.DAMAGE_CONFIRMED, Actor.OWNER)


# --- rules.yaml#EXIT-09 (renderer) -----------------------------------------


def test_noc_renderer_produces_a_pdf():
    from exit_workflow.adapters.noc_pdf import NocPdfRenderer
    from exit_workflow.ports import NocContext

    context = NocContext(
        workflow_id="EX-20260302-00001",
        contract_id="c",
        property_id="p",
        tenant_id="t",
        owner_id="o",
        move_out_date=date(2026, 3, 2),
        security_deposit=Decimal("5000.00"),
        confirmed_damage=Decimal("1234.56"),
        refund_amount=Decimal("3765.44"),
        currency="AED",
        payment_reference="gw-EX-20260302-00001",
        issued_at_dubai="2026-03-02T00:00:00+04:00",
    )
    pdf = NocPdfRenderer().render(context)
    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert b"EX-20260302-00001" in pdf
    assert b"3765.44" in pdf
    # Deterministic for a given context, so the stored sha256 means something.
    assert NocPdfRenderer().render(context) == pdf


def test_uae_region_is_enforced_for_the_noc_bucket():
    from exit_workflow.config import Settings

    with pytest.raises(ValueError, match="UAE region"):
        Settings(noc_region="eu-west-1")


def test_stall_window_constant_is_thirty_days():
    assert rules.STALL_WINDOW == timedelta(days=30)
