"""Phase 8: deterministic reconciliation engine tests (every required scenario)."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from crypto_trader.recovery import (
    AdjustmentSource,
    LocalOrderState,
    OrderStateName,
    ReconciliationEngine,
    ReconciliationError,
    ReconciliationOutcome,
    ReconciliationPolicy,
    RecoveryConfigError,
    RestartRecovery,
    StaticVenueStateSource,
    VenueOrderReport,
    load_policy,
)
from crypto_trader.events import (
    OrderStateChanged,
    OrderStateChangePayload,
    OrderSubmissionAttemptPayload,
    OrderSubmissionAttempted,
)
from crypto_trader.execution.binance_testnet import PRODUCER as VENUE_PRODUCER
from crypto_trader.execution.binance_testnet import deterministic_order_uuid
from crypto_trader.persistence import SQLiteEventJournal

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _local(
    reference: str = "O-1",
    state: OrderStateName = OrderStateName.ACCEPTED,
    *,
    filled: str = "0",
    remaining: str = "0",
    attempted: bool = False,
    symbol: str = "ETHUSDT",
    correlation_id=None,
    order_id=None,
) -> LocalOrderState:
    return LocalOrderState(
        order_id=order_id or uuid4(),
        order_reference=reference,
        intent_id=None,
        correlation_id=correlation_id,
        symbol=symbol,
        state=state,
        filled_quantity=Decimal(filled),
        remaining_quantity=Decimal(remaining),
        submission_attempted=attempted,
    )


def _venue(
    reference: str = "O-1",
    state: OrderStateName = OrderStateName.ACCEPTED,
    *,
    filled: str = "0",
    remaining: str = "0",
    venue_order_id: str | None = "V-1",
    reported_at: datetime | None = None,
) -> VenueOrderReport:
    return VenueOrderReport(
        order_reference=reference,
        venue_order_id=venue_order_id,
        state=state,
        filled_quantity=Decimal(filled),
        remaining_quantity=Decimal(remaining),
        reported_at=reported_at,
    )


def engine(**overrides) -> ReconciliationEngine:
    return ReconciliationEngine(load_policy({"policy_id": "recovery-v1", **overrides}))


# --- policy is explicit and fails closed ------------------------------------

def test_policy_requires_an_explicit_id():
    with pytest.raises(RecoveryConfigError):
        load_policy({})


def test_policy_rejects_unknown_keys():
    with pytest.raises(RecoveryConfigError):
        load_policy({"policy_id": "recovery-v1", "adopt_unknown_orders": True})


def test_policy_refuses_to_ignore_unknown_external_orders():
    with pytest.raises(RecoveryConfigError):
        load_policy({"policy_id": "recovery-v1", "unknown_external_order_policy": "ignore"})


def test_policy_refuses_to_disable_halt_on_ambiguity():
    with pytest.raises(RecoveryConfigError):
        load_policy({"policy_id": "recovery-v1", "halt_on_ambiguity": False})


def test_engine_requires_a_resolved_policy():
    with pytest.raises(ReconciliationError):
        ReconciliationEngine("recovery-v1")  # type: ignore[arg-type]


# --- matched ---------------------------------------------------------------

def test_matched_order_is_resolved_without_any_adjustment():
    report = engine().reconcile([_local(state=OrderStateName.ACCEPTED)], [_venue()])

    assert report.outcome_for("O-1") is ReconciliationOutcome.MATCHED
    assert report.adjustments == []
    assert report.resolved_orders == ["O-1"]
    assert report.requires_halt_new_risk is False
    assert report.is_fully_resolved is True


# --- crash before submission ----------------------------------------------

def test_crash_before_submission_resolves_to_never_submitted():
    local = _local(state=OrderStateName.CREATED, attempted=False)

    report = engine().reconcile([local], [])

    assert report.outcome_for("O-1") is ReconciliationOutcome.NEVER_SUBMITTED
    assert report.never_submitted == ["O-1"]
    assert report.requires_halt_new_risk is False
    assert len(report.adjustments) == 1
    adjustment = report.adjustments[0]
    assert adjustment.source is AdjustmentSource.APPLICATION_JOURNAL
    assert adjustment.before.state is OrderStateName.CREATED
    assert adjustment.after.state is OrderStateName.CANCELED
    assert adjustment.reason_code == "no_submission_attempt_recorded"


# --- ambiguous submission (timeout) ---------------------------------------

def test_ambiguous_submission_is_not_converted_to_failed_or_rejected():
    local = _local(state=OrderStateName.SUBMITTED, attempted=True)

    report = engine().reconcile([local], [])

    assert report.outcome_for("O-1") is ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION
    assert report.reason_for("O-1") == "ambiguous_submission_no_venue_evidence"
    assert report.ambiguous_orders == ["O-1"]
    assert report.requires_halt_new_risk is True
    assert report.unresolved_orders == ["O-1"]
    # No silent correction anywhere: the local state is left exactly as it was.
    assert report.adjustments == []


def test_reconciliation_engine_exposes_no_submission_or_retry_surface():
    for surface in ("submit", "resubmit", "retry", "place_order", "cancel_order", "execute"):
        assert not hasattr(engine(), surface)


# --- venue accepted but local acknowledgement missing ---------------------

def test_venue_accepted_but_local_ack_missing_resolves_from_venue():
    local = _local(state=OrderStateName.SUBMITTED, attempted=True)

    report = engine().reconcile([local], [_venue(state=OrderStateName.ACCEPTED)])

    assert report.outcome_for("O-1") is ReconciliationOutcome.RESOLVED_FROM_VENUE
    assert report.resolved_orders == ["O-1"]
    assert report.requires_halt_new_risk is False
    adjustment = report.adjustments[0]
    assert adjustment.source is AdjustmentSource.VENUE_REPORT
    assert adjustment.before.state is OrderStateName.SUBMITTED
    assert adjustment.after.state is OrderStateName.ACCEPTED
    assert adjustment.reason_code == "venue_state_adopted"


# --- partial fill during downtime -----------------------------------------

def test_partial_fill_during_downtime_resolves_from_venue_with_exact_decimals():
    local = _local(state=OrderStateName.ACCEPTED, remaining="1")

    report = engine().reconcile(
        [local],
        [_venue(state=OrderStateName.PARTIALLY_FILLED, filled="0.4", remaining="0.6")],
    )

    assert report.outcome_for("O-1") is ReconciliationOutcome.RESOLVED_FROM_VENUE
    after = report.adjustments[0].after
    assert after.state is OrderStateName.PARTIALLY_FILLED
    assert after.filled_quantity == Decimal("0.4")
    assert after.remaining_quantity == Decimal("0.6")


# --- cancel during downtime ----------------------------------------------

def test_cancel_during_downtime_resolves_from_venue():
    local = _local(state=OrderStateName.ACCEPTED, remaining="1")

    report = engine().reconcile(
        [local], [_venue(state=OrderStateName.CANCELED, remaining="1")]
    )

    assert report.outcome_for("O-1") is ReconciliationOutcome.RESOLVED_FROM_VENUE
    assert report.adjustments[0].after.state is OrderStateName.CANCELED
    assert report.requires_halt_new_risk is False


# --- local ahead of venue ------------------------------------------------

def test_local_ahead_of_venue_blocks_new_risk_without_overwriting():
    local = _local(state=OrderStateName.FILLED, filled="1", remaining="0")

    report = engine().reconcile(
        [local], [_venue(state=OrderStateName.ACCEPTED, filled="0", remaining="1")]
    )

    assert report.outcome_for("O-1") is ReconciliationOutcome.LOCAL_AHEAD_OF_VENUE
    assert report.local_ahead_of_venue == ["O-1"]
    assert report.requires_halt_new_risk is True
    assert report.adjustments == []


# --- duplicate venue reports ---------------------------------------------

def test_consistent_duplicate_venue_reports_are_deduplicated():
    local = _local(state=OrderStateName.SUBMITTED, attempted=True)
    duplicate = [_venue(state=OrderStateName.ACCEPTED), _venue(state=OrderStateName.ACCEPTED)]

    report = engine().reconcile([local], duplicate)

    assert report.outcome_for("O-1") is ReconciliationOutcome.VENUE_DUPLICATE_REPORT
    assert report.duplicate_reports == ["O-1"]
    assert report.requires_halt_new_risk is False
    assert report.adjustments[0].after.state is OrderStateName.ACCEPTED


def test_conflicting_duplicate_venue_reports_are_ambiguous():
    local = _local(state=OrderStateName.SUBMITTED, attempted=True)
    conflicting = [
        _venue(state=OrderStateName.ACCEPTED),
        _venue(state=OrderStateName.CANCELED, remaining="1"),
    ]

    report = engine().reconcile([local], conflicting)

    assert report.outcome_for("O-1") is ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION
    assert report.reason_for("O-1") == "conflicting_duplicate_venue_reports"
    assert report.requires_halt_new_risk is True
    assert report.adjustments == []


# --- unknown external order ----------------------------------------------

def test_unknown_external_order_is_escalated_never_adopted():
    report = engine().reconcile(
        [_local(state=OrderStateName.ACCEPTED)],
        [_venue(state=OrderStateName.ACCEPTED), _venue("O-EXTERNAL", state=OrderStateName.FILLED)],
    )

    assert report.outcome_for("O-EXTERNAL") is ReconciliationOutcome.UNKNOWN_EXTERNAL_ORDER
    assert report.unknown_external_orders == ["O-EXTERNAL"]
    assert report.requires_halt_new_risk is True
    # The external order is not adopted into local state and gets no adjustment.
    assert all(
        adjustment.before.order_reference != "O-EXTERNAL" for adjustment in report.adjustments
    )
    assert "O-EXTERNAL" not in report.order_ids


# --- operator-forced correction mode -------------------------------------

def test_venue_discrepancy_can_be_forced_through_the_operator_gate():
    strict = engine(allow_resolution_from_venue=False)
    local = _local(state=OrderStateName.SUBMITTED, attempted=True)

    report = strict.reconcile([local], [_venue(state=OrderStateName.ACCEPTED)])

    assert report.outcome_for("O-1") is ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION
    assert report.reason_for("O-1") == "venue_discrepancy_requires_operator"
    assert report.adjustments == []
    assert report.requires_halt_new_risk is True


# --- fail closed on unusable input ---------------------------------------

def test_duplicate_local_order_references_fail_closed():
    with pytest.raises(ReconciliationError):
        engine().reconcile(
            [_local(state=OrderStateName.ACCEPTED), _local(state=OrderStateName.SUBMITTED)],
            [],
        )


def test_local_duplicate_order_ids_for_distinct_references_are_allowed():
    shared_id = uuid4()
    report = engine().reconcile(
        [
            _local("O-1", state=OrderStateName.ACCEPTED, order_id=shared_id),
            _local("O-2", state=OrderStateName.ACCEPTED, order_id=shared_id),
        ],
        [_venue("O-1"), _venue("O-2")],
    )

    assert report.is_fully_resolved is True


# --- determinism ----------------------------------------------------------

def test_reconciliation_is_deterministic_for_identical_inputs():
    local = [
        _local("O-1", state=OrderStateName.SUBMITTED, attempted=True),
        _local("O-2", state=OrderStateName.CREATED),
        _local("O-3", state=OrderStateName.FILLED, filled="1"),
    ]
    venues = [
        _venue("O-1", state=OrderStateName.PARTIALLY_FILLED, filled="0.5", remaining="0.5"),
        _venue("O-4", state=OrderStateName.ACCEPTED),
    ]
    first = engine().reconcile(local, venues)
    second = engine().reconcile(local, venues)

    assert first.outcomes == second.outcomes
    assert first.reason_codes == second.reason_codes
    assert [a.reason_code for a in first.adjustments] == [a.reason_code for a in second.adjustments]
    assert first.unresolved_orders == second.unresolved_orders
    assert first.requires_halt_new_risk == second.requires_halt_new_risk


def test_affected_order_ids_are_the_known_phase3_identities():
    order_id = uuid4()
    report = engine().reconcile([_local(order_id=order_id)], [_venue()])

    assert report.affected_order_ids() == [order_id]


# --- the ONLY terminal -> FILLED release path: current venue evidence -------------

@pytest.mark.parametrize(
    "terminal", [OrderStateName.CANCELED, OrderStateName.EXPIRED, OrderStateName.REJECTED]
)
def test_current_venue_evidence_releases_a_terminal_order_to_filled(terminal):
    """Reconciliation, not historical reconstruction, is the terminal -> FILLED authority."""
    local = _local("O-1", state=terminal, filled="0", remaining="1.00000000", attempted=True)
    report = engine().reconcile(
        [local],
        [_venue("O-1", state=OrderStateName.FILLED, filled="1.00000000", remaining="0")],
    )

    assert report.outcome_for("O-1") is ReconciliationOutcome.RESOLVED_FROM_VENUE
    assert report.is_fully_resolved is True
    assert report.requires_halt_new_risk is False
    adjustment = report.adjustments[0]
    assert adjustment.source is AdjustmentSource.VENUE_REPORT
    assert adjustment.before.state is terminal
    assert adjustment.after.state is OrderStateName.FILLED
    assert adjustment.after.filled_quantity == Decimal("1.00000000")
    assert adjustment.after.remaining_quantity == 0


@pytest.mark.parametrize(
    "filled,remaining",
    [
        ("0.50000000", "0.50000000"),  # executedQty != Q (origQty == Q)
        ("0.60000000", "0.60000000"),  # implied origQty != Q
        ("1.00000000", "0.50000000"),  # inconsistent remaining for a FILLED report
    ],
)
def test_a_contradictory_filled_report_does_not_release_a_terminal_order(tmp_path, filled, remaining):
    """Guard: a FILLED report must prove executedQty == Q and origQty == Q against the anchors."""
    reference = "ct-44444444444444444444"
    order_id = deterministic_order_uuid(reference)
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        journal.append_batch(
            [
                OrderSubmissionAttempted(
                    event_id=uuid4(),
                    occurred_at=BASE,
                    correlation_id=uuid4(),
                    producer="application.production",
                    order_id=order_id,
                    symbol="ETHUSDT",
                    payload=OrderSubmissionAttemptPayload(
                        venue_reference=reference,
                        intent_id=uuid4(),
                        side="buy",
                        order_type="limit",
                        quantity=Decimal("1.00000000"),
                        limit_price=Decimal("100.00000000"),
                        time_in_force="gtc",
                        venue_producer=VENUE_PRODUCER,
                    ),
                ),
                OrderStateChanged(
                    event_id=uuid4(),
                    occurred_at=BASE,
                    correlation_id=uuid4(),
                    producer=VENUE_PRODUCER,
                    order_id=order_id,
                    symbol="ETHUSDT",
                    payload=OrderStateChangePayload(
                        state="canceled", reason="venue_status_canceled"
                    ),
                ),
            ]
        )
        run = RestartRecovery(
            engine=engine(),
            journal=journal,
            venue_sources=[
                StaticVenueStateSource(
                    entries=(
                        VenueOrderReport(
                            order_reference=reference,
                            venue_order_id="V-1",
                            state=OrderStateName.FILLED,
                            filled_quantity=Decimal(filled),
                            remaining_quantity=Decimal(remaining),
                        ),
                    ),
                    name="test.current_venue_evidence",
                )
            ],
        ).run(now=BASE)

    report = run.report
    # The refused report never reached the engine: the order did NOT resolve as FILLED, and the
    # contradiction keeps it unresolved/fenced instead.
    assert report.outcome_for(reference) is not ReconciliationOutcome.RESOLVED_FROM_VENUE
    assert report.is_fully_resolved is False
    assert report.requires_halt_new_risk is True
    assert report.reason_codes[reference].startswith("venue_filled_report")
    assert reference in report.unresolved_orders
    assert run.rebuilt_states[0].state is OrderStateName.CANCELED


def test_current_venue_evidence_releases_a_terminal_order_end_to_end(tmp_path):
    """The positive counterpart, through the real journal + reconciliation entry point."""
    reference = "ct-44444444444444444444"
    order_id = deterministic_order_uuid(reference)
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        journal.append_batch(
            [
                OrderSubmissionAttempted(
                    event_id=uuid4(),
                    occurred_at=BASE,
                    correlation_id=uuid4(),
                    producer="application.production",
                    order_id=order_id,
                    symbol="ETHUSDT",
                    payload=OrderSubmissionAttemptPayload(
                        venue_reference=reference,
                        intent_id=uuid4(),
                        side="buy",
                        order_type="limit",
                        quantity=Decimal("1.00000000"),
                        limit_price=Decimal("100.00000000"),
                        time_in_force="gtc",
                        venue_producer=VENUE_PRODUCER,
                    ),
                ),
                OrderStateChanged(
                    event_id=uuid4(),
                    occurred_at=BASE,
                    correlation_id=uuid4(),
                    producer=VENUE_PRODUCER,
                    order_id=order_id,
                    symbol="ETHUSDT",
                    payload=OrderStateChangePayload(
                        state="canceled", reason="venue_status_canceled"
                    ),
                ),
            ]
        )
        run = RestartRecovery(
            engine=engine(),
            journal=journal,
            venue_sources=[
                StaticVenueStateSource(
                    entries=(
                        VenueOrderReport(
                            order_reference=reference,
                            venue_order_id="V-1",
                            state=OrderStateName.FILLED,
                            filled_quantity=Decimal("1.00000000"),
                            remaining_quantity=Decimal("0"),
                        ),
                    ),
                    name="test.current_venue_evidence",
                )
            ],
        ).run(now=BASE)

    report = run.report
    assert report.outcome_for(reference) is ReconciliationOutcome.RESOLVED_FROM_VENUE
    assert report.is_fully_resolved is True
    assert report.requires_halt_new_risk is False
    adjustment = report.adjustments[0]
    assert adjustment.before.state is OrderStateName.CANCELED
    assert adjustment.after.state is OrderStateName.FILLED
    assert adjustment.after.filled_quantity == Decimal("1.00000000")
    assert adjustment.after.remaining_quantity == 0


# --- review G-4: our journaled venue order id is compared --------------------------

@pytest.mark.parametrize(
    ("local_id", "venue_id", "venue_state", "outcome", "reason"),
    [
        (
            "100", "777", OrderStateName.CANCELED,
            ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION, "venue_order_identity_mismatch",
        ),
        (
            "100", "777", OrderStateName.ACCEPTED,
            ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION, "venue_order_identity_mismatch",
        ),
        (
            "100", "100", OrderStateName.CANCELED,
            ReconciliationOutcome.RESOLVED_FROM_VENUE, "venue_state_adopted",
        ),
        ("100", "100", OrderStateName.ACCEPTED, ReconciliationOutcome.MATCHED, "local_and_venue_agree"),
        ("100", None, OrderStateName.ACCEPTED, ReconciliationOutcome.MATCHED, "local_and_venue_agree"),
    ],
    ids=[
        "another venue order cancelled",
        "another venue order open",
        "our venue order cancelled",
        "our venue order open",
        "report without a venue id",
    ],
)
def test_a_report_for_another_venue_order_never_resolves_ours(
    local_id, venue_id, venue_state, outcome, reason
):
    # Review G-4: the venue may answer for another order that reuses our client order id.
    local = replace(_local(attempted=True), venue_order_id=local_id)

    report = engine().reconcile([local], [_venue(state=venue_state, venue_order_id=venue_id)])

    pending = outcome is ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION
    assert (report.outcome_for("O-1"), report.reason_for("O-1")) == (outcome, reason)
    assert report.is_fully_resolved is not pending
    if pending:
        assert report.adjustments == []
        assert report.requires_halt_new_risk is True
