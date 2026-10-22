"""Phase 15 durable ambiguity, Stage 1: the attempt contract and reconstruction rules.

These tests are fully offline. They prove the durable pre-send attempt fact, the fact-merge (never
a lifecycle regression) replay rule, terminal finality, quantity/identity invariants and conflict
surfacing. No transport, client or runtime sending behaviour is involved.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from crypto_trader.events import (
    ExecutionFillRecorded,
    FillRecordPayload,
    OrderStateChanged,
    OrderStateChangePayload,
    OrderSubmissionAttemptPayload,
    OrderSubmissionAttempted,
    OrderVenueIdentityPayload,
    OrderVenueIdentityRecorded,
)
from crypto_trader.execution.binance_testnet import PRODUCER as VENUE_PRODUCER
from crypto_trader.execution.binance_testnet import deterministic_order_uuid
from crypto_trader.persistence import SnapshotStore, SQLiteEventJournal
from crypto_trader.recovery import (
    OrderStateName,
    OrderStateReconstructor,
    ReconciliationEngine,
    RestartRecovery,
    StaticVenueStateSource,
    load_policy,
    reconstruct_order_states,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
CORRELATION = UUID("5a1c9e30-4b62-4f18-9d7a-2c5e8b0f4d11")
INTENT_ID = UUID("55555555-5555-4555-8555-555555555555")
LOCAL_PRODUCER = "application.production"
CLIENT_ORDER_ID = "ct-44444444444444444444"
ORDER_ID = deterministic_order_uuid(CLIENT_ORDER_ID)
SYMBOL = "SYNTHUSDT"
ORIGINAL = Decimal("1.00000000")


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as active:
        yield active


def attempt_event(
    *,
    index: int = 0,
    quantity: str = "1.00000000",
    client_order_id: str = CLIENT_ORDER_ID,
    order_id: UUID = ORDER_ID,
    intent_id: UUID = INTENT_ID,
    side: str = "buy",
    order_type: str = "limit",
    limit_price: str | None = "100.00000000",
    venue_producer: str = VENUE_PRODUCER,
    producer: str = LOCAL_PRODUCER,
    strategy_id: str | None = "strategy.stage1",
    sequence_number: int | None = None,
) -> OrderSubmissionAttempted:
    return OrderSubmissionAttempted(
        event_id=uuid4(),
        occurred_at=BASE + timedelta(seconds=index),
        correlation_id=CORRELATION,
        producer=producer,
        strategy_id=strategy_id,
        order_id=order_id,
        symbol=SYMBOL,
        sequence_number=sequence_number,
        payload=OrderSubmissionAttemptPayload(
            venue_reference=client_order_id,
            intent_id=intent_id,
            side=side,
            order_type=order_type,
            quantity=Decimal(quantity),
            limit_price=None if limit_price is None else Decimal(limit_price),
            time_in_force="gtc",
            venue_producer=venue_producer,
        ),
    )


def state_event(
    state: str,
    *,
    index: int,
    producer: str = VENUE_PRODUCER,
    order_id: UUID = ORDER_ID,
    reason: str | None = None,
) -> OrderStateChanged:
    return OrderStateChanged(
        event_id=uuid4(),
        occurred_at=BASE + timedelta(seconds=index),
        correlation_id=CORRELATION,
        producer=producer,
        order_id=order_id,
        symbol=SYMBOL,
        payload=OrderStateChangePayload(state=state, reason=reason),
    )


def fill_event(
    quantity: str,
    *,
    index: int,
    remaining: str | None,
    producer: str = VENUE_PRODUCER,
    order_id: UUID = ORDER_ID,
) -> ExecutionFillRecorded:
    return ExecutionFillRecorded(
        event_id=uuid4(),
        occurred_at=BASE + timedelta(seconds=index),
        correlation_id=CORRELATION,
        producer=producer,
        order_id=order_id,
        symbol=SYMBOL,
        payload=FillRecordPayload(
            quantity=Decimal(quantity),
            price=Decimal("100.00000000"),
            remaining_quantity=None if remaining is None else Decimal(remaining),
            liquidity="taker",
        ),
    )


def recovered(journal):
    reconstructor, _summary = reconstruct_order_states(journal)
    return reconstructor


# --- A / B / C: the attempt fact and the fact-merge replay rule --------------------

def test_a_attempt_marker_replayed_before_the_resolution_builds_the_attempted_state(journal):
    journal.append_batch([attempt_event(index=0), state_event("accepted", index=1)])

    state = recovered(journal).state_for(CLIENT_ORDER_ID)

    assert state is not None
    assert state.state is OrderStateName.ACCEPTED
    assert state.submission_attempted is True
    assert state.remaining_quantity == ORIGINAL


def test_b_attempt_marker_replayed_after_the_resolution_merges_and_rekeys(journal):
    journal.append_batch([state_event("accepted", index=0), attempt_event(index=1)])

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert reconstructor.state_for(str(ORDER_ID)) is None, "the order must not exist twice"
    assert state is not None
    assert state.state is OrderStateName.ACCEPTED
    assert state.submission_attempted is True
    assert reconstructor.attempt_anchors(CLIENT_ORDER_ID).quantity == ORIGINAL


def test_c_marker_after_filled_merges_facts_and_preserves_filled(journal):
    journal.append_batch(
        [
            state_event("submitted", index=0),
            fill_event("1.00000000", index=1, remaining="0"),
            attempt_event(index=2, sequence_number=99),  # sequence_number must be irrelevant
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state is not None
    assert state.state is OrderStateName.FILLED, "the attempt must never regress FILLED"
    assert state.submission_attempted is True
    assert state.filled_quantity == ORIGINAL
    anchors = reconstructor.attempt_anchors(CLIENT_ORDER_ID)
    assert anchors is not None and anchors.venue_producer == VENUE_PRODUCER
    assert reconstructor.out_of_order_events_skipped == 0


def test_the_attempt_contract_never_sets_sequence_number():
    assert attempt_event().sequence_number is None


# --- D / E: late partial fills keep the terminal state ---------------------------

def test_d_canceled_with_a_late_partial_fill_keeps_canceled_and_advances_quantity(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("canceled", index=1, reason="venue_status_canceled"),
            fill_event("0.25000000", index=2, remaining="0.75000000"),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.CANCELED
    assert state.filled_quantity == Decimal("0.25000000")
    assert state.remaining_quantity == Decimal("0.75000000")
    assert reconstructor.conflicts() == {}
    assert reconstructor.late_fill_refinements == 1


def test_e_expired_with_a_late_partial_fill_keeps_expired_and_advances_quantity(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("expired", index=1, reason="venue_status_expired"),
            fill_event("0.40000000", index=2, remaining="0.60000000"),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.EXPIRED
    assert state.filled_quantity == Decimal("0.40000000")
    assert reconstructor.conflicts() == {}
    assert reconstructor.late_fill_refinements == 1


# --- F / G / H: a HISTORICAL filled lifecycle fact never refines a terminal order --

@pytest.mark.parametrize("terminal", ["canceled", "expired", "rejected"])
def test_f_g_h_a_historical_filled_event_cannot_refine_a_terminal_order(journal, terminal):
    """A reconstructed historical ``order.state.changed{filled}`` is not current venue state."""
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event(terminal, index=1, reason="venue_status"),
            state_event("filled", index=2, producer=VENUE_PRODUCER),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state.value == terminal
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == (
        "historical_filled_event_cannot_refine_a_terminal_order",
    )


def test_an_older_historical_filled_event_is_ignored_as_stale():
    """Out-of-order delivery of an older FILLED fact is stale, never a refinement."""
    reconstructor = OrderStateReconstructor()
    reconstructor.handle(attempt_event(index=0))
    reconstructor.handle(state_event("canceled", index=5, reason="venue_status"))
    reconstructor.handle(state_event("filled", index=1, producer=VENUE_PRODUCER))

    state = reconstructor.state_for(CLIENT_ORDER_ID)
    assert state.state is OrderStateName.CANCELED
    assert reconstructor.conflicts() == {}
    assert reconstructor.out_of_order_events_skipped == 1


def test_a_replayed_filled_fact_earlier_than_the_terminal_fact_keeps_fencing(journal):
    """Replay order: the open -> FILLED progression is allowed, then the later terminal fact
    contradicts it and is therefore a conflict (never a silent override)."""
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("filled", index=1, producer=VENUE_PRODUCER),
            state_event("canceled", index=5, reason="venue_status_canceled"),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.FILLED
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == ("filled_is_absorbing",)


# --- normal (non-terminal) progression to FILLED still works ----------------------

@pytest.mark.parametrize("open_state", ["submitted", "accepted", "partially_filled"])
def test_non_terminal_states_can_still_reach_filled(journal, open_state):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event(open_state, index=1, producer=VENUE_PRODUCER),
            state_event("filled", index=2, producer=VENUE_PRODUCER),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.FILLED
    assert reconstructor.conflicts() == {}


# --- I / J: REJECTED never becomes an intermediate accepted state -----------------

def _rejected(journal, *extra):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("rejected", index=1, reason="venue_status_rejected"),
            *extra,
        ]
    )
    reconstructor = recovered(journal)
    return reconstructor, reconstructor.state_for(CLIENT_ORDER_ID)


def test_i_rejected_to_accepted_is_a_conflict_that_keeps_fencing(journal):
    reconstructor, state = _rejected(journal, state_event("accepted", index=2))

    assert state.state is OrderStateName.REJECTED
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == (
        "terminal_to_non_terminal_lifecycle_conflict",
    )


def test_j_rejected_to_partially_filled_is_a_conflict_that_keeps_fencing(journal):
    reconstructor, state = _rejected(
        journal, fill_event("0.50000000", index=2, remaining="0.50000000")
    )

    assert state.state is OrderStateName.REJECTED
    assert state.filled_quantity == 0
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == (
        "rejected_fill_requires_authoritative_current_state",
    )


# --- K / L / M: quantity and identity invariants ---------------------------------

def test_k_a_decreasing_executed_quantity_is_a_conflict(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("canceled", index=1, reason="venue_status_canceled"),
            fill_event("0.30000000", index=2, remaining="0.70000000"),
            fill_event("0.10000000", index=3, remaining="0.90000000"),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.CANCELED
    assert state.filled_quantity == Decimal("0.30000000")
    assert state.remaining_quantity == Decimal("0.70000000")
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == ("executed_quantity_decreased",)


def test_l_executed_quantity_above_the_original_quantity_is_a_conflict(journal):
    journal.append_batch(
        [
            attempt_event(index=0, quantity="0.50000000"),
            fill_event("0.60000000", index=1, remaining="0"),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.SUBMITTED
    assert state.filled_quantity == 0
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == (
        "executed_quantity_exceeds_original_quantity",
    )


def test_m_immutable_economic_identity_mismatch_is_a_conflict(journal):
    journal.append_batch([attempt_event(index=0), attempt_event(index=1, quantity="2.00000000")])

    reconstructor = recovered(journal)
    anchors = reconstructor.attempt_anchors(CLIENT_ORDER_ID)

    assert anchors.quantity == ORIGINAL, "the first durable anchors stay immutable"
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == (
        "immutable_economic_identity_mismatch",
    )


def test_m_strategy_attribution_is_part_of_the_immutable_attempt_identity(journal):
    journal.append_batch(
        [
            attempt_event(index=0, strategy_id="strategy.stage1"),
            attempt_event(index=1, strategy_id="strategy.other"),
        ]
    )

    reconstructor = recovered(journal)
    anchors = reconstructor.attempt_anchors(CLIENT_ORDER_ID)

    assert anchors is not None
    assert anchors.strategy_id == "strategy.stage1"
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == (
        "immutable_economic_identity_mismatch",
    )


# --- N / O: legacy fallback and determinism ---------------------------------------

def test_n_legacy_journals_without_the_attempt_contract_still_reconstruct(journal):
    journal.append_batch(
        [
            state_event("created", index=0, producer=LOCAL_PRODUCER),
            state_event("submitted", index=1, producer=LOCAL_PRODUCER),
            state_event("accepted", index=2),
            fill_event("0.50000000", index=3, remaining="0.50000000"),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(str(ORDER_ID))

    assert state is not None and state.submission_attempted is True
    assert state.state is OrderStateName.PARTIALLY_FILLED
    assert reconstructor.attempt_anchors(str(ORDER_ID)) is None
    assert reconstructor.conflicts() == {}


def test_o_replay_is_deterministic_across_repeated_runs():
    def run(root: Path):
        root.mkdir(parents=True, exist_ok=True)
        with SQLiteEventJournal(root / "journal.db") as journal:
            journal.append_batch(
                [
                    attempt_event(index=0),
                    state_event("canceled", index=1, reason="venue_status_canceled"),
                    fill_event("0.25000000", index=2, remaining="0.75000000"),
                ]
            )
            reconstructor = recovered(journal)
            state = reconstructor.state_for(CLIENT_ORDER_ID)
            return (
                state.state.value,
                str(state.filled_quantity),
                str(state.remaining_quantity),
                reconstructor.conflicts(),
                reconstructor.late_fill_refinements,
                reconstructor.out_of_order_events_skipped,
            )

    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        assert run(Path(first) / "a") == run(Path(second) / "b")


# --- conflicts keep the order unresolved so new risk stays fenced ------------------

def _recovery_run(journal):
    return RestartRecovery(
        engine=ReconciliationEngine(load_policy({"policy_id": "recovery-v1"})),
        journal=journal,
        venue_sources=[StaticVenueStateSource(entries=())],
        producer="application.recovery",
    ).run(now=BASE, correlation_id=CORRELATION)


def test_a_lifecycle_conflict_keeps_the_order_unresolved_and_fenced(journal):
    _rejected(journal, state_event("accepted", index=2))

    run = _recovery_run(journal)

    assert run.conflicted_orders == (CLIENT_ORDER_ID,)
    assert run.report.is_fully_resolved is False
    assert run.report.requires_halt_new_risk is True
    assert run.recovery_state == "failed"
    assert CLIENT_ORDER_ID in run.report.unresolved_orders
    assert "terminal_to_non_terminal" in run.report.reason_codes[CLIENT_ORDER_ID]


def test_a_clean_attempt_recovery_stays_resolved(journal):
    journal.append_batch(
        [attempt_event(index=0), fill_event("1.00000000", index=1, remaining="0")]
    )

    run = _recovery_run(journal)

    assert run.conflicted_orders == ()
    assert run.report.is_fully_resolved is True
    assert run.recovery_state == "resolved"


# --- the complete 4x4 terminal -> terminal matrix ---------------------------------

def _disposition(reconstructor, *, baseline_skipped: int, from_state: str, to_state: str) -> str:
    """Mechanically distinguish the four approved dispositions."""
    if reconstructor.conflicts().get(CLIENT_ORDER_ID):
        return "CONFLICT_KEEP_FENCED"
    if reconstructor.out_of_order_events_skipped > baseline_skipped:
        return "IGNORE_AS_STALE"
    state = reconstructor.state_for(CLIENT_ORDER_ID)
    if to_state != from_state and state is not None and state.state.value == to_state:
        return "ALLOW"
    return "IDEMPOTENT_NO_OP"


@pytest.mark.parametrize(
    "from_state,to_state,expected",
    [
        ("filled", "filled", "IDEMPOTENT_NO_OP"),
        ("filled", "canceled", "CONFLICT_KEEP_FENCED"),
        ("filled", "rejected", "CONFLICT_KEEP_FENCED"),
        ("filled", "expired", "CONFLICT_KEEP_FENCED"),
        ("canceled", "filled", "CONFLICT_KEEP_FENCED"),
        ("canceled", "canceled", "IDEMPOTENT_NO_OP"),
        ("canceled", "rejected", "CONFLICT_KEEP_FENCED"),
        ("canceled", "expired", "CONFLICT_KEEP_FENCED"),
        ("rejected", "filled", "CONFLICT_KEEP_FENCED"),
        ("rejected", "canceled", "CONFLICT_KEEP_FENCED"),
        ("rejected", "rejected", "IDEMPOTENT_NO_OP"),
        ("rejected", "expired", "CONFLICT_KEEP_FENCED"),
        ("expired", "filled", "CONFLICT_KEEP_FENCED"),
        ("expired", "canceled", "CONFLICT_KEEP_FENCED"),
        ("expired", "rejected", "CONFLICT_KEEP_FENCED"),
        ("expired", "expired", "IDEMPOTENT_NO_OP"),
    ],
)
def test_the_complete_terminal_to_terminal_matrix(journal, from_state, to_state, expected):
    journal.append_batch(
        [attempt_event(index=0), state_event(from_state, index=1, reason="venue_status")]
    )
    baseline = recovered(journal)
    baseline_skipped = baseline.out_of_order_events_skipped
    assert baseline.state_for(CLIENT_ORDER_ID).state.value == from_state

    journal.append(state_event(to_state, index=2, reason="venue_status"))
    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert (
        _disposition(
            reconstructor,
            baseline_skipped=baseline_skipped,
            from_state=from_state,
            to_state=to_state,
        )
        == expected
    )
    expected_final = to_state if expected == "ALLOW" else from_state
    assert state.state.value == expected_final
    if expected == "IDEMPOTENT_NO_OP":
        assert reconstructor.out_of_order_events_skipped == baseline_skipped


# --- a historical fill never performs the terminal -> FILLED refinement -----------

def test_a_historical_fill_reaching_q_does_not_refine_a_canceled_order(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("canceled", index=1, reason="venue_status_canceled"),
            fill_event("1.00000000", index=2, remaining="0"),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.CANCELED, "fill evidence is not current venue state"
    assert state.filled_quantity == ORIGINAL
    assert state.remaining_quantity == 0
    assert reconstructor.conflicts() == {}
    assert reconstructor.late_fill_refinements == 1


def test_a_historical_fill_reaching_q_does_not_refine_a_rejected_order(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("rejected", index=1, reason="venue_status_rejected"),
            fill_event("1.00000000", index=2, remaining="0"),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.REJECTED
    assert state.filled_quantity == 0
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == (
        "rejected_fill_requires_authoritative_current_state",
    )


# --- authority / provenance is load-bearing, never timestamp-derived --------------

def test_local_producer_cannot_refine_state_to_filled(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("canceled", index=1, reason="venue_status_canceled"),
            # Newer than the venue state, but a local producer: provenance decides, not time.
            state_event("filled", index=2, producer=LOCAL_PRODUCER),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.CANCELED
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == (
        "filled_refinement_requires_authoritative_venue_evidence",
    )


def test_authoritative_venue_fill_invokes_the_late_fill_rule(journal):
    """Positive counterpart: the recognized venue producer's fill IS authoritative evidence."""
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("canceled", index=1, reason="venue_status_canceled"),
            fill_event("0.25000000", index=2, remaining="0.75000000", producer=VENUE_PRODUCER),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.CANCELED
    assert state.filled_quantity == Decimal("0.25000000")
    assert reconstructor.conflicts() == {}
    assert reconstructor.late_fill_refinements == 1


def test_local_fill_cannot_invoke_the_authoritative_late_fill_rule(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("canceled", index=1, reason="venue_status_canceled"),
            fill_event("0.25000000", index=2, remaining="0.75000000", producer=LOCAL_PRODUCER),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.filled_quantity == 0
    assert state.remaining_quantity == ORIGINAL
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == (
        "non_authoritative_fill_on_terminal_order",
    )
    assert reconstructor.late_fill_refinements == 0


# --- snapshot / restart round-trip proof -----------------------------------------

def _attempted_journal(path: Path):
    journal = SQLiteEventJournal(path)
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("canceled", index=1, reason="venue_status_canceled"),
            fill_event("0.25000000", index=2, remaining="0.75000000"),
            state_event("canceled", index=3, reason="venue_status_canceled"),
        ]
    )
    return journal


def _snapshot_of(journal):
    reconstructor, summary = reconstruct_order_states(journal)
    from crypto_trader.persistence import SnapshotRecord

    return reconstructor, SnapshotRecord(
        name="orders",
        snapshot_schema=1,
        state=reconstructor.snapshot_state(),
        last_event_id=summary.last_event_id,
        last_occurred_at_ns=summary.last_occurred_at_ns,
        last_sequence_number=summary.last_sequence_number,
        created_at_iso=BASE.isoformat(),
    )


def test_snapshot_restore_preserves_attempt_anchors_and_bookkeeping(tmp_path):
    with _attempted_journal(tmp_path / "journal.db") as journal:
        before, snapshot = _snapshot_of(journal)
        restored, tail = reconstruct_order_states(journal, snapshot=snapshot)

    assert tail.events_seen == 0
    anchors = restored.attempt_anchors(CLIENT_ORDER_ID)
    assert anchors is not None
    assert anchors.order_reference == CLIENT_ORDER_ID
    assert anchors.order_id == ORDER_ID
    assert anchors.intent_id == INTENT_ID
    assert anchors.strategy_id == "strategy.stage1"
    assert anchors.side == "buy"
    assert anchors.order_type == "limit"
    assert anchors.quantity == ORIGINAL
    assert anchors.limit_price == Decimal("100.00000000")
    assert anchors.time_in_force == "gtc"
    assert anchors.venue_producer == VENUE_PRODUCER

    state = restored.state_for(CLIENT_ORDER_ID)
    assert state is not None and state.submission_attempted is True
    assert state.state is OrderStateName.CANCELED
    assert state.filled_quantity == Decimal("0.25000000")
    assert restored.conflicts() == {}
    assert restored.late_fill_refinements == before.late_fill_refinements == 1
    # The newer identical duplicate in this journal is an idempotent no-op, never "stale".
    assert restored.out_of_order_events_skipped == before.out_of_order_events_skipped == 0


@pytest.mark.parametrize("producer,expect_applied,expect_conflict", [
    (VENUE_PRODUCER, True, False),
    (LOCAL_PRODUCER, False, True),
])
def test_authority_decisions_after_snapshot_restore_match_the_original(
    tmp_path, producer, expect_applied, expect_conflict
):
    with _attempted_journal(tmp_path / "journal.db") as journal:
        _before, snapshot = _snapshot_of(journal)
        journal.append(
            fill_event("0.10000000", index=4, remaining="0.65000000", producer=producer)
        )
        restored, tail = reconstruct_order_states(journal, snapshot=snapshot)

    assert tail.events_seen == 1
    state = restored.state_for(CLIENT_ORDER_ID)
    assert state.state is OrderStateName.CANCELED
    assert (state.filled_quantity == Decimal("0.35000000")) is expect_applied
    assert (state.filled_quantity == Decimal("0.25000000")) is not expect_applied
    assert bool(restored.conflicts()) is expect_conflict


# --- duplicate semantics: older = stale, equal/newer = idempotent no-op -----------

def test_a_newer_identical_duplicate_is_an_idempotent_no_op(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("canceled", index=1, reason="venue_status_canceled"),
            state_event("canceled", index=2, reason="venue_status_canceled"),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.CANCELED
    assert reconstructor.conflicts() == {}
    assert reconstructor.out_of_order_events_skipped == 0


def test_an_equal_timestamp_identical_duplicate_is_an_idempotent_no_op(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("canceled", index=1, reason="venue_status_canceled"),
            state_event("canceled", index=1, reason="venue_status_canceled"),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.CANCELED
    assert reconstructor.conflicts() == {}
    assert reconstructor.out_of_order_events_skipped == 0


def test_an_older_identical_duplicate_is_ignored_as_stale():
    """At-least-once delivery is unordered, so an older redundant fact is stale, not an error."""
    reconstructor = OrderStateReconstructor()
    reconstructor.handle(attempt_event(index=0))
    reconstructor.handle(state_event("canceled", index=5, reason="venue_status_canceled"))
    reconstructor.handle(state_event("canceled", index=1, reason="venue_status_canceled"))

    state = reconstructor.state_for(CLIENT_ORDER_ID)
    assert state.state is OrderStateName.CANCELED
    assert reconstructor.conflicts() == {}
    assert reconstructor.out_of_order_events_skipped == 1


def test_a_snapshot_preserves_the_stale_skip_counter(tmp_path):
    reconstructor = OrderStateReconstructor()
    reconstructor.handle(attempt_event(index=0))
    reconstructor.handle(
        state_event("canceled", index=5, reason="venue_status_canceled")
    )
    reconstructor.handle(
        state_event("canceled", index=1, reason="venue_status_canceled")
    )
    assert reconstructor.out_of_order_events_skipped == 1

    with SnapshotStore(tmp_path / "snapshots.db") as store:
        store.save("orders", reconstructor.snapshot_state())
        record = store.latest("orders")
    restored = OrderStateReconstructor()
    restored.restore_state(record.state)

    assert restored.out_of_order_events_skipped == 1
    assert restored.late_fill_refinements == reconstructor.late_fill_refinements
    anchors = restored.attempt_anchors(CLIENT_ORDER_ID)
    assert anchors is not None and anchors.quantity == ORIGINAL
    assert restored.state_for(CLIENT_ORDER_ID).state is OrderStateName.CANCELED


def test_authoritative_late_fills_after_filled_state_refine_quantities_without_state_regression(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("filled", index=1, producer=VENUE_PRODUCER),
            fill_event("0.40000000", index=2, remaining="0.60000000", producer=VENUE_PRODUCER),
            fill_event("0.60000000", index=3, remaining="0", producer=VENUE_PRODUCER),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.FILLED
    assert state.filled_quantity == ORIGINAL
    assert state.remaining_quantity == 0
    assert reconstructor.conflicts() == {}
    assert reconstructor.late_fill_refinements == 2


def test_filled_late_fill_without_remaining_quantity_stays_fenced(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("filled", index=1, producer=VENUE_PRODUCER),
            fill_event("0.40000000", index=2, remaining=None, producer=VENUE_PRODUCER),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.FILLED
    assert state.filled_quantity == 0
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == (
        "late_fill_without_remaining_quantity_evidence",
    )


def test_filled_late_fill_with_inconsistent_quantity_evidence_stays_fenced(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("filled", index=1, producer=VENUE_PRODUCER),
            fill_event("0.40000000", index=2, remaining="0.50000000", producer=VENUE_PRODUCER),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.FILLED
    assert state.filled_quantity == 0
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == (
        "inconsistent_terminal_fill_quantities",
    )


def test_filled_late_fill_from_local_producer_stays_fenced(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("filled", index=1, producer=VENUE_PRODUCER),
            fill_event("0.40000000", index=2, remaining="0.60000000", producer=LOCAL_PRODUCER),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.FILLED
    assert state.filled_quantity == 0
    assert reconstructor.conflicts()[CLIENT_ORDER_ID] == (
        "non_authoritative_fill_on_terminal_order",
    )


def test_unrecognized_venue_producer_never_grants_authority(journal):
    journal.append_batch(
        [
            attempt_event(index=0, venue_producer=LOCAL_PRODUCER),
            state_event("accepted", index=1, producer=LOCAL_PRODUCER),
            fill_event("1.00000000", index=2, remaining="0", producer=LOCAL_PRODUCER),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state is not None
    assert state.state is not OrderStateName.FILLED
    assert "unrecognized_venue_producer" in reconstructor.conflicts()[CLIENT_ORDER_ID]


def test_local_fill_cannot_complete_an_attempted_order(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("accepted", index=1, producer=VENUE_PRODUCER),
            fill_event("1.00000000", index=2, remaining="0", producer=LOCAL_PRODUCER),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state is not None
    assert state.state is OrderStateName.PARTIALLY_FILLED
    assert state.filled_quantity == ORIGINAL
    assert reconstructor.conflicts() == {}


def test_local_lifecycle_cannot_complete_an_attempted_order(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("accepted", index=1, producer=VENUE_PRODUCER),
            state_event("filled", index=2, producer=LOCAL_PRODUCER),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state is not None
    assert state.state is OrderStateName.ACCEPTED
    assert "filled_completion_requires_authoritative_venue_evidence" in (
        reconstructor.conflicts()[CLIENT_ORDER_ID]
    )


def test_supplied_local_states_preserve_unrecognized_producer_conflict(journal):
    journal.append_batch(
        [
            attempt_event(index=0, venue_producer=LOCAL_PRODUCER),
            state_event("accepted", index=1, producer=LOCAL_PRODUCER),
        ]
    )
    supplied_states = recovered(journal).states()
    run = RestartRecovery(
        engine=ReconciliationEngine(load_policy({"policy_id": "recovery-v1"})),
        journal=journal,
        venue_sources=[StaticVenueStateSource(entries=())],
        producer="application.recovery",
    ).run(now=BASE + timedelta(minutes=1), local_states=supplied_states)

    assert CLIENT_ORDER_ID in run.conflicted_orders
    assert run.requires_halt_new_risk is True
    assert "unrecognized_venue_producer" in run.report.reason_codes[CLIENT_ORDER_ID]


def test_authoritative_zero_remaining_below_quantity_stays_open_without_conflict(journal):
    journal.append_batch(
        [
            attempt_event(index=0),
            state_event("accepted", index=1),
            fill_event("0.10000000", index=2, remaining="0"),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.PARTIALLY_FILLED
    assert state.filled_quantity == Decimal("0.10000000")
    assert state.remaining_quantity == Decimal("0")
    assert reconstructor.conflicts() == {}


def test_local_completion_before_attempt_is_fenced_when_attempt_arrives(journal):
    journal.append_batch(
        [
            state_event("submitted", index=0, producer=LOCAL_PRODUCER),
            fill_event("1.00000000", index=1, remaining="0", producer=LOCAL_PRODUCER),
            attempt_event(index=2),
        ]
    )

    reconstructor = recovered(journal)
    state = reconstructor.state_for(CLIENT_ORDER_ID)

    assert state.state is OrderStateName.FILLED
    assert "filled_completion_requires_authoritative_venue_evidence" in (
        reconstructor.conflicts()[CLIENT_ORDER_ID]
    )


# --- review G-4: our venue order id, bound to the attempt it answers --------------

OTHER_CLIENT_ORDER_ID = "ct-33333333333333333333"
OTHER_ORDER_ID = deterministic_order_uuid(OTHER_CLIENT_ORDER_ID)
OTHER_INTENT_ID = UUID("33333333-3333-4333-8333-333333333333")


def identity_event(
    venue_order_id: str = "4242",
    *,
    index: int = 1,
    client_order_id: str = CLIENT_ORDER_ID,
    order_id: UUID = ORDER_ID,
    intent_id: UUID = INTENT_ID,
    symbol: str = SYMBOL,
    producer: str = VENUE_PRODUCER,
) -> OrderVenueIdentityRecorded:
    return OrderVenueIdentityRecorded(
        event_id=uuid4(),
        occurred_at=BASE + timedelta(seconds=index),
        correlation_id=CORRELATION,
        producer=producer,
        order_id=order_id,
        symbol=symbol,
        payload=OrderVenueIdentityPayload(
            venue_reference=client_order_id, intent_id=intent_id, venue_order_id=venue_order_id
        ),
    )


@pytest.mark.parametrize("attempt_index", [0, 3], ids=["attempt first", "venue answer first"])
def test_our_venue_identity_binds_to_the_attempt_it_answers(journal, attempt_index):
    # The venue stamps its answer, so replay may order it before our locally stamped attempt.
    journal.append_batch(
        [
            attempt_event(index=attempt_index),
            identity_event(index=1),
            state_event("accepted", index=1),
            identity_event(index=2),  # the same identity again is idempotent
        ]
    )

    reconstructor = recovered(journal)

    assert reconstructor.state_for(CLIENT_ORDER_ID).venue_order_id == "4242"
    assert reconstructor.conflicts() == {}


@pytest.mark.parametrize(
    "events,venue_ids,conflicts",
    [
        (
            lambda: [attempt_event(index=0), identity_event(index=1), identity_event("777", index=2)],
            ["4242"],
            {CLIENT_ORDER_ID: ("conflicting_venue_order_identity",)},
        ),
        (
            lambda: [state_event("accepted", index=0), identity_event(index=1)],
            [None],
            {CLIENT_ORDER_ID: ("venue_identity_without_a_matching_attempt",)},
        ),
        (
            lambda: [attempt_event(index=0), identity_event(intent_id=OTHER_INTENT_ID)],
            [None],
            {CLIENT_ORDER_ID: ("venue_identity_bound_to_a_different_attempt",)},
        ),
        (
            lambda: [attempt_event(index=0), identity_event(order_id=OTHER_ORDER_ID)],
            [None],
            {CLIENT_ORDER_ID: ("venue_identity_bound_to_a_different_attempt",)},
        ),
        (
            lambda: [attempt_event(index=0), identity_event(symbol="OTHERUSDT")],
            [None],
            {CLIENT_ORDER_ID: ("venue_identity_bound_to_a_different_attempt",)},
        ),
        (
            lambda: [attempt_event(index=0), identity_event(producer=LOCAL_PRODUCER)],
            [None],
            {CLIENT_ORDER_ID: ("venue_identity_requires_the_attempt_venue_producer",)},
        ),
        (
            lambda: [
                attempt_event(index=0),
                identity_event(index=1),
                attempt_event(
                    index=2,
                    client_order_id=OTHER_CLIENT_ORDER_ID,
                    order_id=OTHER_ORDER_ID,
                    intent_id=OTHER_INTENT_ID,
                ),
                identity_event(
                    index=3,
                    client_order_id=OTHER_CLIENT_ORDER_ID,
                    order_id=OTHER_ORDER_ID,
                    intent_id=OTHER_INTENT_ID,
                ),
            ],
            [None, "4242"],
            {
                OTHER_CLIENT_ORDER_ID: ("venue_order_identity_claimed_by_another_attempt",),
                CLIENT_ORDER_ID: ("venue_order_identity_claimed_by_another_attempt",),
            },
        ),
    ],
    ids=[
        "a second venue id for our attempt",
        "no attempt to bind to",
        "another intent",
        "another order id",
        "another symbol",
        "not the attempt's venue producer",
        "one venue order claimed by two attempts",
    ],
)
def test_an_unbound_or_contradicting_venue_identity_is_a_conflict(
    journal, events, venue_ids, conflicts
):
    journal.append_batch(events())

    reconstructor = recovered(journal)

    assert [state.venue_order_id for state in reconstructor.states()] == venue_ids
    assert reconstructor.conflicts() == conflicts


def test_a_snapshot_keeps_our_venue_identity_and_its_first_binding(tmp_path):
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        journal.append_batch(
            [attempt_event(index=0), identity_event(index=1), state_event("accepted", index=2)]
        )
        _before, snapshot = _snapshot_of(journal)
        journal.append_batch(
            [
                identity_event("777", index=3),
                attempt_event(
                    index=4,
                    client_order_id=OTHER_CLIENT_ORDER_ID,
                    order_id=OTHER_ORDER_ID,
                    intent_id=OTHER_INTENT_ID,
                ),
                identity_event(
                    index=5,
                    client_order_id=OTHER_CLIENT_ORDER_ID,
                    order_id=OTHER_ORDER_ID,
                    intent_id=OTHER_INTENT_ID,
                ),
            ]
        )
        restored, _tail = reconstruct_order_states(journal, snapshot=snapshot)

    assert restored.state_for(CLIENT_ORDER_ID).venue_order_id == "4242"
    assert restored.conflicts() == {
        OTHER_CLIENT_ORDER_ID: ("venue_order_identity_claimed_by_another_attempt",),
        CLIENT_ORDER_ID: (
            "conflicting_venue_order_identity",
            "venue_order_identity_claimed_by_another_attempt",
        ),
    }

    # A snapshot written before review G-4 restores with the identity unknown.
    legacy = dict(
        snapshot.state,
        orders=[
            {key: value for key, value in entry.items() if key != "venue_order_id"}
            for entry in snapshot.state["orders"]
        ],
    )
    unknown = OrderStateReconstructor()
    unknown.restore_state(legacy)
    assert unknown.state_for(CLIENT_ORDER_ID).venue_order_id is None


def test_a_snapshot_keeps_a_venue_identity_that_arrived_before_its_attempt(tmp_path):
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        journal.append_batch([state_event("accepted", index=1), identity_event(index=1)])
        before, snapshot = _snapshot_of(journal)
        journal.append(attempt_event(index=3))
        restored, _tail = reconstruct_order_states(journal, snapshot=snapshot)

    assert before.conflicts() == {CLIENT_ORDER_ID: ("venue_identity_without_a_matching_attempt",)}
    assert restored.state_for(CLIENT_ORDER_ID).venue_order_id == "4242"
    assert restored.conflicts() == {}
