"""G4 fenced pre-start trade-backfill maintenance writer tests."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from crypto_trader.breakglass import OwnerKind, StaleWriterFenceError
from crypto_trader.events import OrderSubmissionAttemptPayload, OrderSubmissionAttempted
from crypto_trader.execution.binance_testnet.backfill import (
    TradeBackfillPlan,
    plan_trade_backfill,
    trade_backfill_plan_fingerprint,
)
from crypto_trader.execution.binance_testnet.models import TestnetOrder as VenueOrderSnapshot, VenueTrade
from crypto_trader.execution.binance_testnet.translate import (
    PRODUCER,
    deterministic_order_uuid,
    testnet_order_to_phase3_events as build_phase3_events,
)
from crypto_trader.execution.ports import OrderLifecycleState
from crypto_trader.persistence import SQLiteEventJournal, SnapshotError, SnapshotStore
from crypto_trader.production.backfill import TradeBackfillMaintenanceWriter
from crypto_trader.production.errors import ProductionStateError
from crypto_trader.tests.production_fixtures import control_for, operator_command

UTC = timezone.utc
BASE = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SYMBOL = "BTCUSDT"
REFERENCE = "ct-writer"
ORDER_ID = deterministic_order_uuid(REFERENCE)
CORRELATION_ID = UUID("60000000-0000-4000-8000-000000000001")
INTENT_ID = UUID("70000000-0000-4000-8000-000000000001")


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as active:
        yield active


@pytest.fixture
def snapshots(tmp_path: Path):
    with SnapshotStore(tmp_path / "snapshots.db") as active:
        yield active


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def attempt() -> OrderSubmissionAttempted:
    return OrderSubmissionAttempted(
        event_id=UUID("80000000-0000-4000-8000-000000000001"),
        occurred_at=BASE,
        correlation_id=CORRELATION_ID,
        producer="application.production",
        strategy_id="strategy.writer",
        order_id=ORDER_ID,
        symbol=SYMBOL,
        payload=OrderSubmissionAttemptPayload(
            venue_reference=REFERENCE,
            intent_id=INTENT_ID,
            side="buy",
            order_type="limit",
            quantity=Decimal("1"),
            limit_price=Decimal("100"),
            time_in_force="gtc",
            venue_producer=PRODUCER,
        ),
    )


def trade(*, qty="0.4", quote="40", seconds=1) -> VenueTrade:
    return VenueTrade(
        symbol=SYMBOL,
        venue_order_id="42",
        trade_id="7",
        price=Decimal("100"),
        quantity=Decimal(qty),
        quote_quantity=Decimal(quote),
        commission=Decimal("0.01"),
        commission_asset="USDT",
        trade_time=BASE + timedelta(seconds=seconds),
        is_buyer=True,
        is_maker=False,
    )


def order(
    *,
    executed="0.4",
    quote="40",
    status="PARTIALLY_FILLED",
    state=OrderLifecycleState.PARTIALLY_FILLED,
) -> VenueOrderSnapshot:
    return VenueOrderSnapshot(
        symbol=SYMBOL,
        client_order_id=REFERENCE,
        venue_order_id="42",
        status=status,
        state=state,
        original_quantity=Decimal("1"),
        executed_quantity=Decimal(executed),
        cummulative_quote_quantity=Decimal(quote),
        transact_time=BASE + timedelta(seconds=2),
    )


def build_plan(journal, *, venue_order=None, trades=None):
    venue_order = venue_order or order()
    trades = trades if trades is not None else [trade()]
    return plan_trade_backfill(
        journal.read_range(),
        orders={REFERENCE: venue_order},
        trades_by_reference={REFERENCE: trades},
        allowed_fee_assets={"BTC", "USDT"},
    )


def writer(journal, snapshots, state_dir, *, control=None):
    return TradeBackfillMaintenanceWriter(
        journal=journal,
        control=control or control_for(journal, state_dir),
        snapshot_store=snapshots,
        symbol=SYMBOL,
    )


def test_writer_claims_normal_lease_and_atomically_appends_exact_plan(
    journal, snapshots, state_dir,
):
    journal.append(attempt())
    plan = build_plan(journal)
    maintenance = writer(journal, snapshots, state_dir)

    report = maintenance.apply(plan)

    assert report.inserted_events == len(plan.events) == 2
    assert report.planned_event_ids == tuple(event.event_id for event in plan.events)
    assert report.fence_term == 1
    assert journal.count() == 3
    for expected in plan.events:
        assert journal.read_by_id(expected.event_id) == expected
    ownership = maintenance._control.ownership_store.require_fence()
    assert ownership.owner_id == "normal:trade-backfill-maintenance"


def test_writer_refuses_when_journal_changed_after_planning(journal, snapshots, state_dir):
    journal.append(attempt())
    plan = build_plan(journal)
    journal.append(plan.events[0])
    maintenance = writer(journal, snapshots, state_dir)
    before = journal.count()

    with pytest.raises(ProductionStateError, match="journal changed"):
        maintenance.apply(plan)

    assert journal.count() == before
    assert maintenance._control.ownership_store.read() is None


def test_writer_refuses_snapshot_that_would_hide_historical_backfill(
    journal, snapshots, state_dir,
):
    root = attempt()
    venue = order(
        executed="1",
        quote="100",
        status="FILLED",
        state=OrderLifecycleState.FILLED,
    )
    lifecycle = build_phase3_events(
        venue,
        correlation_id=CORRELATION_ID,
        occurred_at=BASE + timedelta(seconds=2),
    )[0]
    journal.append_batch([root, lifecycle])
    plan = build_plan(
        journal,
        venue_order=venue,
        trades=[trade(qty="1", quote="100", seconds=1)],
    )
    snapshots.save(
        "orders",
        {"synthetic": True},
        last_event_id=lifecycle.event_id,
        last_occurred_at_ns=int(lifecycle.occurred_at.timestamp() * 1_000_000_000),
    )
    maintenance = writer(journal, snapshots, state_dir)
    before = journal.count()

    with pytest.raises(ProductionStateError, match="snapshot would hide"):
        maintenance.apply(plan)

    assert journal.count() == before
    assert maintenance._control.ownership_store.read() is None


def test_writer_allows_snapshot_cursor_strictly_before_backfill_time(
    journal, snapshots, state_dir,
):
    root = attempt()
    journal.append(root)
    plan = build_plan(journal)
    snapshots.save(
        "orders",
        {"synthetic": True},
        last_event_id=root.event_id,
        last_occurred_at_ns=int(root.occurred_at.timestamp() * 1_000_000_000),
    )

    report = writer(journal, snapshots, state_dir).apply(plan)

    assert report.inserted_events == 2
    assert journal.count() == 3


def test_writer_refuses_plan_event_outside_its_single_symbol(
    journal, snapshots, state_dir,
):
    journal.append(attempt())
    plan = build_plan(journal)
    bad_event = plan.events[0].model_copy(update={"symbol": "ETHUSDT"})
    bad = TradeBackfillPlan(
        events=(bad_event, *plan.events[1:]),
        references=plan.references,
        source_entries=plan.source_entries,
        source_fingerprint=plan.source_fingerprint,
        plan_fingerprint=plan.plan_fingerprint,
    )
    maintenance = writer(journal, snapshots, state_dir)

    with pytest.raises(ProductionStateError, match="outside the writer symbol"):
        maintenance.apply(bad)

    assert journal.count() == 1
    assert maintenance._control.ownership_store.read() is None


def test_empty_plan_is_verified_without_claiming_writer_lease(
    journal, snapshots, state_dir,
):
    root = attempt()
    journal.append(root)
    empty = plan_trade_backfill(
        journal.read_range(),
        orders={
            REFERENCE: order(
                executed="0",
                quote="0",
                status="NEW",
                state=OrderLifecycleState.ACCEPTED,
            )
        },
        trades_by_reference={REFERENCE: []},
        allowed_fee_assets={"BTC", "USDT"},
    )
    maintenance = writer(journal, snapshots, state_dir)

    report = maintenance.apply(empty)

    assert report.changed is False
    assert report.fence_term is None
    assert journal.count() == 1
    assert maintenance._control.ownership_store.read() is None


def test_writer_refuses_drift_after_lease_claim_before_locked_recheck(
    journal, snapshots, state_dir, monkeypatch,
):
    journal.append(attempt())
    plan = build_plan(journal)
    control = control_for(journal, state_dir)
    maintenance = writer(journal, snapshots, state_dir, control=control)
    original_claim = control.claim_normal_writer

    def raced_claim(*, owner_id, reason_code="runtime_started"):
        guard = original_claim(owner_id=owner_id, reason_code=reason_code)
        journal.append(plan.events[0])
        return guard

    monkeypatch.setattr(control, "claim_normal_writer", raced_claim)

    with pytest.raises(ProductionStateError, match="journal changed"):
        maintenance.apply(plan)

    assert journal.read_by_id(plan.events[0].event_id) == plan.events[0]
    assert journal.read_by_id(plan.events[1].event_id) is None


def test_atomic_append_refuses_unfenced_journal_race_after_locked_recheck(
    journal, snapshots, state_dir, monkeypatch,
):
    journal.append(attempt())
    plan = build_plan(journal)
    maintenance = writer(journal, snapshots, state_dir)
    original = journal.append_batch_if_revision

    def raced_append(events, *, expected, require_all_new=False):
        with SQLiteEventJournal(journal.path) as other:
            other.append(plan.events[0])
        return original(
            events,
            expected=expected,
            require_all_new=require_all_new,
        )

    monkeypatch.setattr(journal, "append_batch_if_revision", raced_append)

    with pytest.raises(ProductionStateError, match="journal changed before the atomic append"):
        maintenance.apply(plan)

    assert journal.read_by_id(plan.events[0].event_id) == plan.events[0]
    assert journal.read_by_id(plan.events[1].event_id) is None


@pytest.mark.parametrize(
    ("last_event_id", "last_occurred_at_ns"),
    [
        (UUID("80000000-0000-4000-8000-000000000001"), None),
        (None, int(BASE.timestamp() * 1_000_000_000)),
    ],
)
def test_writer_refuses_incomplete_snapshot_cursor(
    journal, snapshots, state_dir, last_event_id, last_occurred_at_ns,
):
    journal.append(attempt())
    plan = build_plan(journal)
    snapshots.save(
        "orders",
        {"synthetic": True},
        last_event_id=last_event_id,
        last_occurred_at_ns=last_occurred_at_ns,
    )
    maintenance = writer(journal, snapshots, state_dir)

    with pytest.raises(ProductionStateError, match="incomplete or malformed"):
        maintenance.apply(plan)

    assert journal.count() == 1
    assert maintenance._control.ownership_store.read() is None


def test_writer_refuses_foreign_snapshot_cursor_id(journal, snapshots, state_dir):
    journal.append(attempt())
    plan = build_plan(journal)
    snapshots.save(
        "orders",
        {"synthetic": True},
        last_event_id=UUID("90000000-0000-4000-8000-000000000099"),
        last_occurred_at_ns=int(BASE.timestamp() * 1_000_000_000),
    )
    maintenance = writer(journal, snapshots, state_dir)

    with pytest.raises(ProductionStateError, match="not part of the planned journal view"):
        maintenance.apply(plan)

    assert journal.count() == 1


def test_writer_refuses_snapshot_cursor_timestamp_that_disagrees_with_event(
    journal, snapshots, state_dir,
):
    root = attempt()
    journal.append(root)
    plan = build_plan(journal)
    snapshots.save(
        "orders",
        {"synthetic": True},
        last_event_id=root.event_id,
        last_occurred_at_ns=int((BASE + timedelta(milliseconds=1)).timestamp() * 1_000_000_000),
    )

    with pytest.raises(ProductionStateError, match="does not match its event"):
        writer(journal, snapshots, state_dir).apply(plan)

    assert journal.count() == 1


def test_writer_refuses_snapshot_cursor_equal_to_earliest_backfill_time(
    journal, snapshots, state_dir,
):
    root = attempt()
    item = trade(seconds=1)
    venue = order()
    lifecycle = build_phase3_events(
        venue,
        correlation_id=CORRELATION_ID,
        occurred_at=item.trade_time,
    )[0]
    journal.append_batch([root, lifecycle])
    plan = build_plan(journal, venue_order=venue, trades=[item])
    snapshots.save(
        "orders",
        {"synthetic": True},
        last_event_id=lifecycle.event_id,
        last_occurred_at_ns=int(item.trade_time.timestamp() * 1_000_000_000),
    )

    with pytest.raises(ProductionStateError, match="snapshot would hide"):
        writer(journal, snapshots, state_dir).apply(plan)

    assert journal.count() == 2


def test_writer_takeover_invalidates_an_old_normal_writer_guard(
    journal, snapshots, state_dir,
):
    journal.append(attempt())
    plan = build_plan(journal)
    control = control_for(journal, state_dir)
    old_guard = control.claim_normal_writer(owner_id="normal:old-runtime")

    report = writer(
        journal, snapshots, state_dir, control=control
    ).apply(plan)

    assert report.inserted_events == 2
    with pytest.raises(StaleWriterFenceError):
        old_guard.require_current()


def test_writer_refuses_while_break_glass_epoch_is_open(
    journal, snapshots, state_dir,
):
    journal.append(attempt())
    control = control_for(journal, state_dir)
    control.handle(operator_command("halt_new_risk", seed="backfill-open-epoch"))
    plan = build_plan(journal)
    maintenance = writer(journal, snapshots, state_dir, control=control)
    before = journal.count()

    with pytest.raises(ProductionStateError, match="normal writer lease is unavailable"):
        maintenance.apply(plan)

    assert journal.count() == before


def test_second_apply_of_same_plan_is_refused_and_writes_nothing_more(
    journal, snapshots, state_dir,
):
    journal.append(attempt())
    plan = build_plan(journal)
    maintenance = writer(journal, snapshots, state_dir)
    first = maintenance.apply(plan)
    before = journal.count()

    with pytest.raises(ProductionStateError, match="journal changed"):
        maintenance.apply(plan)

    assert first.inserted_events == 2
    assert journal.count() == before


def test_writer_refuses_tampered_plan_fingerprint_before_claim(
    journal, snapshots, state_dir,
):
    journal.append(attempt())
    plan = build_plan(journal)
    tampered = replace(plan, plan_fingerprint="0" * 64)
    maintenance = writer(journal, snapshots, state_dir)

    with pytest.raises(ProductionStateError, match="fingerprint"):
        maintenance.apply(tampered)

    assert journal.count() == 1
    assert maintenance._control.ownership_store.read() is None


def test_writer_refuses_event_type_outside_backfill_contract_before_claim(
    journal, snapshots, state_dir,
):
    root = attempt()
    journal.append(root)
    plan = build_plan(journal)
    unauthorized = replace(plan, events=(root,))
    maintenance = writer(journal, snapshots, state_dir)

    with pytest.raises(ProductionStateError, match="event type outside"):
        maintenance.apply(unauthorized)

    assert journal.count() == 1
    assert maintenance._control.ownership_store.read() is None


def test_writer_refuses_planned_event_identity_already_in_source_revision(
    journal, snapshots, state_dir,
):
    journal.append(attempt())
    first_plan = build_plan(journal)
    existing_fill = first_plan.events[0]
    journal.append(existing_fill)
    source_plan = build_plan(journal)
    overlapping_events = (existing_fill,)
    overlapping = replace(
        source_plan,
        events=overlapping_events,
        plan_fingerprint=trade_backfill_plan_fingerprint(
            events=overlapping_events,
            references=source_plan.references,
            source_fingerprint=source_plan.source_fingerprint,
        ),
    )
    maintenance = writer(journal, snapshots, state_dir)

    with pytest.raises(ProductionStateError, match="overlaps an event identity"):
        maintenance.apply(overlapping)

    assert journal.count() == 2
    assert maintenance._control.ownership_store.read() is None


def test_emergency_takeover_between_claim_and_locked_section_is_typed_and_writes_no_plan_events(
    journal, snapshots, state_dir, monkeypatch,
):
    journal.append(attempt())
    plan = build_plan(journal)
    control = control_for(journal, state_dir)
    maintenance = writer(journal, snapshots, state_dir, control=control)
    original_claim = control.claim_normal_writer

    def claim_then_emergency(*, owner_id, reason_code="runtime_started"):
        guard = original_claim(owner_id=owner_id, reason_code=reason_code)
        control.handle(operator_command("halt_new_risk", seed="backfill-race-takeover"))
        return guard

    monkeypatch.setattr(control, "claim_normal_writer", claim_then_emergency)

    with pytest.raises(ProductionStateError, match="writer fence changed"):
        maintenance.apply(plan)

    assert all(journal.read_by_id(event.event_id) is None for event in plan.events)


def test_snapshot_store_failure_is_wrapped_as_production_state_error(
    journal, snapshots, state_dir, monkeypatch,
):
    journal.append(attempt())
    plan = build_plan(journal)
    maintenance = writer(journal, snapshots, state_dir)

    def broken_latest(_name):
        raise SnapshotError("synthetic snapshot failure")

    monkeypatch.setattr(snapshots, "latest", broken_latest)

    with pytest.raises(ProductionStateError, match="snapshot state could not be verified"):
        maintenance.apply(plan)

    assert journal.count() == 1
    assert maintenance._control.ownership_store.read() is None


def test_emergency_takeover_between_claim_and_lock_refuses_before_journal_append(
    journal, snapshots, state_dir, monkeypatch,
):
    journal.append(attempt())
    plan = build_plan(journal)
    control = control_for(journal, state_dir)
    maintenance = writer(journal, snapshots, state_dir, control=control)
    original_claim = control.claim_normal_writer

    def claim_then_emergency_takeover(*, owner_id, reason_code="runtime_started"):
        guard = original_claim(owner_id=owner_id, reason_code=reason_code)
        control.ownership_store.takeover(
            owner_id="emergency:test",
            owner_kind=OwnerKind.EMERGENCY,
            acquired_at=BASE + timedelta(seconds=10),
            reason_code="test_emergency_takeover",
        )
        return guard

    monkeypatch.setattr(control, "claim_normal_writer", claim_then_emergency_takeover)
    before = journal.count()

    with pytest.raises(ProductionStateError, match="writer fence changed"):
        maintenance.apply(plan)

    assert journal.count() == before
    assert control.ownership_store.require_fence().owner_kind is OwnerKind.EMERGENCY
