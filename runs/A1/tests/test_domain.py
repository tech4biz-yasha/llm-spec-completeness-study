"""Unit tests for the pure domain layer: money, settlement maths, the state machine,
token verification and the PDF writer. No database, no HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.pdf import (
    Heading,
    KeyValue,
    Paragraph,
    Rule,
    render_pdf,
    text_width,
    wrap_text,
)
from app.domain.settlement import DeductionInput, compute_settlement
from app.domain.states import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    ExitWorkflowState,
    allowed_transitions,
    assert_can_transition,
    can_transition,
    is_active,
    progress_step,
)
from app.errors import AuthenticationError, InvalidStateTransition, ValidationError
from app.money import Money, MoneyError, aed_to_fils, fils_to_aed, format_aed
from app.security import (
    PrincipalRole,
    decode_token,
    encode_token,
    generate_api_key,
    hash_api_key,
    principal_from_claims,
)

S = ExitWorkflowState
SECRET = "unit-test-secret"


# --- money --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("aed", "fils"),
    [("0", 0), ("1", 100), ("0.01", 1), ("5000.00", 500_000), ("1234.56", 123_456)],
)
def test_aed_to_fils_is_exact(aed: str, fils: int) -> None:
    assert aed_to_fils(aed) == fils
    assert fils_to_aed(fils) == Decimal(aed)


def test_sub_fils_precision_is_rejected() -> None:
    """Silently rounding a tenth of a fil would quietly lose money."""
    with pytest.raises(MoneyError):
        aed_to_fils("10.005")


def test_money_arithmetic_and_currency_guard() -> None:
    assert (Money(500_000) - Money(125_000)).fils == 375_000
    assert Money(-1).clamped_to_zero().fils == 0
    assert str(Money(375_000)) == "3,750.00 AED"
    with pytest.raises(MoneyError):
        Money(100, "AED") + Money(100, "USD")


def test_format_aed_groups_thousands() -> None:
    assert format_aed(1_234_567) == "12,345.67 AED"


# --- settlement (O16) ----------------------------------------------------------------------


def _deductions(*amounts: int) -> list[DeductionInput]:
    return [
        DeductionInput(code=f"C{i}", description="damage", amount_fils=a)
        for i, a in enumerate(amounts)
    ]


def test_refund_is_deposit_minus_damage() -> None:
    result = compute_settlement(deposit_fils=500_000, deductions=_deductions(100_000, 25_000))
    assert result.total_deductions_fils == 125_000
    assert result.refund_fils == 375_000
    assert result.balance_due_fils == 0
    assert result.tenant_owes is False


def test_damage_exceeding_deposit_clamps_refund_and_records_balance() -> None:
    result = compute_settlement(deposit_fils=500_000, deductions=_deductions(620_000))
    assert result.refund_fils == 0
    assert result.balance_due_fils == 120_000
    assert result.tenant_owes is True


def test_exactly_consumed_deposit_leaves_nothing_on_either_side() -> None:
    result = compute_settlement(deposit_fils=500_000, deductions=_deductions(500_000))
    assert result.refund_fils == 0
    assert result.balance_due_fils == 0
    assert result.tenant_owes is False


def test_no_damage_returns_the_whole_deposit() -> None:
    result = compute_settlement(deposit_fils=500_000, deductions=[])
    assert result.refund_fils == 500_000
    assert result.as_display()["refund"] == "5,000.00 AED"


@pytest.mark.parametrize(
    ("deposit", "damages"),
    [(0, 0), (0, 5), (1, 0), (999_999_999, 1), (1, 999_999_999)],
)
def test_settlement_books_always_balance(deposit: int, damages: int) -> None:
    result = compute_settlement(deposit_fils=deposit, deductions=_deductions(damages))
    assert result.refund_fils - result.balance_due_fils == deposit - damages
    assert result.refund_fils >= 0 and result.balance_due_fils >= 0
    assert result.refund_fils == 0 or result.balance_due_fils == 0


def test_negative_amounts_are_refused() -> None:
    with pytest.raises(ValidationError):
        DeductionInput(code="X", description="d", amount_fils=-1)
    with pytest.raises(ValidationError):
        compute_settlement(deposit_fils=-1, deductions=[])


# --- state machine -------------------------------------------------------------------------


def test_terminal_states_have_no_exits() -> None:
    for state in TERMINAL_STATES:
        assert allowed_transitions(state) == frozenset()
        assert not is_active(state)


def test_active_and_terminal_states_partition_the_enum() -> None:
    assert ACTIVE_STATES | TERMINAL_STATES == set(ExitWorkflowState)
    assert ACTIVE_STATES & TERMINAL_STATES == set()


def test_the_happy_path_is_walkable() -> None:
    path = [
        S.DOCUMENTS_PENDING,
        S.PENDING_OWNER_APPROVAL,
        S.OWNER_APPROVED,
        S.INSPECTION_SCHEDULING,
        S.INSPECTION_SCHEDULED,
        S.INSPECTION_COMPLETED,
        S.DAMAGE_REVIEW,
        S.PENDING_SETTLEMENT,
        S.SETTLED,
        S.NOC_ISSUED,
        S.COMPLETED,
    ]
    current = S.DRAFT
    for target in path:
        assert can_transition(current, target), f"{current} -> {target}"
        current = target
    assert current is S.COMPLETED


def test_every_state_is_reachable_from_draft() -> None:
    seen = {S.DRAFT}
    frontier = [S.DRAFT]
    while frontier:
        for nxt in allowed_transitions(frontier.pop()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(ExitWorkflowState)


def test_money_cannot_be_unwound_by_cancellation() -> None:
    """Once SETTLED, the only way out is forward — cancelling would strand a payment."""
    for state in (S.SETTLED, S.NOC_ISSUED, S.COMPLETED):
        assert S.CANCELLED not in allowed_transitions(state)


def test_invalid_transition_reports_the_legal_alternatives() -> None:
    with pytest.raises(InvalidStateTransition) as exc:
        assert_can_transition(S.DRAFT, S.SETTLED)
    details = exc.value.details
    assert details["current_state"] == "DRAFT"
    assert details["attempted_state"] == "SETTLED"
    assert "PENDING_OWNER_APPROVAL" in details["allowed_next_states"]


def test_progress_steps_stay_within_the_ten_step_flow() -> None:
    for state in ExitWorkflowState:
        step = progress_step(state)
        assert step is None or 1 <= step <= 10
    assert progress_step(S.COMPLETED) == 10
    assert progress_step(S.CANCELLED) is None


# --- security -------------------------------------------------------------------------------


def test_round_trip_token() -> None:
    token = encode_token({"sub": "00000000-0000-0000-0000-000000000001", "role": "TENANT"}, SECRET)
    claims = decode_token(token, SECRET)
    principal = principal_from_claims(claims)
    assert principal.role is PrincipalRole.TENANT


def test_tampered_signature_is_rejected() -> None:
    token = encode_token({"sub": "x", "role": "TENANT"}, SECRET)
    header, body, signature = token.split(".")
    forged = f"{header}.{body}.{'A' * len(signature)}"
    with pytest.raises(AuthenticationError, match="signature"):
        decode_token(forged, SECRET)


def test_wrong_secret_is_rejected() -> None:
    token = encode_token({"sub": "x", "role": "TENANT"}, SECRET)
    with pytest.raises(AuthenticationError):
        decode_token(token, "a-different-secret")


def test_alg_none_is_rejected() -> None:
    """The classic JWT bypass: swap the algorithm for 'none' and drop the signature."""
    import base64
    import json

    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    forged = f"{seg({'alg': 'none', 'typ': 'JWT'})}.{seg({'sub': 'x', 'role': 'ADMIN'})}."
    with pytest.raises(AuthenticationError, match="algorithm"):
        decode_token(forged, SECRET)


def test_expired_token_is_rejected() -> None:
    token = encode_token({"sub": "x", "role": "OWNER"}, SECRET, expires_in=timedelta(seconds=-10))
    with pytest.raises(AuthenticationError, match="expired"):
        decode_token(token, SECRET)


def test_issuer_and_audience_are_enforced() -> None:
    token = encode_token({"sub": "x", "role": "OWNER", "iss": "meridian", "aud": "exit"}, SECRET)
    assert decode_token(token, SECRET, issuer="meridian", audience="exit")
    with pytest.raises(AuthenticationError, match="issuer"):
        decode_token(token, SECRET, issuer="someone-else")
    with pytest.raises(AuthenticationError, match="audience"):
        decode_token(token, SECRET, audience="another-service")


def test_malformed_tokens_do_not_crash() -> None:
    for bad in ("", "a", "a.b", "a.b.c.d", "!!.??.$$"):
        with pytest.raises(AuthenticationError):
            decode_token(bad, SECRET)


def test_api_keys_are_high_entropy_and_stored_hashed() -> None:
    key, digest = generate_api_key()
    assert key.startswith("nwa_") and len(key) > 40
    assert digest == hash_api_key(key)
    assert len(digest) == 64
    assert generate_api_key()[0] != key


# --- PDF ---------------------------------------------------------------------------------------


ISSUED = datetime(2026, 8, 8, 10, 30, tzinfo=UTC)


def _sample_blocks() -> list:
    return [
        Heading("EXIT NO OBJECTION CERTIFICATE", centered=True),
        Paragraph("NOC-2026-000001", bold=True, centered=True),
        Rule(),
        KeyValue("Refund released", "3,750.00 AED", bold_value=True),
        Paragraph("Settled in full. " * 40),
    ]


def test_render_produces_a_structurally_valid_pdf() -> None:
    document = render_pdf(_sample_blocks(), title="Exit NOC", created_at=ISSUED)
    assert document.startswith(b"%PDF-1.4")
    assert document.rstrip().endswith(b"%%EOF")
    assert b"/Type /Catalog" in document
    assert b"/BaseFont /Helvetica-Bold" in document

    # The cross-reference table must point at real object offsets.
    start = int(document[document.rfind(b"startxref") + 9 :].split()[0])
    assert document[start : start + 4] == b"xref"
    count = int(document[start:].split(b"\n")[1].split()[1])
    for i in range(1, count):
        line = document[start + 10 + i * 20 : start + 30 + i * 20]
        offset = int(line[:10])
        assert document[offset : offset + len(str(i))] == str(i).encode()


def test_rendering_is_deterministic() -> None:
    """Byte-stability is what makes the stored SHA-256 a tamper check rather than noise."""
    first = render_pdf(_sample_blocks(), title="Exit NOC", created_at=ISSUED)
    second = render_pdf(_sample_blocks(), title="Exit NOC", created_at=ISSUED)
    assert first == second


def test_long_content_paginates() -> None:
    blocks = [Paragraph("Line of text. " * 12) for _ in range(90)]
    document = render_pdf(blocks, title="Long", created_at=ISSUED)
    assert document.count(b"/Type /Page\n") == 0  # pages are inline dicts
    assert b"/Count 1 " not in document
    assert b"/Type /Pages" in document


def test_parentheses_and_backslashes_are_escaped() -> None:
    """Unescaped '(' would terminate the PDF string and corrupt the document."""
    document = render_pdf(
        [Paragraph(r"Deduction (major) \ repair")], title="Escape", created_at=ISSUED
    )
    assert rb"Deduction \(major\) \\ repair" in document


def test_non_latin1_characters_are_transliterated_not_dropped() -> None:
    document = render_pdf(
        [Paragraph("Tenant’s deposit – settled")], title="Unicode", created_at=ISSUED
    )
    assert b"Tenant's deposit - settled" in document


def test_wrapping_respects_the_measured_column_width() -> None:
    lines = wrap_text("word " * 200, 9.5, 480.0)
    assert len(lines) > 1
    assert all(text_width(line, 9.5) <= 480.0 for line in lines)


def test_a_word_wider_than_the_column_is_split_rather_than_overflowing() -> None:
    lines = wrap_text("M" * 400, 10.0, 200.0)
    assert len(lines) > 1
    assert all(text_width(line, 10.0) <= 200.0 for line in lines)


def test_bold_metrics_differ_from_regular() -> None:
    assert text_width("Refund", 10, bold=True) > text_width("Refund", 10, bold=False)
