"""Phase 15: immediate rejection vs post-send ambiguity, driven through real reconciliation."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from crypto_trader.mainnet.faults import (
    IMMEDIATE_REJECTION_FAULTS,
    POST_SEND_AMBIGUITY_FAULTS,
    DryRunError,
    FaultClass,
    MainnetConfigError,
    SendFault,
    classify_send_fault,
    inject_post_send_ambiguity,
    require_post_send_ambiguity,
)
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.recovery import reconstruct_order_states
from crypto_trader.tests import mainnet_fixtures as fx

CLIENT_ORDER_ID = "ct-phase15-ambiguity-0001"
CORRELATION_ID = UUID("5c2a7f10-8d43-4b96-a1e2-7f0d3c9b4a55")


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as active:
        yield active


@pytest.mark.parametrize("fault", sorted(IMMEDIATE_REJECTION_FAULTS, key=lambda item: item.value))
def test_immediate_rejections_are_classified_before_the_send_boundary(fault: SendFault):
    assert classify_send_fault(fault) is FaultClass.IMMEDIATE_REJECTION
    with pytest.raises(DryRunError):
        require_post_send_ambiguity(fault)


@pytest.mark.parametrize("fault", sorted(POST_SEND_AMBIGUITY_FAULTS, key=lambda item: item.value))
def test_post_send_faults_are_classified_as_ambiguity(fault: SendFault):
    assert classify_send_fault(fault) is FaultClass.POST_SEND_AMBIGUITY
    assert require_post_send_ambiguity(fault) is fault


def test_an_unknown_fault_fails_closed():
    with pytest.raises(MainnetConfigError):
        classify_send_fault("something_else")


@pytest.mark.parametrize("fault", sorted(POST_SEND_AMBIGUITY_FAULTS, key=lambda item: item.value))
def test_a_lost_response_becomes_unknown_pending_reconciliation(journal, fault: SendFault):
    result = inject_post_send_ambiguity(
        journal=journal,
        policy=fx.recovery_policy(),
        fault=fault,
        symbol=fx.SYMBOL,
        client_order_id=CLIENT_ORDER_ID,
        correlation_id=CORRELATION_ID,
        occurred_at=fx.BASE,
        quantity=Decimal("0.01000"),
    )

    assert result.is_unknown_pending_reconciliation is True
    assert result.outcome == "unknown_pending_reconciliation"
    assert result.requires_halt_new_risk is True
    assert result.reconciliation_state == "failed"
    assert result.journaled_event_ids
    # The submission-attempt marker survives: the order is genuinely ambiguous, not "never sent".
    reconstructed, _ = reconstruct_order_states(journal)
    states = {state.order_reference: state for state in reconstructed.states()}
    state = states[result.order_reference]
    assert state.submission_attempted is True
    assert state.is_open is True


def test_the_ambiguity_path_is_deterministic_across_two_identical_runs(tmp_path: Path):
    def run(root: Path):
        with SQLiteEventJournal(root / "journal.db") as journal:
            result = inject_post_send_ambiguity(
                journal=journal,
                policy=fx.recovery_policy(),
                fault=SendFault.TIMEOUT_AFTER_SEND,
                symbol=fx.SYMBOL,
                client_order_id=CLIENT_ORDER_ID,
                correlation_id=CORRELATION_ID,
                occurred_at=fx.BASE,
                quantity=Decimal("0.01000"),
            )
            reconstructed, _ = reconstruct_order_states(journal)
            states = [
                (state.order_reference, state.state.value, str(state.filled_quantity))
                for state in reconstructed.states()
            ]
            event_types = sorted(event.event_type for event in journal.replay())
        return result.outcome, result.requires_halt_new_risk, states, event_types

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert run(tmp_path / "a") == run(tmp_path / "b")


def test_an_immediate_rejection_creates_no_ambiguous_order_state(journal):
    """A pre-submit rejection must simply never enter the ambiguity path."""
    with pytest.raises(DryRunError):
        inject_post_send_ambiguity(
            journal=journal,
            policy=fx.recovery_policy(),
            fault=SendFault.RISK_REJECTED,
            symbol=fx.SYMBOL,
            client_order_id=CLIENT_ORDER_ID,
            correlation_id=CORRELATION_ID,
            occurred_at=fx.BASE,
            quantity=Decimal("0.01000"),
        )
    reconstructed, _ = reconstruct_order_states(journal)
    assert reconstructed.states() == ()
    assert list(journal.replay()) == []
