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


def bundle_case(*, quote="100000", decisions=None, data_feed=None, simulator=None, limits=None):
    simulator = simulator or aligned_runner()
    data_feed = data_feed or fx.feed("2700.00", "2701.00")
    decisions = decisions or fx.scripted(fx.buy_decision(), fx.hold_decision())
    config = fx.config(starting_equity_quote=quote)
    return FrozenRunInputs.capture(feed=data_feed, decisions=decisions, config=config,
        instrument=simulator.instrument, book_updates=simulator.book_updates,
        trades=simulator.trades, cash=SpotCashSettings(Decimal(quote)), limits=limits or fx.limits(),
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
    from crypto_trader.risk import load_limits
    session.limits = load_limits(doc["limits"])
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
    connection.close()
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


# Corrective-review probes mutate disposable test artifacts only, never evidence.
@pytest.fixture
def completed_artifacts(tmp_path):
    session, bundle, journal = prepared(tmp_path)
    try:
        session.run()
        path, spec = Path(journal.path), session.config.historical
    finally:
        journal.close()
    return path, spec, bundle


def rewrite_probe(path, change):
    """Keep checksums/revision consistent so semantic probes reach the target gate."""
    import sqlite3
    from crypto_trader.persistence.journal import _revision_fingerprint
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT event_id,payload_json FROM events ORDER BY rowid").fetchall()
        documents = [json.loads(raw) for _, raw in rows]
        change(documents)
        registry = historical_registry()
        raw_events = [registry.decode(json.dumps(doc)).to_json() for doc in documents[:-1]]
        entries = tuple(sorted(((registry.decode(raw).event_id, hashlib.sha256(raw.encode()).hexdigest())
                                for raw in raw_events), key=lambda pair: str(pair[0])))
        seal = documents[-1]
        seal["payload"]["pre_revision_entries"] = [(str(identity), digest) for identity, digest in entries]
        seal["payload"]["pre_revision_fingerprint"] = _revision_fingerprint(entries)
        hashes = dict(entries)
        seal["payload"]["event_order"] = [(doc["event_id"], hashes[__import__("uuid").UUID(doc["event_id"])])
                                            for doc in documents[:-1]]
        raw_events.append(registry.decode(json.dumps(seal)).to_json())
        for (identity, _), raw in zip(rows, raw_events):
            connection.execute("UPDATE events SET payload_json=?,payload_sha256=? WHERE event_id=?",
                               (raw, hashlib.sha256(raw.encode()).hexdigest(), identity))
    connection.close()


@pytest.mark.parametrize("damage", ["missing", "invalid_json", "changed_bytes", "wrong_run",
    "wrong_policy", "invalid_nonce", "missing_field", "duplicate_key", "wrong_replay"])
def test_completion_reader_refuses_invalid_reservation(completed_artifacts, damage):
    path, spec, _ = completed_artifacts
    marker = path.parent / "reservation.json"
    original = marker.read_bytes()
    doc = json.loads(original)
    if damage == "missing":
        marker.unlink()
    elif damage == "invalid_json":
        marker.write_bytes(b"{truncated")
    elif damage == "changed_bytes":
        marker.write_bytes(original + b"\n")
    else:
        if damage == "wrong_run": doc["run_id"] = str(uuid4())
        if damage == "wrong_policy": doc["policy"] = "Legacy"
        if damage == "invalid_nonce": doc["nonce"] = "invalid"
        if damage == "missing_field": doc.pop("nonce")
        if damage == "wrong_replay": doc["replay_of_run_id"] = str(uuid4())
        data = input_json(doc)
        if damage == "duplicate_key":
            data = data[:-1] + b',"policy":"R-02"}'
        marker.write_bytes(data)
        # An internally updated digest must not hide malformed reservation identity.
        rewrite_probe(path, lambda events: events[0]["payload"].update(
            reservation_sha256=hashlib.sha256(data).hexdigest()))
    before = {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    with pytest.raises(PaperStateError):
        read_historical_completion(path, spec)
    assert {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("destination", ["unreserved", "different_run", "renamed_journal"])
def test_completion_reader_refuses_unprovable_relocation(completed_artifacts, tmp_path, destination):
    import shutil
    path, spec, _ = completed_artifacts
    target = tmp_path / (f"r02-{uuid4()}" if destination == "different_run" else "relocated")
    if destination == "renamed_journal":
        target = tmp_path / "copy" / path.parent.name
    target.mkdir(parents=True)
    copied = target / ("renamed.sqlite" if destination == "renamed_journal" else "journal.sqlite")
    shutil.copyfile(path, copied)
    if destination != "unreserved":
        shutil.copyfile(path.parent / "reservation.json", target / "reservation.json")
    with pytest.raises(PaperStateError):
        read_historical_completion(copied, spec)


@pytest.mark.parametrize("field", ["positions", "orders", "fills", "decisions", "strategy_id", "engine_version"])
def test_completion_reader_rechecks_report_references(completed_artifacts, field):
    path, spec, _ = completed_artifacts
    def change(events):
        payload = events[-1]["payload"]
        report = json.loads(payload["report_json"])
        report[field] = {"ETHUSDT": "999"} if field == "positions" else [] if field in {"orders", "fills", "decisions"} else "wrong"
        raw = json.dumps(report, sort_keys=True, separators=(",", ":"))
        payload.update(report_json=raw, report_sha256=hashlib.sha256(raw.encode()).hexdigest())
    rewrite_probe(path, change)
    with pytest.raises(PaperStateError):
        read_historical_completion(path, spec)


@pytest.mark.parametrize("boundary", ["started", "completed"])
def test_completion_reader_rechecks_boundary_symbol(completed_artifacts, boundary):
    path, spec, _ = completed_artifacts
    rewrite_probe(path, lambda events: events[0 if boundary == "started" else -1].update(symbol="BTCUSDT"))
    with pytest.raises(PaperStateError):
        read_historical_completion(path, spec)


@pytest.mark.parametrize("damage", ["physical_order", "row_metadata", "journal_version"])
def test_completion_reader_checks_storage_evidence(completed_artifacts, damage):
    import sqlite3
    path, spec, _ = completed_artifacts
    with sqlite3.connect(path) as connection:
        if damage == "physical_order":
            connection.execute("UPDATE events SET rowid=(SELECT MAX(rowid)+1 FROM events) WHERE rowid=(SELECT MIN(rowid) FROM events)")
        elif damage == "row_metadata":
            connection.execute("UPDATE events SET correlation_id=?", (str(uuid4()),))
        else:
            connection.execute("UPDATE journal_meta SET value='999' WHERE key='schema_version'")
    connection.close()
    with pytest.raises(PaperStateError):
        read_historical_completion(path, spec)


def test_completion_diagnostics_do_not_create_or_modify_evidence(completed_artifacts):
    path, spec, _ = completed_artifacts
    before = {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    assert type(read_historical_completion(path, spec)) is PaperRunCompleted
    assert {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_completion_diagnostics_refuse_sidecars_without_ignoring_or_modifying_them(completed_artifacts, suffix):
    path, spec, _ = completed_artifacts
    Path(str(path) + suffix).write_bytes(b"unresolved evidence")
    before = {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    with pytest.raises(PaperStateError):
        read_historical_completion(path, spec)
    assert {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("damage", ["missing", "modified", "wrong_pin", "wrong_tree"])
def test_completion_reader_rechecks_independently_pinned_inputs(completed_artifacts, damage):
    path, spec, _ = completed_artifacts
    if damage == "missing": spec.inputs_path.unlink()
    elif damage == "modified": spec.inputs_path.write_bytes(spec.inputs_path.read_bytes() + b"\n")
    elif damage == "wrong_pin": spec = replace(spec, expected_inputs_sha256="0" * 64)
    else: spec = replace(spec, expected_code_tree="0" * 40)
    with pytest.raises(PaperStateError):
        read_historical_completion(path, spec)


@pytest.mark.parametrize("field", ["pre_revision_fingerprint", "event_order", "report_sha256", "economic_state",
                                  "economic_execution", "native_unsettled", "native_locked", "native_fee"])
def test_completion_reader_rechecks_every_seal_evidence_group(completed_artifacts, field):
    import sqlite3
    path, spec, _ = completed_artifacts
    with sqlite3.connect(path) as connection:
        identity, raw = connection.execute("SELECT event_id,payload_json FROM events WHERE event_type='paper.run.completed'").fetchone()
        doc = json.loads(raw)
        payload = doc["payload"]
        if field == "pre_revision_fingerprint": payload[field] = "0" * 64
        elif field == "event_order": payload[field][0][1] = "0" * 64
        elif field == "report_sha256": payload[field] = "0" * 64
        elif field == "economic_state": payload[field]["quote_total"] = "0"
        elif field == "economic_execution": payload[field] = []
        elif field == "native_unsettled": payload["native_cash"]["unsettled"] = True
        elif field == "native_locked": payload["native_cash"]["quote_locked"] = "1"
        else: payload["native_cash"]["fees_quote"] = "0"
        updated = historical_registry().decode(json.dumps(doc)).to_json()
        connection.execute("UPDATE events SET payload_json=?,payload_sha256=? WHERE event_id=?",
                           (updated, hashlib.sha256(updated.encode()).hexdigest(), identity))
    connection.close()
    with pytest.raises(PaperStateError):
        read_historical_completion(path, spec)


def test_r02_reservation_precedes_started_and_nonempty_attempt_never_starts_engine(tmp_path, monkeypatch):
    session, _, journal = prepared(tmp_path)
    path = Path(journal.path)
    assert path.parent.name == f"r02-{session.config.session_id}"
    assert (path.parent / "reservation.json").is_file() and journal.count() == 0
    # Simulate an unauthorized writer before the session's first CAS. The
    # application must refuse the polluted attempt, not append its started seal.
    journal.append(StartedProbe(occurred_at=fx.BASE, correlation_id=uuid4(),
        producer="test.foreign_writer", payload={"run_id": str(uuid4())}))
    before = journal.revision()
    called = []
    monkeypatch.setattr(session.simulator, "start_temporal", lambda: called.append(True))
    try:
        with pytest.raises(PaperStateError): session.run()
        assert not called and journal.revision() == before
    finally:
        journal.close()


@pytest.mark.parametrize("quantity", ["1", "0.3"])
def test_spot_three_way_cash_reconciliation_against_independent_oracle(tmp_path, quantity, record_property):
    from dataclasses import asdict
    simulator = aligned_runner()
    simulator.trades = simulator.trades[:2] + (replace(simulator.trades[2], size=Decimal(quantity)),)
    bundle = bundle_case(simulator=simulator, data_feed=fx.feed("2700.00"),
                         decisions=fx.scripted(fx.buy_decision()))
    session, _, journal = prepared(tmp_path, bundle)
    try:
        if quantity == "1": session.run()
        else:
            with pytest.raises(PaperStateError, match="unresolved"): session.run()
        fills = [e.payload for e in journal.read_range() if e.event_type == "execution.fill.recorded"]
        # A direct cash-flow oracle, independent of PositionLedger's calculations.
        acquired = sum((fill.quantity for fill in fills), Decimal("0"))
        fees = sum((fill.fee for fill in fills), Decimal("0"))
        spent = sum((fill.quantity * fill.price for fill in fills), Decimal("0"))
        assert acquired == Decimal(quantity)
        assert all(fill.fee_asset == "USDT" for fill in fills)
        locked = (Decimal("1") - acquired) * Decimal("2700")
        cash = Decimal("100000") - spent - fees
        oracle = {"base_total": acquired, "base_free": acquired, "base_locked": Decimal("0"),
            "quote_total": cash, "quote_free": cash - locked, "quote_locked": locked,
            "fees_quote": fees, "average_cost": spent / acquired, "realized_pnl_quote": -fees,
            "unrealized_pnl_quote": Decimal("2700") * acquired - spent,
            "equity_quote": cash + Decimal("2700") * acquired,
            "gross_exposure_quote": Decimal("2700") * acquired}
        ledger = session._r02_ledger.economic_state(Decimal("2700"))
        assert ledger == oracle
        native = asdict(session.simulator.cash_end_evidence())
        for field in ("base_total", "base_free", "base_locked", "quote_total", "fees_quote"):
            assert native[field] == oracle[field]
        # Distinct economic concepts, independently pinned by the live-engine
        # test below: remaining commitment vs actual native original-order lock.
        native_lock = Decimal("0") if quantity == "1" else Decimal("2700")
        assert native["quote_locked"] == native_lock
        assert native["quote_free"] == cash - native_lock
        assert oracle["quote_locked"] == (1 - acquired) * 2700
        assert native["account_type"] == "CASH" and native["allow_borrowing"] is False
        assert native["position_quantity"] == acquired
        assert native["unsettled"] is (quantity != "1")
        assert bool(session._reservations) is (quantity != "1")
        if quantity != "1":
            assert not any(e.event_type == "paper.run.completed" for e in journal.read_range())
        record_property("cash_oracle", input_json(oracle).decode())
        record_property("native_cash", input_json(native).decode())
    finally:
        journal.close()


def partial_competition_bundle(second_quantity="36"):
    simulator = aligned_runner()
    base = simulator.book_updates[0].ts_ns
    simulator.trades = simulator.trades[:2] + (replace(simulator.trades[2], size=Decimal("0.3")),)
    simulator.book_updates += tuple(replace(simulator.book_updates[0], ts_ns=base + i * 1_000_000_000,
        sequence=i + 1, action="UPDATE") for i in (1, 2))
    limits = fx.limits(max_order_notional_quote="1000000", max_symbol_exposure_quote="1000000",
        max_gross_exposure_quote="1000000", max_net_exposure_quote="1000000",
        max_strategy_exposure_quote="1000000", concentration_limit_pct="100")
    return bundle_case(simulator=simulator, data_feed=fx.feed("2700.00", "2700.00", "2700.00"),
        decisions=fx.scripted(fx.buy_decision(), fx.buy_decision(quantity=second_quantity), fx.hold_decision()),
        limits=limits)


def test_second_buy_between_ledger_and_native_free_is_denied_before_send(tmp_path, monkeypatch, record_property):
    session, _, journal = prepared(tmp_path, partial_competition_bundle())
    submit = session.simulator.submit_temporal
    sent = []
    def track(request, **kwargs):
        sent.append(request)
        return submit(request, **kwargs)
    monkeypatch.setattr(session.simulator, "submit_temporal", track)
    try:
        error = None
        try: session.run()
        except Exception as exc: error = exc
        assert len(sent) == 1, "cash admission must refuse the second request in our layer"
        assert isinstance(error, PaperStateError)
        rejections = [e for e in journal.read_range() if e.event_type == "safety.state.changed"]
        assert len(rejections) == 1
        proof = json.loads(rejections[0].payload.reason)
        assert Decimal(proof["ledger_free_quote"]) == Decimal("97299.838")
        assert Decimal(proof["native_free_quote"]) == Decimal("96489.838")
        assert Decimal(proof["admissible_free_quote"]) == Decimal("96489.838")
        assert proof["allowed"] is False
        assert proof["selection_reason"] == "native_free_lower"
        record_property("cash_rejection", json.dumps(proof, sort_keys=True))
        # No fabricated Hard Risk decision for the refused second intent.
        assert sum(e.event_type == "risk.decision.recorded" for e in journal.read_range()) == 1
        before = journal.revision()
        with pytest.raises(PaperStateError): session.run()
        assert journal.revision() == before
    finally:
        journal.close()


def test_missing_live_native_cash_blocks_first_send(tmp_path, monkeypatch):
    from crypto_trader.execution.nautilus.runtime import NautilusExecutionRuntime
    session, _, journal = prepared(tmp_path)
    sent = []
    def missing(*args, **kwargs): raise PaperStateError("native cash read unavailable")
    monkeypatch.setattr(NautilusExecutionRuntime, "cash_end_evidence", missing)
    monkeypatch.setattr(session.simulator, "submit_temporal", lambda *a, **k: sent.append(True))
    try:
        with pytest.raises(PaperStateError): session.run()
        assert not sent, "the final snapshot cannot replace a live pre-submission read"
    finally:
        journal.close()


def test_native_1231_original_quantity_lock_is_pinned_independently(tmp_path, monkeypatch, record_property):
    from importlib.metadata import version
    assert version("nautilus-trader") == "1.231.0", "Nautilus version changed: independently revalidate native lock behavior"
    session, _, journal = prepared(tmp_path, partial_competition_bundle())
    read = session.simulator.current_cash_reading
    observed = []
    def inspect(*, boundary_ns):
        reading = read(boundary_ns=boundary_ns)
        if session._r02_ledger.base_total == Decimal("0.3"):
            runtime = session.simulator._temporal_runtime
            orders = runtime._engine.cache.orders_open()
            assert len(orders) == 1
            order = orders[0]
            assert order.quantity.as_decimal() == 1 and order.filled_qty.as_decimal() == Decimal("0.3")
            assert order.leaves_qty.as_decimal() == Decimal("0.7")
            assert order.status.name == "PARTIALLY_FILLED"
            native = reading.native
            assert native.quote_total == Decimal("99189.838")
            assert native.quote_locked == Decimal("2700"), "Nautilus 1.231.0 original-order lock behavior changed"
            assert native.quote_free == Decimal("96489.838")
            assert session._r02_ledger.quote_locked == Decimal("1890")
            assert session._r02_ledger.quote_free == Decimal("97299.838")
            observed.append(True)
            record_property("pinned_native_cash", input_json(asdict(native)).decode())
        return reading
    from dataclasses import asdict
    monkeypatch.setattr(session.simulator, "current_cash_reading", inspect)
    try:
        with pytest.raises(PaperStateError): session.run()
        assert observed == [True]
    finally:
        journal.close()


@pytest.mark.parametrize("damage", ["boundary", "sequence", "runtime", "missing"])
def test_uncertain_live_reading_never_sends(tmp_path, monkeypatch, damage):
    session, _, journal = prepared(tmp_path)
    read = session.simulator.current_cash_reading
    sent = []
    def wrong(*, boundary_ns):
        value = read(boundary_ns=boundary_ns)
        if damage == "boundary": return replace(value, boundary_ns=boundary_ns - 1)
        if damage == "sequence": return replace(value, sequence=0)
        if damage == "runtime": return replace(value, runtime_id=None)
        return None
    monkeypatch.setattr(session.simulator, "current_cash_reading", wrong)
    monkeypatch.setattr(session.simulator, "submit_temporal", lambda *a, **k: sent.append(True))
    try:
        with pytest.raises(PaperStateError, match="cash"): session.run()
        assert not sent
        assert any(e.event_type == "safety.state.changed" for e in journal.read_range())
    finally:
        journal.close()


def test_frozen_inputs_refuse_changed_native_distribution_version(completed_artifacts, monkeypatch):
    from crypto_trader.paper import decisions as module
    path, spec, _ = completed_artifacts
    original = module.version
    monkeypatch.setattr(module, "version", lambda name: "9.0.0" if name == "nautilus-trader" else original(name))
    with pytest.raises(PaperStateError, match="provenance"):
        read_historical_completion(path, spec)


@pytest.mark.parametrize("field", ["native_free_quote", "admissible_free_quote", "unreflected_principal_quote", "boundary_ns", "native_unsettled_context"])
def test_completion_rechecks_cash_gate_context(completed_artifacts, field):
    path, spec, _ = completed_artifacts
    def change(events):
        event = next(e for e in events if e["event_type"] == "validity.disposition.recorded")
        proof = json.loads(event["payload"]["notes"])
        if field == "native_unsettled_context":
            proof["native_cash"]["unsettled"] = True
        else:
            proof[field] = proof[field] - 1 if field == "boundary_ns" else "1"
        event["payload"]["notes"] = input_json(proof).decode()
    rewrite_probe(path, change)
    with pytest.raises(PaperStateError): read_historical_completion(path, spec)


def test_competing_unsubmitted_order_is_reserved_once_before_third_send(tmp_path, monkeypatch, record_property):
    from datetime import timedelta
    doc = partial_competition_bundle().document()
    runner = aligned_runner()
    # Admit the queued first request on a book tick before the single queue-
    # clearing trade. No trade is replayed across the scripted decision wait.
    runner.book_updates += (replace(runner.book_updates[0], ts_ns=runner.book_updates[0].ts_ns + 1_000_000,
                                   sequence=2, action="UPDATE"),)
    runner.trades = (runner.trades[0], replace(runner.trades[2], size=Decimal("0.3")))
    completion = fx.BASE + timedelta(milliseconds=30)
    observations = fx.feed("2700.00", "2700.00", "2700.00").observations()
    feed = fx.FrozenMarketDataFeed(entries=(observations[0],
        replace(observations[1], observed_at=fx.BASE + timedelta(milliseconds=1)),
        replace(observations[2], observed_at=completion)))
    # The second decision finishes at the partial-fill tick; the third observes
    # that same processed boundary before any later tick can submit request two.
    source = fx.scripted(fx.buy_decision(), fx.buy_decision(quantity="0.1", decided_at=completion),
                         fx.buy_decision(quantity="35.7", decided_at=completion))
    from crypto_trader.risk import load_limits
    bundle = bundle_case(simulator=runner, data_feed=feed,
                         decisions=source, limits=load_limits(doc["limits"]))
    session, _, journal = prepared(tmp_path, bundle)
    submit, sent = session.simulator.submit_temporal, []
    def track(request, **kwargs):
        sent.append(request)
        return submit(request, **kwargs)
    monkeypatch.setattr(session.simulator, "submit_temporal", track)
    try:
        with pytest.raises(PaperStateError, match="cash"): session.run()
        assert [request.quantity for request in sent] == [Decimal("1"), Decimal("0.1")]
        proofs = sorted((json.loads(e.payload.notes) for e in journal.read_range()
            if e.event_type == "validity.disposition.recorded"), key=lambda p: p["market_reference"])
        second, third = proofs[1:]
        assert Decimal(second["unreflected_principal_quote"]) == 0
        assert Decimal(second["spendable_free_quote"]) == Decimal("96489.838")
        assert second["allowed"] is True
        assert Decimal(third["ledger_free_quote"]) == Decimal("97029.838")
        assert Decimal(third["native_free_quote"]) == Decimal("96489.838")
        assert Decimal(third["admissible_free_quote"]) == Decimal("96489.838")
        assert Decimal(third["unreflected_principal_quote"]) == Decimal("270")
        assert Decimal(third["spendable_free_quote"]) == Decimal("96219.838")
        assert third["allowed"] is False
        assert Decimal(third["required_quote_including_fee"]) < Decimal(third["admissible_free_quote"])
        record_property("competing_cash_evidence", json.dumps(proofs, sort_keys=True))
    finally:
        journal.close()


def test_native_cash_change_after_risk_is_audited_without_send_or_rollback(tmp_path, monkeypatch):
    from crypto_trader.execution.nautilus.runtime import NautilusExecutionRuntime
    original = NautilusExecutionRuntime.cash_end_evidence
    calls, sent = [], []
    def changed(runtime):
        value = original(runtime)
        calls.append(True)
        return value if len(calls) == 1 else replace(value, quote_free=value.quote_free - 1, quote_locked=value.quote_locked + 1)
    session, _, journal = prepared(tmp_path)
    monkeypatch.setattr(NautilusExecutionRuntime, "cash_end_evidence", changed)
    # Count the actual boundary call, not entry to the adapter's guarded method.
    monkeypatch.setattr(NautilusExecutionRuntime, "submit_timed", lambda *a, **k: sent.append(True))
    try:
        with pytest.raises(PaperStateError): session.run()
        assert not sent
        assert len(session._pending) == 1 and session._reservations
        assert sum(e.event_type == "risk.decision.recorded" for e in journal.read_range()) == 1
        assert sum(e.event_type == "risk.evaluation.recorded" for e in journal.read_range()) == 1
        assert any(e.event_type == "safety.state.changed" for e in journal.read_range()), "late cash uncertainty must be auditable"
        with pytest.raises(PaperStateError): session.run()
    finally:
        journal.close()
