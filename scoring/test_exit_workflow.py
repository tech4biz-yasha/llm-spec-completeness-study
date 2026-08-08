"""
Acceptance tests — the fixed yardstick for BOTH runs.

Written against behaviour, not implementation. Each test names the SRS/kit item
it checks. Adapt the import block to the generated code's module layout; change
NOTHING below the imports.

Run A code will fail the tests whose behaviour the SRS never specified — that is
the point, record it, do not fix the code.
"""
import pytest
from decimal import Decimal

# ---- ADAPT THIS BLOCK PER RUN ------------------------------------------------
# from app.exit_workflow import (initiate_exit, schedule_inspection,
#     submit_inspection_report, confirm_damage, settle, ExitError)
# ------------------------------------------------------------------------------


class TestInitiation:
    def test_active_contract_required(self, inactive_contract, tenant):
        """T13: exit starts from an active tenancy."""
        with pytest.raises(Exception):
            initiate_exit(tenant, inactive_contract, valid_payload())

    def test_duplicate_initiation_rejected_with_existing_id(self, active_contract, tenant):
        """X-001 / EXIT-01: second initiation returns the existing workflow, never a second one."""
        w1 = initiate_exit(tenant, active_contract, valid_payload())
        with pytest.raises(Exception) as e:
            initiate_exit(tenant, active_contract, valid_payload())
        assert str(w1.workflow_id) in str(e.value)

    def test_past_move_out_date_rejected(self, active_contract, tenant):
        """EXIT-02: move_out_date must be today or later (Asia/Dubai calendar)."""
        with pytest.raises(Exception):
            initiate_exit(tenant, active_contract, valid_payload(move_out="2020-01-01"))

    def test_exit_lock_set_on_initiation(self, active_contract, tenant, db):
        """BR-1 / EXIT-03: property is locked for new contracts the moment exit starts."""
        initiate_exit(tenant, active_contract, valid_payload())
        assert property_locked(db, active_contract.property_id) is True


class TestSettlement:
    def test_refund_is_deposit_minus_damage(self, workflow_damage_confirmed):
        """T13 verbatim: 'deposit refund (deposit minus damage)'."""
        r = settle(workflow_damage_confirmed(deposit="10000.00", damage="2500.00"))
        assert r.refund_amount == Decimal("7500.00")

    def test_refund_never_negative_auto(self, workflow_damage_confirmed):
        """EXIT-07: damage > deposit must NOT silently produce a negative or zero refund.
        Correct behaviour per kit: raise/refuse (SpecUnresolved). A silent 0.00 or a
        negative number are both failures."""
        w = workflow_damage_confirmed(deposit="10000.00", damage="14000.00")
        with pytest.raises(Exception):
            settle(w)

    def test_noc_only_after_payment_succeeded(self, workflow_refund_pending):
        """EXIT-08 / X-004: NOC while the refund is PENDING must be refused."""
        with pytest.raises(Exception):
            issue_noc(workflow_refund_pending)

    def test_settlement_idempotent(self, workflow_damage_confirmed):
        """X-005: two settle calls, one payment."""
        w = workflow_damage_confirmed(deposit="10000.00", damage="0.00")
        r1 = settle(w); r2 = settle(w)
        assert r1.payment_id == r2.payment_id

    def test_damage_needs_owner_confirmation(self, workflow_inspection_done):
        """EXIT-06: settlement straight from the agency report, without owner
        confirmation, must be refused."""
        with pytest.raises(Exception):
            settle(workflow_inspection_done)


class TestCompletion:
    def test_complete_releases_lock(self, workflow_noc_issued, db):
        """EXIT-09: COMPLETE and lock release happen together."""
        w = complete(workflow_noc_issued)
        assert w.status == "COMPLETE"
        assert property_locked(db, w.property_id) is False

    def test_forbidden_shortcut_blocked(self, fresh_workflow):
        """states.yaml forbidden: INITIATED -> COMPLETE must raise."""
        with pytest.raises(Exception):
            force_transition(fresh_workflow, "COMPLETE")

    def test_money_is_decimal(self, workflow_damage_confirmed):
        """AGENTS.md: float money is an automatic fail."""
        r = settle(workflow_damage_confirmed(deposit="10000.00", damage="333.33"))
        assert isinstance(r.refund_amount, Decimal)
