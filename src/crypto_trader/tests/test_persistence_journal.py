"""Phase 5: durable append-only journal tests."""

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from crypto_trader.events import (
    EventValidationError,
    StrategyDecisionPayload,
    StrategyDecisionRecorded,
    TradeIntentCreated,
    TradeIntentPayload,
    UnknownEventTypeError,
)
from crypto_trader.persistence import (
    JOURNAL_SCHEMA_VERSION,
    EventIdConflictError,
    JournalRevisionMismatchError,
    JournalSchemaError,
    SQLiteEventJournal,
)

from decimal import Decimal

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
CORRELATION = UUID("11111111-1111-4111-8111-111111111111")


def decision(index: int = 0, *, correlation: UUID | None = None, sequence: int | None = None) -> StrategyDecisionRecorded:
    return StrategyDecisionRecorded(
        occurred_at=BASE + timedelta(seconds=index),
        correlation_id=correlation or CORRELATION,
        producer="agent.trading_plan_strategist",
        symbol="SYNTHUSDT",
        sequence_number=sequence,
        payload=StrategyDecisionPayload(action="buy", confidence="medium", rationale=f"event {index}"),
    )


def intent(index: int, *, correlation: UUID) -> TradeIntentCreated:
    return TradeIntentCreated(
        occurred_at=BASE + timedelta(seconds=index),
        correlation_id=correlation,
        producer="agent.trading_plan_strategist",
        symbol="SYNTHUSDT",
        payload=TradeIntentPayload(side="buy", quantity=Decimal("1.00000000"), order_type="market"),
    )


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as instance:
        yield instance


# --- basics ---

def test_append_and_read_back_round_trip(journal):
    event = decision()

    assert journal.append(event) is True
    assert journal.count() == 1
    assert journal.read_by_id(event.event_id) == event


def test_duplicate_append_is_idempotent(journal):
    event = decision()

    assert journal.append(event) is True
    assert journal.append(event) is False  # same logical event re-delivered
    assert journal.count() == 1


def test_same_event_id_with_different_content_fails_closed(journal):
    event = decision()
    conflicting = StrategyDecisionRecorded(
        event_id=event.event_id,
        occurred_at=event.occurred_at,
        correlation_id=event.correlation_id,
        producer=event.producer,
        symbol=event.symbol,
        payload=StrategyDecisionPayload(action="sell", confidence="low", rationale="different"),
    )

    journal.append(event)

    with pytest.raises(EventIdConflictError):
        journal.append(conflicting)
    assert journal.count() == 1


def test_batch_append_is_one_transaction_and_counts_new_rows(journal):
    events = [decision(i) for i in range(3)]

    assert journal.append_batch(events) == 3
    assert journal.count() == 3
    assert journal.append_batch(events) == 0  # idempotent batch
    assert journal.count() == 3


def test_batch_with_conflict_writes_nothing(journal):
    first = decision(0)
    journal.append(first)
    fresh_one, fresh_two = decision(1), decision(2)
    conflicting = StrategyDecisionRecorded(
        event_id=first.event_id,
        occurred_at=first.occurred_at,
        correlation_id=first.correlation_id,
        producer=first.producer,
        symbol=first.symbol,
        payload=StrategyDecisionPayload(action="sell", confidence="low", rationale="changed"),
    )

    with pytest.raises(EventIdConflictError):
        journal.append_batch([fresh_one, conflicting, fresh_two])

    assert journal.count() == 1, "a conflicting batch must not partially apply"
    assert journal.read_by_id(fresh_one.event_id) is None


def test_replay_order_is_deterministic_regardless_of_insertion_order(tmp_path: Path):
    events = [decision(2), decision(0), decision(1)]

    with SQLiteEventJournal(tmp_path / "a.db") as first_journal:
        first_journal.append_batch(events)
        first_order = [str(event.event_id) for event in first_journal.replay()]
    with SQLiteEventJournal(tmp_path / "b.db") as second_journal:
        second_journal.append_batch(list(reversed(events)))
        second_order = [str(event.event_id) for event in second_journal.replay()]

    expected = [str(event.event_id) for event in sorted(events, key=lambda item: item.occurred_at)]
    assert first_order == second_order == expected


def test_correlation_queries_return_only_that_chain_in_order(journal):
    other = uuid4()
    journal.append_batch([decision(0), decision(1, correlation=other), decision(2), intent(3, correlation=CORRELATION)])

    chain = journal.read_by_correlation(CORRELATION)
    other_chain = journal.read_by_correlation(other)

    assert [event.occurred_at for event in chain] == sorted(event.occurred_at for event in chain)
    assert {event.correlation_id for event in chain} == {CORRELATION}
    assert len(other_chain) == 1
    assert journal.read_by_correlation(CORRELATION, limit=2) == chain[:2]


def test_read_range_filters_by_occurred_at(journal):
    journal.append_batch([decision(i) for i in range(5)])

    window = journal.read_range(start=BASE + timedelta(seconds=1), end=BASE + timedelta(seconds=3))

    assert [event.occurred_at for event in window] == [
        BASE + timedelta(seconds=1),
        BASE + timedelta(seconds=2),
        BASE + timedelta(seconds=3),
    ]


def test_latest_sequence_number_is_scoped(journal):
    journal.append_batch(
        [
            decision(0, sequence=1),
            decision(1, sequence=2),
            decision(2, correlation=uuid4(), sequence=7),
        ]
    )

    assert journal.latest_sequence_number(symbol="SYNTHUSDT") == 7
    assert journal.latest_sequence_number(symbol="BTCUSDT") is None


# --- storage guarantees ---

def test_journal_uses_wal_and_records_schema_version(journal):
    assert journal.journal_mode().lower() == "wal"
    assert journal.schema_version() == JOURNAL_SCHEMA_VERSION


def test_unknown_future_schema_version_refuses_to_open(tmp_path: Path):
    path = tmp_path / "future.db"
    with SQLiteEventJournal(path):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE journal_meta SET value = '99' WHERE key = 'schema_version'")

    with pytest.raises(JournalSchemaError):
        SQLiteEventJournal(path)


def test_unknown_event_type_is_rejected_on_read(tmp_path: Path):
    path = tmp_path / "unknown.db"
    with SQLiteEventJournal(path) as journal:
        journal.append(decision())
        payload = journal._conn.execute("SELECT payload_json FROM events").fetchone()["payload_json"]
        journal._conn.execute(
            "UPDATE events SET payload_json = ?, event_type = 'unknown.event.recorded'",
            (payload.replace("strategy.decision.recorded", "unknown.event.recorded"),),
        )
        journal._conn.commit()

        with pytest.raises(UnknownEventTypeError):
            list(journal.replay())


def test_malformed_payload_is_rejected_on_read(tmp_path: Path):
    path = tmp_path / "malformed.db"
    with SQLiteEventJournal(path) as journal:
        journal.append(decision())
        journal._conn.execute(
            "UPDATE events SET payload_json = ?",
            ('{"event_type": "strategy.decision.recorded", "schema_version": 1}',),
        )
        journal._conn.commit()

        with pytest.raises(EventValidationError):
            list(journal.replay())


def test_journal_durably_survives_a_fresh_process(tmp_path: Path):
    db_path = tmp_path / "cross_process.db"
    child = Path(__file__).resolve().parent / "journal_writer_child.py"

    completed = subprocess.run(
        [sys.executable, "-B", str(child), str(db_path), "5"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stderr

    with SQLiteEventJournal(db_path) as journal:
        events = list(journal.replay())

    assert len(events) == 5
    assert [event.occurred_at for event in events] == sorted(event.occurred_at for event in events)
    assert {str(event.correlation_id) for event in events} == {"11111111-1111-4111-8111-111111111111"}


def test_journal_revision_tracks_exact_stored_payload_bytes(journal):
    first = decision(0)
    journal.append(first)

    revision = journal.revision()

    assert revision.count == 1
    assert revision.event_ids == (first.event_id,)
    assert len(revision.entries[0][1]) == 64
    assert len(revision.fingerprint) == 64

    journal.append(decision(1))
    assert journal.revision() != revision


def test_conditional_append_commits_only_when_revision_matches(journal):
    first = decision(0)
    fresh = decision(1)
    journal.append(first)
    expected = journal.revision()

    result = journal.append_batch_if_revision([fresh], expected=expected)

    assert result.before == expected
    assert result.inserted == 1
    assert result.after == journal.revision()
    assert result.after.count == 2
    assert journal.read_by_id(fresh.event_id) == fresh


def test_conditional_append_revision_mismatch_writes_nothing(journal):
    first = decision(0)
    unrelated = decision(1)
    planned = decision(2)
    journal.append(first)
    stale = journal.revision()
    journal.append(unrelated)
    before = journal.revision()

    with pytest.raises(JournalRevisionMismatchError):
        journal.append_batch_if_revision([planned], expected=stale)

    assert journal.revision() == before
    assert journal.read_by_id(planned.event_id) is None


def test_conditional_append_detects_cross_connection_drift_before_write(tmp_path: Path):
    path = tmp_path / "conditional-cross-connection.db"
    planned = decision(2)
    with SQLiteEventJournal(path) as first:
        first.append(decision(0))
        stale = first.revision()
        with SQLiteEventJournal(path) as second:
            second.append(decision(1))

        with pytest.raises(JournalRevisionMismatchError):
            first.append_batch_if_revision([planned], expected=stale)

        assert first.read_by_id(planned.event_id) is None
        assert first.count() == 2


def test_revision_refuses_payload_hash_corruption(journal):
    event = decision(0)
    journal.append(event)
    journal._conn.execute(
        "UPDATE events SET payload_json = ? WHERE event_id = ?",
        ('{"corrupt":true}', str(event.event_id)),
    )
    journal._conn.commit()

    with pytest.raises(JournalSchemaError, match="payload hash mismatch"):
        journal.revision()


def test_conditional_append_refuses_an_already_open_transaction_without_rolling_it_back(journal):
    journal.append(decision(0))
    expected = journal.revision()
    planned = decision(1)
    journal._conn.execute("BEGIN")
    try:
        with pytest.raises(JournalSchemaError, match="existing transaction"):
            journal.append_batch_if_revision([planned], expected=expected)
        assert journal._conn.in_transaction is True
        assert journal.read_by_id(planned.event_id) is None
    finally:
        journal._conn.rollback()
