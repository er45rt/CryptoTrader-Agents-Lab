"""R-02 durable run boundary, entirely offline; old evidence is never repaired."""
from pathlib import Path
from uuid import uuid4
from typing import Literal

import pytest

from crypto_trader.events import EventEnvelope, build_default_registry
from crypto_trader.paper import PaperStateError, ScriptedSimulationRunner
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx


class StartedProbe(EventEnvelope):
    event_type: Literal["paper.run.started"] = "paper.run.started"
    schema_version: Literal[1] = 1
    payload: dict[str, str]


def test_legacy_refuses_journal_after_durable_r02_started(tmp_path):
    # A separately registered marker proves the pre-existing boundary gap without
    # relying on an unimplemented production API. Any R-02 identity must fence it.
    registry = build_default_registry()
    registry.register_model(StartedProbe)
    with SQLiteEventJournal(tmp_path / "started.db", registry=registry) as journal:
        journal.append(StartedProbe(occurred_at=fx.BASE,
            correlation_id=uuid4(), producer="application.paper.r02",
            payload={"run_id": str(uuid4())}))
        before = journal.revision()
        session = fx.session(journal, decisions=fx.scripted(fx.buy_decision()),
            data_feed=fx.feed("2700"), simulator=ScriptedSimulationRunner())
        with pytest.raises(PaperStateError, match="R-02|reserved|historical"):
            session.run()
        assert journal.revision() == before


@pytest.mark.parametrize("marker_bytes", [b"", b'{"policy":"R-02"}'])
def test_legacy_refuses_empty_journal_in_reserved_r02_directory(tmp_path, marker_bytes):
    reserved = tmp_path / f"r02-{uuid4()}"
    reserved.mkdir()
    (reserved / "reservation.json").write_bytes(marker_bytes)
    with SQLiteEventJournal(reserved / "journal.sqlite") as journal:
        session = fx.session(journal, decisions=fx.scripted(fx.buy_decision()),
            data_feed=fx.feed("2700"), simulator=ScriptedSimulationRunner())
        with pytest.raises(PaperStateError, match="R-02|reserved|historical"):
            session.run()
        assert journal.count() == 0


from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from crypto_trader.paper.config import HistoricalRunSpec, SpotCashSettings
from crypto_trader.paper.decisions import FrozenRunInputs, input_json
from crypto_trader.paper.report import (
    reserve_historical_run, historical_registry, verify_historical_completion,
    PaperRunStarted, PaperRunCompleted,
    read_historical_completion,
)
from crypto_trader.tests.test_paper_nautilus import aligned_runner
from crypto_trader.validity import load_policy


TREE_FIXTURE = "1" * 40  # Explicit test provenance pin, not a claimed Git Tree.


def bundle_case(*, quote="100000", decisions=None, data_feed=None, simulator=None):
    simulator = simulator or aligned_runner()
    data_feed = data_feed or fx.feed("2700.00", "2701.00")
    decisions = decisions or fx.scripted(fx.buy_decision(), fx.hold_decision())
    config = fx.config(starting_equity_quote=quote)
    return FrozenRunInputs.capture(feed=data_feed, decisions=decisions, config=config,
        instrument=simulator.instrument, book_updates=simulator.book_updates,
        trades=simulator.trades, cash=SpotCashSettings(Decimal(quote)), limits=fx.limits(),
        validity_policy=load_policy(fx.VALIDITY_CONFIG),
        safety_mode="normal", code_tree=TREE_FIXTURE)


def prepared(tmp_path, bundle=None, *, run_id=None, replay_of=None):
    bundle = bundle or bundle_case()
    doc = bundle.document()
    path = bundle.save(tmp_path / "bundles")
    attempt = reserve_historical_run(tmp_path / "runs", run_id or uuid4(), replay_of_run_id=replay_of)
    config = fx.config(session_id=attempt.run_id,
        starting_equity_quote=doc["session"]["starting_equity_quote"],
        historical=HistoricalRunSpec(path, bundle.sha256, TREE_FIXTURE))
    session = fx.session(attempt.journal, cfg=config, decisions=bundle.decision_source(),
        data_feed=fx.FrozenMarketDataFeed(entries=bundle.observations()), simulator=aligned_runner())
    session.historical_attempt = attempt
    return session, bundle, attempt.journal


def test_native_cash_complete_and_independent_economic_reproduction(tmp_path):
    first, bundle, journal = prepared(tmp_path)
    try:
        report = first.run()
        seal = verify_historical_completion(journal, bundle)
        assert journal.read_by_id(seal.payload.event_order[0][0]).event_type == "paper.run.started"
        assert Decimal(seal.payload.economic_state["quote_total"]) == Decimal("97299.46")
        assert Decimal(seal.payload.economic_state["fees_quote"]) == Decimal("0.54")
        assert report.positions == {"ETHUSDT": Decimal("1")}
        second, _, another = prepared(tmp_path, bundle, replay_of=first.config.session_id)
        try:
            second.run()
            other_seal = verify_historical_completion(another, bundle)
            assert second.config.session_id != first.config.session_id
            assert journal.path != another.path
            assert other_seal.payload.economic_state == seal.payload.economic_state
            assert other_seal.payload.economic_execution == seal.payload.economic_execution
            assert another.read_by_id(other_seal.payload.started_event_id).payload.replay_of_run_id == first.config.session_id
        finally:
            another.close()
    finally:
        journal.close()


def test_crash_immediately_after_started_fences_r02_and_legacy(tmp_path, monkeypatch):
    session, bundle, journal = prepared(tmp_path)
    def crash():
        assert journal.count() == 1
        assert type(journal.read_range()[0]) is PaperRunStarted
        raise OSError("crash immediately after durable started")
    monkeypatch.setattr(session.simulator, "start_temporal", crash)
    try:
        with pytest.raises(OSError, match="durable started"):
            session.run()
        before = journal.revision()
        with pytest.raises(PaperStateError):
            session.run()
        legacy = fx.session(journal, cfg=fx.config(session_id=uuid4()),
            decisions=fx.scripted(fx.buy_decision()), data_feed=fx.feed("2700"),
            simulator=ScriptedSimulationRunner())
        with pytest.raises(PaperStateError):
            legacy.run()
        assert journal.revision() == before
        with pytest.raises(PaperStateError, match="missing|invalid"):
            verify_historical_completion(journal, bundle)
    finally:
        journal.close()


@pytest.mark.parametrize("boundary", ["directory_only", "marker_only", "empty_database"])
def test_crash_before_started_never_adopts_reserved_path(tmp_path, boundary):
    run_id = uuid4()
    root = tmp_path / "runs"
    directory = root / f"r02-{run_id}"
    directory.mkdir(parents=True)
    if boundary != "directory_only":
        (directory / "reservation.json").write_text("{}")
    if boundary == "empty_database":
        SQLiteEventJournal(directory / "journal.sqlite").close()
    original = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    with pytest.raises(PaperStateError, match="reserved"):
        reserve_historical_run(root, run_id)
    assert {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()} == original


@pytest.mark.parametrize("mutation", ["truncate", "decision_binding", "missing_hold", "wrong_cash", "rehash", "source_version"])
def test_corrupt_or_unapproved_bundle_never_starts_engine(tmp_path, monkeypatch, mutation):
    session, bundle, journal = prepared(tmp_path)
    called = []
    monkeypatch.setattr(session.simulator, "start_temporal", lambda: called.append(True))
    doc = bundle.document()
    if mutation == "truncate":
        blob = bundle.data[:10]
    else:
        if mutation in {"decision_binding", "rehash"}:
            doc["decisions"][0]["decision"]["quantity"] = "2"
        if mutation == "missing_hold":
            doc["decisions"].pop()
        if mutation == "wrong_cash":
            doc["cash"]["initial_quote"] = "1"
        if mutation == "source_version":
            doc["provenance"]["python_version"] = "different runtime"
        blob = input_json(doc)
    session.config.historical.inputs_path.write_bytes(blob)
    if mutation != "rehash":
        # Pin changed bytes deliberately to reach semantic validation where applicable.
        session.config = replace(session.config, historical=HistoricalRunSpec(
            session.config.historical.inputs_path, hashlib.sha256(blob).hexdigest(), TREE_FIXTURE))
    try:
        with pytest.raises(PaperStateError):
            session.run()
        assert not called and journal.count() == 0
    finally:
        journal.close()


def test_dynamic_and_default_decisions_cannot_be_frozen(tmp_path):
    class Dynamic:
        def decide(self, observation):
            raise AssertionError("dynamic callback must never execute")
    with pytest.raises(PaperStateError, match="dynamic"):
        bundle_case(decisions=Dynamic())
    with pytest.raises(PaperStateError, match="default|incomplete"):
        bundle_case(decisions=fx.scripted(fx.buy_decision(), default=fx.hold_decision()))


@pytest.mark.parametrize("after_commit", [False, True])
def test_completion_commit_failure_or_lost_ack_never_resumes(tmp_path, monkeypatch, after_commit):
    session, bundle, journal = prepared(tmp_path)
    append = journal.append_batch_if_revision
    def fail(events, **kwargs):
        if events[0].event_type == "paper.run.completed":
            if after_commit:
                append(events, **kwargs)
            raise OSError("uncertain completion commit")
        return append(events, **kwargs)
    monkeypatch.setattr(journal, "append_batch_if_revision", fail)
    try:
        with pytest.raises(OSError, match="uncertain"):
            session.run()
        before = journal.revision()
        if after_commit:
            assert type(verify_historical_completion(journal, bundle)) is PaperRunCompleted
        else:
            with pytest.raises(PaperStateError):
                verify_historical_completion(journal, bundle)
        with pytest.raises(PaperStateError):
            session.run()
        assert journal.revision() == before
    finally:
        journal.close()


def test_registry_independent_and_completed_journal_is_read_only_in_policy(tmp_path):
    default_before = build_default_registry().registered_pairs()
    session, bundle, journal = prepared(tmp_path)
    try:
        session.run()
        path = Path(journal.path)
        before = journal.revision()
        with pytest.raises(PaperStateError):
            session.run()
        assert journal.revision() == before
    finally:
        journal.close()
    with SQLiteEventJournal(path, registry=historical_registry()) as reopened:
        verify_historical_completion(reopened, bundle)
        legacy = fx.session(reopened, decisions=fx.scripted(fx.buy_decision()),
            data_feed=fx.feed("2700"), simulator=ScriptedSimulationRunner())
        with pytest.raises(PaperStateError):
            legacy.run()
    assert build_default_registry().registered_pairs() == default_before


def test_native_buy_sell_quote_fees_and_cash_pnl(tmp_path):
    from crypto_trader.execution import MarketTrade
    simulator = aligned_runner()
    base_ns = simulator.book_updates[0].ts_ns
    simulator.trades += tuple(MarketTrade(Decimal("2701.00"), Decimal(size), "BUYER",
        base_ns + offset, identity) for size, offset, identity in (
            ("100", 1_010_000_000, "R02-SELL-TRIGGER"),
            ("100", 1_020_000_000, "R02-SELL-CLEAR"),
            ("1", 1_030_000_000, "R02-SELL-FILL")))
    bundle = bundle_case(simulator=simulator, data_feed=fx.feed("2700.00", "2701.00"),
                         decisions=fx.scripted(fx.buy_decision(), fx.sell_decision(limit_price="2701.00")))
    session, _, journal = prepared(tmp_path, bundle)
    try:
        session.run()
        seal = verify_historical_completion(journal, bundle)
        state = seal.payload.economic_state
        assert Decimal(state["base_total"]) == 0
        assert Decimal(state["fees_quote"]) == Decimal("1.0802")
        assert Decimal(state["realized_pnl_quote"]) == Decimal("-0.0802")
        assert Decimal(state["equity_quote"]) == Decimal("99999.9198")
        assert len(seal.payload.economic_execution) == 2
    finally:
        journal.close()


@pytest.mark.parametrize("filled_qty", ["0", "0.3"])
def test_native_unfilled_or_partial_attempt_has_no_completion_or_release(tmp_path, filled_qty):
    simulator = aligned_runner()
    simulator.trades = simulator.trades[:2] + (() if filled_qty == "0" else
        (replace(simulator.trades[2], size=Decimal(filled_qty)),))
    bundle = bundle_case(simulator=simulator, data_feed=fx.feed("2700.00"),
                         decisions=fx.scripted(fx.buy_decision()))
    session, _, journal = prepared(tmp_path, bundle)
    try:
        with pytest.raises(PaperStateError, match="unresolved"):
            session.run()
        assert not any(event.event_type == "paper.run.completed" for event in journal.read_range())
        assert sum(qty for _, qty, _ in session._reservations.values()) == 1 - Decimal(filled_qty)
        assert session._r02_ledger.base_total == Decimal(filled_qty)
        assert session._r02_ledger.quote_locked == (1 - Decimal(filled_qty)) * 2700
        before = journal.revision()
        with pytest.raises(PaperStateError):
            session.run()
        assert journal.revision() == before
    finally:
        journal.close()


@pytest.mark.parametrize("after_commit", [False, True])
def test_uncertain_started_write_never_starts_engine(tmp_path, monkeypatch, after_commit):
    session, bundle, journal = prepared(tmp_path)
    append = journal.append_batch_if_revision
    called = []
    monkeypatch.setattr(session.simulator, "start_temporal", lambda: called.append(True))
    def fail(events, **kwargs):
        if after_commit:
            append(events, **kwargs)
        raise OSError("uncertain started commit")
    monkeypatch.setattr(journal, "append_batch_if_revision", fail)
    try:
        with pytest.raises(OSError):
            session.run()
        assert not called and journal.count() == int(after_commit)
    finally:
        journal.close()


def test_read_only_completion_and_corruption_detection(tmp_path):
    session, bundle, journal = prepared(tmp_path)
    session.run()
    path, spec = Path(journal.path), session.config.historical
    journal.close()
    before = path.read_bytes()
    seal = read_historical_completion(path, spec)
    assert path.read_bytes() == before
    assert seal.payload.inputs_sha256 == bundle.sha256
    # Corrupt only this disposable test database, never repository/evidence inputs.
    import sqlite3
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE events SET payload_sha256=? WHERE event_id=?",
                           ("0" * 64, str(seal.event_id)))
    with pytest.raises(PaperStateError, match="corruption"):
        read_historical_completion(path, spec)


@pytest.mark.parametrize("mutation", ["remove_stage", "reorder_risk", "extra_event", "report_hash"])
def test_completion_rejects_missing_conflicting_or_reordered_evidence(tmp_path, mutation):
    session, bundle, journal = prepared(tmp_path)
    try:
        session.run()
        events = journal.read_range()
        seal = next(e for e in events if type(e) is PaperRunCompleted)
        if mutation == "remove_stage":
            events = [e for e in events if e.event_id != seal.payload.event_order[2][0]]
        elif mutation == "extra_event":
            events.append(events[0].model_copy(update={"event_id": uuid4()}))
        else:
            payload = seal.payload.model_dump()
            if mutation == "report_hash":
                payload["report_sha256"] = "0" * 64
            else:
                order = list(payload["event_order"])
                order[1], order[2] = order[2], order[1]
                payload["event_order"] = tuple(order)
            events = [PaperRunCompleted(**{**seal.model_dump(), "payload": payload}) if e.event_id == seal.event_id else e for e in events]
        entries = tuple(sorted(((e.event_id, hashlib.sha256(e.to_json().encode()).hexdigest()) for e in events), key=lambda p: str(p[0])))
        from crypto_trader.persistence import JournalRevision
        from crypto_trader.persistence.journal import _revision_fingerprint
        revision = JournalRevision(entries, _revision_fingerprint(entries))
        class View:
            def read_range(self): return events
            def revision(self): return revision
        with pytest.raises(PaperStateError):
            verify_historical_completion(View(), bundle)
    finally:
        journal.close()


def test_live_decision_source_and_nonzero_cursor_never_execute(tmp_path, monkeypatch):
    session, bundle, journal = prepared(tmp_path)
    class Dynamic:
        def decide(self, observation):
            raise AssertionError("live inference must never run")
    session.decisions = Dynamic()
    try:
        with pytest.raises(PaperStateError, match="dynamic"):
            session.run()
        assert journal.count() == 0
    finally:
        journal.close()
    session, _, journal = prepared(tmp_path, bundle)
    try:
        with pytest.raises(PaperStateError, match="resume"):
            session.run(start_index=1)
        assert journal.count() == 0
    finally:
        journal.close()


def test_same_run_identity_cannot_be_reserved_at_another_root(tmp_path):
    identity = uuid4()
    attempt = reserve_historical_run(tmp_path / "first", identity)
    try:
        with pytest.raises(PaperStateError, match="Run ID"):
            reserve_historical_run(tmp_path / "other", identity)
    finally:
        attempt.journal.close()


def test_all_hold_and_risk_rejection_can_complete_without_orders(tmp_path):
    for name, decisions in (("hold", fx.scripted(fx.hold_decision(), fx.hold_decision())),
                            ("risk", fx.scripted(fx.buy_decision(quantity="2"), fx.hold_decision()))):
        bundle = bundle_case(decisions=decisions)
        session, _, journal = prepared(tmp_path / name, bundle)
        try:
            report = session.run()
            seal = verify_historical_completion(journal, bundle)
            assert seal.payload.economic_execution == ()
            assert Decimal(seal.payload.economic_state["quote_total"]) == Decimal("100000")
            assert not report.orders
        finally:
            journal.close()


@pytest.mark.parametrize("quote,decision", [("100", fx.buy_decision()), ("100000", fx.sell_decision())])
def test_unfunded_cash_decision_never_sends_native_request(tmp_path, monkeypatch, quote, decision):
    bundle = bundle_case(quote=quote, data_feed=fx.feed("2700.00"), decisions=fx.scripted(decision))
    session, _, journal = prepared(tmp_path, bundle)
    sent = []
    monkeypatch.setattr(session.simulator, "submit_temporal", lambda *a, **k: sent.append(True))
    try:
        with pytest.raises(PaperStateError, match="cash|inventory"):
            session.run()
        assert not sent
        assert not any(e.event_type == "paper.run.completed" for e in journal.read_range())
    finally:
        journal.close()
