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

def test_r03_full_foundation_header_and_schema_roll_back_together(tmp_path, monkeypatch):
    path = tmp_path / "interrupted-genesis.db"
    connect = sqlite3.connect

    class InterruptedFoundation(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql.startswith("PRAGMA application_id="):
                assert self.in_transaction
                assert super().execute("PRAGMA synchronous").fetchone()[0] == 2
                raise RuntimeError("offline interrupted founding transaction")
            return result

    with monkeypatch.context() as fault:
        fault.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(
            *args, **kwargs, factory=InterruptedFoundation,
        ))
        with pytest.raises(RuntimeError, match="interrupted founding"):
            SQLiteEventJournal(path, venue_writes=True)
    with connect(path) as reader:
        assert reader.execute("PRAGMA application_id").fetchone()[0] == 0
        assert reader.execute("SELECT name FROM sqlite_schema").fetchall() == []
    with pytest.raises(JournalSchemaError, match="retrospective"):
        SQLiteEventJournal(path, venue_writes=True)


def test_append_and_read_back_round_trip(journal):
    event = decision()

    assert journal.append(event) is True
    assert journal.count() == 1
    assert journal.read_by_id(event.event_id) == event


def test_r03_full_foundation_active_connection_and_header(tmp_path, record_property):
    from crypto_trader.tests.test_testnet_durable_submission import _r03_journal
    with _r03_journal(tmp_path / "full.db") as journal:
        synchronous = journal._conn.execute("PRAGMA synchronous").fetchone()[0]
        application = journal._conn.execute("PRAGMA application_id").fetchone()[0]
        record_property("synchronous", synchronous)
        record_property("application_id", application)
        assert journal.journal_mode().lower() == "wal"
        assert synchronous == 2
        assert application == 0x43544647
        assert journal._conn.execute("PRAGMA user_version").fetchone()[0] == 0
        journal.append(decision())


def test_r03_refuse_full_stamp_on_populated_legacy_journal(tmp_path):
    from crypto_trader.tests.test_testnet_durable_submission import _r03_journal
    path = tmp_path / "legacy.db"
    original = decision()
    with SQLiteEventJournal(path) as journal:
        journal.append(original)
    with pytest.raises(JournalSchemaError):
        with _r03_journal(path):
            pass
    with SQLiteEventJournal(path) as journal:
        assert journal.read_by_id(original.event_id) == original
        assert journal.count() == 1
        assert journal._conn.execute("PRAGMA application_id").fetchone()[0] == 0
        assert journal._conn.execute("PRAGMA synchronous").fetchone()[0] == 1


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


@pytest.mark.parametrize("method", ["batch", "conditional", "conditional_all_new"])
@pytest.mark.parametrize("seeded", [False, True])
@pytest.mark.parametrize("reverse_conflict", [False, True])
def test_f5_intra_batch_conflicting_event_id_writes_nothing(
    journal, method, seeded, reverse_conflict, record_property,
):
    prior = decision(-1)
    if seeded:
        journal.append(prior)
    before = journal.revision()
    original = decision(0)
    conflicting = original.model_copy(update={
        "payload": StrategyDecisionPayload(
            action="sell", confidence="low", rationale="conflicting intra-batch content",
        ),
    })
    pair = [original, conflicting]
    if reverse_conflict:
        pair.reverse()
    fresh_one, fresh_two = decision(1), decision(2)
    events = [fresh_one, *pair, fresh_two]

    error = None
    try:
        if method == "batch":
            journal.append_batch(events)
        else:
            journal.append_batch_if_revision(
                events, expected=before, require_all_new=method == "conditional_all_new",
            )
    except Exception as exc:
        error = exc

    after = journal.revision()
    newly_persisted = [
        str(event.event_id) for event in (fresh_one, original, fresh_two)
        if journal.read_by_id(event.event_id) is not None
    ]
    observed = {
        "error_type": type(error).__name__,
        "error_message": str(error),
        "new_events": len(newly_persisted),
        "revision_unchanged": after == before,
    }
    record_property("f5_observed", str(observed))
    assert type(error) is EventIdConflictError, observed
    assert error.event_id == original.event_id
    assert str(error) == (
        f"event_id {original.event_id} already exists with different content; refusing to overwrite "
        "an append-only journal entry."
    )
    assert after == before
    assert journal.count() == int(seeded)
    assert newly_persisted == []
    assert journal.read_by_id(prior.event_id) == (prior if seeded else None)


def test_f5_intra_batch_identical_duplicates_remain_idempotent(journal):
    first, second = decision(0), decision(1)
    events = [first, first, second, second]

    assert journal.append_batch(events) == 2
    before = journal.revision()
    assert journal.append_batch(events) == 0
    assert journal.revision() == before
    assert journal.count() == 2
    assert journal.read_by_id(first.event_id) == first
    assert journal.read_by_id(second.event_id) == second


@pytest.mark.parametrize("require_all_new", [False, True])
def test_f5_conditional_identical_intra_batch_duplicates_keep_schema_rejection(
    journal, require_all_new,
):
    event = decision(0)
    before = journal.revision()

    with pytest.raises(JournalSchemaError) as caught:
        journal.append_batch_if_revision(
            [event, event], expected=before, require_all_new=require_all_new,
        )

    assert type(caught.value) is JournalSchemaError
    assert str(caught.value) == (
        f"conditional append batch contains duplicate event_id {event.event_id}."
    )
    assert journal.revision() == before
    assert journal.count() == 0


def test_f5_conditional_identical_redelivery_remains_idempotent(journal):
    event = decision(0)
    journal.append(event)
    before = journal.revision()

    result = journal.append_batch_if_revision([event], expected=before)

    assert result.inserted == 0
    assert result.before == result.after == journal.revision() == before
    assert journal.read_by_id(event.event_id) == event


class _InterleaveBeforeFirstInsert:
    """Delegating connection: runs ``interleave`` once, just before the first event INSERT."""

    def __init__(self, conn, interleave):
        self._real = conn
        self._interleave = interleave
        self.fired = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *args):
        return self._real.__exit__(*args)

    def execute(self, sql, *args, **kwargs):
        if not self.fired and sql.lstrip().startswith("INSERT INTO events"):
            self.fired = True
            self._interleave()
        return self._real.execute(sql, *args, **kwargs)


def test_f5_cross_writer_conflict_cannot_slip_between_batch_precheck_and_insert(
    tmp_path, record_property,
):
    path = tmp_path / "writers.db"
    original = decision(0)
    conflicting = original.model_copy(update={
        "payload": StrategyDecisionPayload(
            action="sell", confidence="low", rationale="conflicting cross-writer content",
        ),
    })
    with SQLiteEventJournal(path) as a, SQLiteEventJournal(path, timeout_seconds=0.1) as b:
        interleaved = {}

        def other_writer():
            try:
                interleaved["result"] = b.append_batch([conflicting])
            except Exception as exc:
                interleaved["error"] = exc

        conn = a._conn
        proxy = _InterleaveBeforeFirstInsert(conn, other_writer)
        a._conn = proxy
        try:
            inserted = a.append_batch([original])
        finally:
            a._conn = conn

        stored = a.read_by_id(original.event_id)
        error = interleaved.get("error")
        observed = {
            "interleaved": proxy.fired,
            "a_inserted": inserted,
            "stored_rationale": None if stored is None else stored.payload.rationale,
            "b_result": interleaved.get("result"),
            "b_error_type": type(error).__name__,
            "b_error_message": str(error),
        }
        record_property("f5_cross_writer_observed", str(observed))
        assert proxy.fired, observed
        assert inserted == 1, observed
        assert stored == original, observed
        assert "result" not in interleaved, observed
        assert type(error) is JournalSchemaError, observed
        assert str(error) == "journal batch failed: database is locked"
        assert type(error.__cause__) is sqlite3.OperationalError
        assert not b._conn.in_transaction

        with pytest.raises(EventIdConflictError) as caught:
            b.append_batch([conflicting])
        assert caught.value.event_id == original.event_id
        assert str(caught.value) == (
            f"event_id {original.event_id} already exists with different content; "
            "refusing to overwrite an append-only journal entry."
        )
        assert b.count() == 1
        assert b.read_by_id(original.event_id) == original


def test_f5_cross_writer_identical_redelivery_remains_idempotent(tmp_path):
    path = tmp_path / "writers.db"
    event = decision(0)
    with SQLiteEventJournal(path) as a, SQLiteEventJournal(path) as b:
        assert a.append_batch([event]) == 1
        before = b.revision()

        assert b.append_batch([event]) == 0

        assert b.revision() == before
        assert a.count() == b.count() == 1
        assert b.read_by_id(event.event_id) == event


def test_f5_batch_refuses_to_start_inside_an_existing_transaction(journal):
    event = decision(0)
    journal._conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(JournalSchemaError) as caught:
            journal.append_batch([event])
        assert type(caught.value) is JournalSchemaError
        assert str(caught.value) == "append batch refuses to start inside an existing transaction."
        assert journal._conn.in_transaction
    finally:
        journal._conn.rollback()
    assert journal.count() == 0
    assert journal.read_by_id(event.event_id) is None


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
