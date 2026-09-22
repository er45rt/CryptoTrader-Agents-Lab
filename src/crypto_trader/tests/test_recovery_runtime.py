"""Phase 8: restart recovery, journaling and reconciliation-runtime tests."""

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
)
from crypto_trader.persistence import SnapshotRecord, SnapshotStore, SQLiteEventJournal
from crypto_trader.recovery import (
    OrderStateName,
    OrderStateReconstructor,
    ReconciliationEngine,
    ReconciliationOutcome,
    ReconciliationSources,
    RecoveryConfigError,
    RecoveryError,
    ReconciliationError,
    RestartRecovery,
    StaticVenueStateSource,
    VenueOrderReport,
    load_policy,
    reconstruct_order_states,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
CORRELATION = UUID("22222222-2222-4222-8222-222222222222")
PRODUCER = "application.execution"


def state_event(order_id: UUID, state: str, *, index: int = 0, reason: str | None = None,
                symbol: str = "ETHUSDT") -> OrderStateChanged:
    return OrderStateChanged(
        occurred_at=BASE + timedelta(seconds=index),
        correlation_id=CORRELATION,
        producer=PRODUCER,
        order_id=order_id,
        symbol=symbol,
        payload=OrderStateChangePayload(state=state, reason=reason),
    )


def fill_event(order_id: UUID, quantity: str, *, remaining: str | None, index: int = 0,
               symbol: str = "ETHUSDT") -> ExecutionFillRecorded:
    return ExecutionFillRecorded(
        occurred_at=BASE + timedelta(seconds=index),
        correlation_id=CORRELATION,
        producer=PRODUCER,
        order_id=order_id,
        symbol=symbol,
        payload=FillRecordPayload(
            quantity=Decimal(quantity),
            price=Decimal("2700"),
            remaining_quantity=None if remaining is None else Decimal(remaining),
        ),
    )


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as instance:
        yield instance


def recovery(journal, entries=(), *, sources=None) -> RestartRecovery:
    engine = ReconciliationEngine(load_policy({"policy_id": "recovery-v1"}))
    if sources is None:
        sources = [StaticVenueStateSource(entries=tuple(entries))]
    return RestartRecovery(engine=engine, journal=journal, venue_sources=sources)


# --- reconstruction from the journal --------------------------------------

def test_reconstruction_rebuilds_lifecycle_and_fill_state(journal):
    order_id = uuid4()
    journal.append_batch(
        [
            state_event(order_id, "created", index=0),
            state_event(order_id, "submitted", index=1),
            state_event(order_id, "accepted", index=2),
            fill_event(order_id, "0.4", remaining="0.6", index=3),
        ]
    )

    reconstructor, summary = reconstruct_order_states(journal)
    states = reconstructor.states()

    assert summary.events_applied == 4
    assert len(states) == 1
    state = states[0]
    assert state.order_id == order_id
    assert state.order_reference == str(order_id)
    assert state.state is OrderStateName.PARTIALLY_FILLED
    assert state.filled_quantity == Decimal("0.4")
    assert state.remaining_quantity == Decimal("0.6")
    assert state.submission_attempted is True


def test_reconstruction_marks_a_created_order_as_not_submitted(journal):
    order_id = uuid4()
    journal.append(state_event(order_id, "created"))

    reconstructor, _ = reconstruct_order_states(journal)

    assert reconstructor.state_for(str(order_id)).submission_attempted is False


def test_reconstruction_marks_a_full_fill_as_filled(journal):
    order_id = uuid4()
    journal.append_batch(
        [
            state_event(order_id, "submitted", index=0),
            fill_event(order_id, "1", remaining="0", index=1),
        ]
    )

    reconstructor, _ = reconstruct_order_states(journal)
    state = reconstructor.state_for(str(order_id))

    assert state.state is OrderStateName.FILLED
    assert state.remaining_quantity == Decimal("0")


def test_reconstruction_fails_closed_on_a_fill_for_an_unknown_order(journal):
    journal.append(fill_event(uuid4(), "1", remaining="0"))

    with pytest.raises(ReconciliationError):
        reconstruct_order_states(journal)


def test_reconstruction_is_idempotent_for_redelivered_events():
    order_id = uuid4()
    event = state_event(order_id, "submitted")
    reconstructor = OrderStateReconstructor()

    assert reconstructor.handle(event) is True
    assert reconstructor.handle(event) is False
    assert len(reconstructor.applied_events) == 1


def test_reconstruction_round_trips_through_a_snapshot(journal):
    order_id = uuid4()
    journal.append_batch(
        [
            state_event(order_id, "submitted", index=0),
            state_event(order_id, "accepted", index=1),
        ]
    )
    reconstructor, summary = reconstruct_order_states(journal)
    snapshot = SnapshotRecord(
        name="orders",
        snapshot_schema=1,
        state=reconstructor.snapshot_state(),
        last_event_id=summary.last_event_id,
        last_occurred_at_ns=summary.last_occurred_at_ns,
        last_sequence_number=summary.last_sequence_number,
        created_at_iso=BASE.isoformat(),
    )

    # One more event lands after the snapshot; only the tail must be replayed.
    journal.append(fill_event(order_id, "0.5", remaining="0.5", index=2))
    restored, tail_summary = reconstruct_order_states(journal, snapshot=snapshot)

    assert tail_summary.events_seen == 1
    state = restored.state_for(str(order_id))
    assert state.state is OrderStateName.PARTIALLY_FILLED
    assert state.filled_quantity == Decimal("0.5")


def test_snapshot_store_carries_the_reconstruction_schema(tmp_path: Path, journal):
    order_id = uuid4()
    journal.append(state_event(order_id, "submitted"))
    reconstructor, summary = reconstruct_order_states(journal)

    with SnapshotStore(tmp_path / "snapshots.db") as store:
        store.save(
            "orders",
            reconstructor.snapshot_state(),
            last_event_id=summary.last_event_id,
            last_occurred_at_ns=summary.last_occurred_at_ns,
            last_sequence_number=summary.last_sequence_number,
        )
        record = store.latest("orders")

    assert record.state["reconstruction_schema"] == 1
    assert record.state["orders"][0]["filled_quantity"] == "0"


# --- restart recovery runtime --------------------------------------------

def test_crash_before_submission_recovers_and_resumes(journal):
    order_id = uuid4()
    journal.append(state_event(order_id, "created"))

    run = recovery(journal).run(now=BASE + timedelta(minutes=1))

    assert run.report.outcome_for(str(order_id)) is ReconciliationOutcome.NEVER_SUBMITTED
    assert run.recovery_state == "resolved"
    assert run.may_resume_trading is True
    assert run.blocked_orders == []
    assert [event.event_type for event in journal.read_range()][-3:] == [
        "recovery.state.changed",
    ] * 3


def test_ambiguous_submission_blocks_new_risk_and_is_not_failed(journal):
    order_id = uuid4()
    journal.append(state_event(order_id, "submitted"))

    run = recovery(journal).run(now=BASE + timedelta(minutes=1))

    assert run.report.outcome_for(str(order_id)) is (
        ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION
    )
    assert run.recovery_state == "failed"
    assert run.requires_halt_new_risk is True
    assert run.blocked_orders == [str(order_id)]
    assert run.may_resume_trading is False
    # The reconstructed local state is untouched: a timeout never becomes failed/rejected.
    assert run.report.adjustments == []
    assert run.rebuilt_states[0].state is OrderStateName.SUBMITTED
    terminal = journal.read_by_id(run.journaled_event_ids[2])
    assert terminal.payload.state == "failed"
    assert "unknown_pending_reconciliation" in terminal.payload.reason


def test_venue_accepted_with_missing_local_ack_resolves(journal):
    order_id = uuid4()
    journal.append(state_event(order_id, "submitted"))
    venue = [VenueOrderReport(str(order_id), "V-9", OrderStateName.ACCEPTED)]

    run = recovery(journal, venue).run(now=BASE + timedelta(minutes=1))

    assert run.report.outcome_for(str(order_id)) is ReconciliationOutcome.RESOLVED_FROM_VENUE
    assert run.recovery_state == "resolved"
    assert run.may_resume_trading is True


def test_partial_fill_during_downtime_is_recovered(journal):
    order_id = uuid4()
    journal.append_batch(
        [state_event(order_id, "submitted", index=0), state_event(order_id, "accepted", index=1)]
    )
    venue = [
        VenueOrderReport(
            str(order_id),
            "V-9",
            OrderStateName.PARTIALLY_FILLED,
            filled_quantity=Decimal("0.3"),
            remaining_quantity=Decimal("0.7"),
        )
    ]

    run = recovery(journal, venue).run(now=BASE + timedelta(minutes=1))
    adjustment = run.report.adjustments[0]

    assert adjustment.before.state is OrderStateName.ACCEPTED
    assert adjustment.after.state is OrderStateName.PARTIALLY_FILLED
    assert adjustment.after.filled_quantity == Decimal("0.3")
    assert run.recovery_state == "resolved"


def test_cancel_during_downtime_is_recovered(journal):
    order_id = uuid4()
    journal.append(state_event(order_id, "submitted"))
    venue = [
        VenueOrderReport(
            str(order_id),
            "V-9",
            OrderStateName.CANCELED,
            remaining_quantity=Decimal("1"),
        )
    ]

    run = recovery(journal, venue).run(now=BASE + timedelta(minutes=1))

    assert run.report.adjustments[0].after.state is OrderStateName.CANCELED
    assert run.recovery_state == "resolved"


def test_local_ahead_of_venue_blocks_new_risk(journal):
    order_id = uuid4()
    journal.append_batch(
        [
            state_event(order_id, "submitted", index=0),
            fill_event(order_id, "1", remaining="0", index=1),
        ]
    )
    venue = [VenueOrderReport(str(order_id), "V-9", OrderStateName.ACCEPTED)]

    run = recovery(journal, venue).run(now=BASE + timedelta(minutes=1))

    assert run.report.outcome_for(str(order_id)) is ReconciliationOutcome.LOCAL_AHEAD_OF_VENUE
    assert run.recovery_state == "failed"
    assert run.requires_halt_new_risk is True
    assert run.report.adjustments == []


def test_two_agreeing_sources_are_deduplicated(journal):
    order_id = uuid4()
    journal.append(state_event(order_id, "submitted"))
    entry = VenueOrderReport(str(order_id), "V-9", OrderStateName.ACCEPTED)
    sources = [
        StaticVenueStateSource(entries=(entry,), name="venue"),
        StaticVenueStateSource(entries=(entry,), name="nautilus_cache"),
    ]

    run = recovery(journal, sources=sources).run(now=BASE + timedelta(minutes=1))

    assert run.report.outcome_for(str(order_id)) is ReconciliationOutcome.VENUE_DUPLICATE_REPORT
    assert run.recovery_state == "resolved"


def test_two_conflicting_sources_are_ambiguous(journal):
    order_id = uuid4()
    journal.append(state_event(order_id, "submitted"))
    sources = [
        StaticVenueStateSource(
            entries=(VenueOrderReport(str(order_id), "V-9", OrderStateName.ACCEPTED),),
            name="venue",
        ),
        StaticVenueStateSource(
            entries=(
                VenueOrderReport(
                    str(order_id), "V-9", OrderStateName.CANCELED, remaining_quantity=Decimal("1")
                ),
            ),
            name="nautilus_cache",
        ),
    ]

    run = recovery(journal, sources=sources).run(now=BASE + timedelta(minutes=1))

    assert run.report.outcome_for(str(order_id)) is (
        ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION
    )
    assert run.recovery_state == "failed"
    assert run.report.adjustments == []


def test_unknown_external_order_blocks_new_risk(journal):
    order_id = uuid4()
    journal.append(state_event(order_id, "submitted"))
    venue = [
        VenueOrderReport(str(order_id), "V-9", OrderStateName.ACCEPTED),
        VenueOrderReport("EXTERNAL-1", "V-77", OrderStateName.FILLED, filled_quantity=Decimal("2")),
    ]

    run = recovery(journal, venue).run(now=BASE + timedelta(minutes=1))

    assert run.report.unknown_external_orders == ["EXTERNAL-1"]
    assert run.requires_halt_new_risk is True
    assert run.recovery_state == "failed"


def test_recovery_run_is_journaled_once_per_logical_run(journal):
    order_id = uuid4()
    journal.append(state_event(order_id, "submitted"))
    event_ids = (uuid4(), uuid4(), uuid4())
    runtime = recovery(journal)

    runtime.run(now=BASE + timedelta(minutes=1), event_ids=event_ids)
    count_after_first = journal.count()
    runtime.run(now=BASE + timedelta(minutes=1), event_ids=event_ids)

    assert journal.count() == count_after_first
    by_id = {event.event_id: event for event in journal.read_range()}
    assert [by_id[event_id].payload.state for event_id in event_ids] == [
        "detected",
        "reconciling",
        "failed",
    ]


def test_recovery_events_decode_through_the_phase3_registry(journal):
    order_id = uuid4()
    journal.append(state_event(order_id, "submitted"))

    run = recovery(journal).run(now=BASE + timedelta(minutes=1))
    decoded = journal.read_by_id(run.journaled_event_ids[0])

    assert decoded.event_type == "recovery.state.changed"
    assert decoded.payload.state == "detected"
    assert decoded.payload.affected_order_ids == [order_id]


def test_recovery_requires_at_least_one_venue_source(journal):
    engine = ReconciliationEngine(load_policy({"policy_id": "recovery-v1"}))
    with pytest.raises(ValueError):
        RestartRecovery(engine=engine, journal=journal, venue_sources=[])


def test_recovery_never_reads_the_wall_clock(journal):
    """Same inputs + same ``now`` -> identical journaled reasons (deterministic)."""
    order_id = uuid4()
    journal.append(state_event(order_id, "submitted"))
    now = BASE + timedelta(minutes=2)
    first_ids = (uuid4(), uuid4(), uuid4())
    second_ids = (uuid4(), uuid4(), uuid4())
    runtime = recovery(journal)

    runtime.run(now=now, event_ids=first_ids)
    runtime.run(now=now, event_ids=second_ids)
    first = journal.read_by_id(first_ids[1])
    second = journal.read_by_id(second_ids[1])

    assert first.payload.reason == second.payload.reason
    assert first.occurred_at == second.occurred_at


def test_reconciliation_sources_combines_sources(journal):
    order_id = uuid4()
    combined = ReconciliationSources(
        sources=(
            StaticVenueStateSource(entries=(VenueOrderReport(str(order_id), "V", OrderStateName.ACCEPTED),)),
            StaticVenueStateSource(entries=()),
        )
    )

    assert len(combined.reports()) == 1


def test_recovery_run_requires_an_explicit_correlation_for_multiple_orders(journal):
    first, second = uuid4(), uuid4()
    journal.append_batch(
        [
            OrderStateChanged(
                occurred_at=BASE,
                correlation_id=CORRELATION,
                producer=PRODUCER,
                order_id=first,
                symbol="ETHUSDT",
                payload=OrderStateChangePayload(state="submitted"),
            ),
            OrderStateChanged(
                occurred_at=BASE + timedelta(seconds=1),
                correlation_id=uuid4(),
                producer=PRODUCER,
                order_id=second,
                symbol="ETHUSDT",
                payload=OrderStateChangePayload(state="submitted"),
            ),
        ]
    )

    with pytest.raises(RecoveryConfigError):
        recovery(journal).run(now=BASE + timedelta(minutes=1))


def test_reconstruct_order_states_rejects_an_unsupported_snapshot_schema(journal):
    reconstructor = OrderStateReconstructor()

    with pytest.raises(RecoveryError):
        reconstructor.restore_state({"reconstruction_schema": 99, "orders": []})
