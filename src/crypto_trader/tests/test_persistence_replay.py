"""Phase 5: snapshot, replay-tail and idempotent-consumer tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from crypto_trader.events import (
    StrategyDecisionPayload,
    StrategyDecisionRecorded,
    TradeIntentCreated,
    TradeIntentPayload,
)
from crypto_trader.persistence import (
    IdempotentConsumer,
    SQLiteEventJournal,
    SnapshotStore,
    rebuild_state,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
CORRELATION = UUID("11111111-1111-4111-8111-111111111111")


def decision(index: int) -> StrategyDecisionRecorded:
    return StrategyDecisionRecorded(
        occurred_at=BASE + timedelta(seconds=index),
        correlation_id=CORRELATION,
        producer="agent.trading_plan_strategist",
        symbol="SYNTHUSDT",
        payload=StrategyDecisionPayload(action="buy", confidence="medium", rationale=f"event {index}"),
    )


class CountingConsumer(IdempotentConsumer):
    """Deterministic consumer whose effect is a simple counter (mirrors domain state)."""

    def __init__(self) -> None:
        super().__init__()
        self.count_by_type: dict[str, int] = {}

    def apply(self, event) -> None:
        self.count_by_type[event.event_type] = self.count_by_type.get(event.event_type, 0) + 1

    def restore_state(self, state: dict) -> None:
        super().restore_state(state)
        self.count_by_type = {str(k): int(v) for k, v in state.get("count_by_type", {}).items()}

    def snapshot_state(self) -> dict:
        return {"count_by_type": self.count_by_type}


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as instance:
        yield instance


@pytest.fixture
def snapshots(tmp_path: Path):
    with SnapshotStore(tmp_path / "snapshots.db") as instance:
        yield instance


def test_snapshot_round_trip_records_journal_position(snapshots, journal):
    journal.append_batch([decision(0), decision(1)])
    last = list(journal.replay())[-1]

    saved = snapshots.save(
        "paper-session",
        {"count_by_type": {"strategy.decision.recorded": 2}},
        last_event_id=last.event_id,
        last_occurred_at_ns=int(last.occurred_at.timestamp() * 1_000_000_000),
        last_sequence_number=last.sequence_number,
    )
    loaded = snapshots.latest("paper-session")

    assert loaded == saved
    assert loaded.state == {"count_by_type": {"strategy.decision.recorded": 2}}
    assert loaded.last_event_id == last.event_id
    assert snapshots.count() == 1


def test_snapshot_plus_replay_tail_only_replays_the_difference(journal, snapshots):
    journal.append_batch([decision(i) for i in range(3)])
    consumer = CountingConsumer()
    first_rebuild = rebuild_state(journal, consumer)
    assert first_rebuild.events_applied == 3
    last_applied = journal.read_by_id(first_rebuild.last_event_id)
    snapshots.save(
        "session",
        consumer.snapshot_state(),
        last_event_id=first_rebuild.last_event_id,
        last_occurred_at_ns=first_rebuild.last_occurred_at_ns,
    )

    # Two new events arrive after the snapshot.
    journal.append_batch([decision(3), decision(4)])

    resumed = CountingConsumer()
    summary = rebuild_state(journal, resumed, snapshot=snapshots.latest("session"))

    assert summary.events_seen == 2, "only the tail after the snapshot is replayed"
    assert summary.events_applied == 2
    # 3 events restored from the snapshot + 2 replayed from the tail.
    assert resumed.count_by_type == {"strategy.decision.recorded": 5}
    assert last_applied is not None


def test_replay_is_deterministic_and_repeatable(journal):
    journal.append_batch([decision(i) for i in range(4)])

    first = CountingConsumer()
    rebuild_state(journal, first)
    second = CountingConsumer()
    rebuild_state(journal, second)

    assert first.count_by_type == second.count_by_type == {"strategy.decision.recorded": 4}
    assert first.applied_events == second.applied_events


def test_duplicate_event_application_has_no_extra_effect(journal):
    event = decision(0)
    journal.append(event)
    consumer = CountingConsumer()

    assert consumer.handle(event) is True
    assert consumer.handle(event) is False  # at-least-once redelivery
    assert consumer.count_by_type == {"strategy.decision.recorded": 1}
    assert consumer.applied_count == 1


def test_crash_between_append_and_apply_is_recovered_exactly_once(journal):
    """Simulates interruption after the journal write but before the consumer applied it."""
    events = [decision(i) for i in range(3)]
    journal.append_batch(events)

    crashed_consumer = CountingConsumer()
    crashed_consumer.handle(events[0])  # only the first effect happened before the crash

    # Restart: rebuild from the same journal.
    resumed_consumer = CountingConsumer()
    summary = rebuild_state(journal, resumed_consumer)

    assert summary.events_applied == 3
    assert resumed_consumer.count_by_type == {"strategy.decision.recorded": 3}
    assert summary.duplicates_skipped == 0, "a fresh consumer sees each event once"


def test_consumer_never_double_applies_when_journal_replayed_twice(journal):
    journal.append_batch([decision(0), decision(1)])
    consumer = CountingConsumer()

    rebuild_state(journal, consumer)
    second_pass = rebuild_state(journal, consumer)  # same consumer, replayed again

    assert second_pass.events_applied == 0
    assert second_pass.duplicates_skipped == 2
    assert consumer.count_by_type == {"strategy.decision.recorded": 2}


def test_at_least_once_delivery_preserves_state_across_mixed_event_types(journal):
    intent = TradeIntentCreated(
        occurred_at=BASE + timedelta(seconds=10),
        correlation_id=CORRELATION,
        causation_id=decision(0).event_id,
        producer="agent.trading_plan_strategist",
        symbol="SYNTHUSDT",
        payload=TradeIntentPayload(side="buy", quantity=Decimal("1.00000000"), order_type="market"),
    )
    journal.append_batch([decision(0), intent, decision(1)])

    consumer = CountingConsumer()
    rebuild_state(journal, consumer)
    rebuild_state(journal, consumer)

    assert consumer.count_by_type == {
        "strategy.decision.recorded": 2,
        "trade.intent.created": 1,
    }


def test_unknown_snapshot_name_returns_none(snapshots):
    assert snapshots.latest("never-saved") is None
