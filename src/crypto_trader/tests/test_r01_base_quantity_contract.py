"""R-01 pre-fix evidence and acceptance regressions; entirely offline.

No native engine, venue, market connection, credentials, or trading is used.
Characterization controls identify the old defects; acceptance assertions are
expected to be RED until the owner approves the reservation design and repair.
"""

from dataclasses import replace
from decimal import Decimal
from threading import Event, Thread
from uuid import uuid4

import pytest
from pydantic import ValidationError

from crypto_trader.events import (
    RiskEvaluationRecorded, RiskEvaluationRecordedV2, RiskRuleResultPayload, decode_event,
)
from crypto_trader.paper import FrozenMarketDataFeed, PaperStateError, SimulationOutcome
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.persistence.errors import (
    EventIdConflictError, JournalRevisionMismatchError, JournalSchemaError,
)
from crypto_trader.risk import (
    HardRiskEngine, PendingIntent, PendingIntentRegistry, RiskRequest,
    RiskStateSnapshot, Side, record_risk_decision,
)
from crypto_trader.tests import paper_fixtures as fx


def risk_case(branch="ordinary"):
    pending = PendingIntentRegistry()
    short = branch == "attributed_short_buy"
    reducing = branch in {"reduce_only", "attributed_short_buy"}
    side = Side.BUY if short or branch == "ordinary" else Side.SELL
    position = Decimal("-1") if short else Decimal("1")
    engine = HardRiskEngine(
        fx.limits(reduce_only_mode=reducing), pending=pending,
        enforce_attributed_inventory=branch.startswith("attributed"),
    )
    request = RiskRequest(
        correlation_id=uuid4(), symbol="ETHUSDT", strategy_id="s1",
        intent_id=uuid4(), side=side, quantity=Decimal("0.1"), price=Decimal("100"),
    )
    state = RiskStateSnapshot(
        equity_quote=Decimal("10000"), peak_equity_quote=Decimal("10000"),
        realized_pnl_quote=Decimal("0"), gross_exposure_quote=Decimal("100"),
        net_exposure_quote=position * Decimal("100"), open_orders=0,
        open_positions=1, position_quantity={"ETHUSDT": position},
        strategy_position_quantity={"s1": position}, snapshot_ts=fx.BASE,
    )
    decision = engine.evaluate(request, state, now=fx.BASE)
    assert decision.allowed, decision.reason_codes
    return engine, request, state, pending, decision


def test_v1_still_rejects_base_quantity_and_keeps_old_units():
    fields = dict(rule_id="quantity", allowed=True, reason_code="ok",
                  actual=Decimal("0.1"), limit=Decimal("1"))
    with pytest.raises(ValidationError, match="unit"):
        RiskRuleResultPayload(**fields, unit="base_quantity")
    for unit in ("count", "quote_currency", "percent"):
        assert RiskRuleResultPayload(**fields, unit=unit).unit == unit


@pytest.mark.parametrize("branch", ["reduce_only", "attributed_sell", "attributed_short_buy"])
def test_v1_audit_failure_preserves_pending_and_blocks_conflict(tmp_path, monkeypatch, branch):
    engine, request, state, pending, decision = risk_case(branch)
    assert any(rule.unit == "base_quantity" for rule in decision.rule_results)
    before = pending.items()
    original = HardRiskEngine.to_phase3_evaluation

    def force_legacy_v1(*args, **kwargs):
        # Retain the proven failure boundary after the publisher moves to V2:
        # an explicit V1 decoder must still refuse base_quantity.
        data = original(*args, **kwargs).model_dump(mode="json")
        data["schema_version"] = 1
        return RiskEvaluationRecorded.model_validate(data)

    monkeypatch.setattr(HardRiskEngine, "to_phase3_evaluation", staticmethod(force_legacy_v1))
    with SQLiteEventJournal(tmp_path / "audit.db") as journal:
        with pytest.raises(ValidationError, match="unit"):
            record_risk_decision(journal, decision, occurred_at=fx.BASE)
        assert journal.count() == 0
        assert pending.items() == before and pending.has_intent(request.intent_id)
    opposite = replace(request, intent_id=uuid4(), side=Side.SELL if request.side is Side.BUY else Side.BUY)
    conflict = engine.evaluate(opposite, state, now=fx.BASE)
    assert not conflict.allowed
    assert "conflicting_intent" in conflict.reason_codes
    assert pending.items() == before


@pytest.mark.parametrize("branch", ["reduce_only", "attributed_sell", "attributed_short_buy"])
def test_quantity_decision_must_publish_v2_without_changing_economics(tmp_path, branch):
    _, request, _, pending, decision = risk_case(branch)
    with SQLiteEventJournal(tmp_path / "publish.db") as journal:
        _, audit_id = record_risk_decision(journal, decision, occurred_at=fx.BASE)
        audit = journal.read_by_id(audit_id)
        assert audit.schema_version == 2
        quantity_rules = [r for r in audit.payload.rule_results if r.unit == "base_quantity"]
        expected = [r for r in decision.rule_results if r.unit == "base_quantity"]
        assert [(r.rule_id, r.actual, r.limit) for r in quantity_rules] == [
            (r.rule_id, r.actual, r.limit) for r in expected
        ]
        assert audit.payload.quantity == request.quantity
        assert pending.has_intent(request.intent_id)


def test_mixed_v1_v2_replay_preserves_v1_bytes_and_rejects_unknown_version(tmp_path):
    _, _, _, _, decision = risk_case()
    v2 = HardRiskEngine.to_phase3_evaluation(decision, occurred_at=fx.BASE)
    data = v2.model_dump(mode="json")
    data.update(schema_version=1, event_id=str(uuid4()))
    v1 = RiskEvaluationRecorded.model_validate(data)
    original_json = v1.to_json()
    with SQLiteEventJournal(tmp_path / "mixed.db") as journal:
        journal.append_batch((v1, v2))
        assert type(journal.read_by_id(v1.event_id)) is RiskEvaluationRecorded
        assert type(journal.read_by_id(v2.event_id)) is RiskEvaluationRecordedV2
        assert journal.read_by_id(v1.event_id).to_json() == original_json
        assert [r.unit for r in journal.read_by_id(v1.event_id).payload.rule_results] == [
            r.unit for r in v1.payload.rule_results
        ]
    from crypto_trader.events import UnsupportedSchemaVersionError
    data["schema_version"] = 3
    with pytest.raises(UnsupportedSchemaVersionError):
        decode_event(data)


def reject_audit_insert(journal):
    # Real SQLite failure at the second risk INSERT, not a fake rollback.
    journal._conn.execute("""
        CREATE TEMP TRIGGER reject_risk_audit BEFORE INSERT ON events
        WHEN NEW.event_type = 'risk.evaluation.recorded'
        BEGIN SELECT RAISE(ABORT, 'injected second risk insert failure'); END
    """)


def test_second_risk_insert_failure_must_not_leave_one_durable_event(tmp_path, record_property):
    _, request, _, pending, decision = risk_case()
    with SQLiteEventJournal(tmp_path / "atomic.db") as journal:
        reject_audit_insert(journal)
        with pytest.raises(JournalSchemaError):
            record_risk_decision(journal, decision, occurred_at=fx.BASE)
        events = tuple(journal.read_range())
        record_property("durable_risk_types_after_failure", ",".join(e.event_type for e in events))
        assert pending.has_intent(request.intent_id)
        assert events == (), [e.event_type for e in events]


def test_existing_batch_rolls_back_real_second_insert_failure(tmp_path):
    _, _, _, _, decision = risk_case()
    with SQLiteEventJournal(tmp_path / "batch-control.db") as journal:
        # Capture the existing pair without changing the production recorder.
        record_risk_decision(journal, decision, occurred_at=fx.BASE)
        pair = tuple(journal.read_range())
    with SQLiteEventJournal(tmp_path / "rollback.db") as journal:
        reject_audit_insert(journal)
        with pytest.raises(JournalSchemaError):
            journal.append_batch(pair)
        assert journal.count() == 0


def test_batch_precheck_has_no_independent_writer_window(tmp_path):
    """Closure: the conflict precheck and the insert share one BEGIN IMMEDIATE transaction."""
    _, _, _, _, decision = risk_case()
    with SQLiteEventJournal(tmp_path / "pair.db") as source:
        record_risk_decision(source, decision, occurred_at=fx.BASE)
        pair = tuple(source.read_range())
    semantic = next(e for e in pair if e.event_type == "risk.decision.recorded")
    audit = next(e for e in pair if e.event_type == "risk.evaluation.recorded")
    conflicting = semantic.model_copy(update={
        "payload": semantic.payload.model_copy(update={"reason": "other writer"})
    })
    path = tmp_path / "writers.db"
    with SQLiteEventJournal(path) as a, SQLiteEventJournal(path, timeout_seconds=0.1) as b:
        conn = a._conn
        interleaved = []

        class InterleaveBeforeFirstInsert:
            def __getattr__(self, name):
                return getattr(conn, name)

            def __enter__(self):
                return conn.__enter__()

            def __exit__(self, *args):
                return conn.__exit__(*args)

            def execute(self, sql, *args, **kwargs):
                if not interleaved and sql.lstrip().startswith("INSERT INTO events"):
                    try:  # after A's SELECT, before its first INSERT
                        b.append(conflicting)
                    except JournalSchemaError as exc:
                        interleaved.append(exc)
                    else:
                        interleaved.append(None)
                return conn.execute(sql, *args, **kwargs)

        a._conn = InterleaveBeforeFirstInsert()
        try:
            inserted = a.append_batch((semantic, audit))
        finally:
            a._conn = conn
        assert (inserted, len(interleaved)) == (2, 1), interleaved
        assert type(interleaved[0]) is JournalSchemaError
        assert str(interleaved[0]) == "journal write failed: database is locked"
        assert a.read_by_id(semantic.event_id) == semantic
        assert a.read_by_id(audit.event_id).payload.reason_codes == decision.reason_codes
        with pytest.raises(EventIdConflictError) as caught:
            b.append(conflicting)
        assert caught.value.event_id == semantic.event_id


def test_existing_conditional_batch_rejects_changed_revision_without_writing(tmp_path):
    _, _, _, _, decision = risk_case()
    with SQLiteEventJournal(tmp_path / "pair.db") as source:
        record_risk_decision(source, decision, occurred_at=fx.BASE)
        pair = tuple(source.read_range())
    with SQLiteEventJournal(tmp_path / "revision.db") as a, SQLiteEventJournal(tmp_path / "revision.db") as b:
        revision = a.revision()
        unrelated = pair[0].model_copy(update={"event_id": uuid4()})
        b.append(unrelated)
        with pytest.raises(JournalRevisionMismatchError):
            a.append_batch_if_revision(pair, expected=revision)
        assert tuple(e.event_id for e in a.read_range()) == (unrelated.event_id,)


def test_risk_recorder_must_refuse_a_writer_during_audit_construction(tmp_path, monkeypatch):
    _, _, _, _, decision = risk_case()
    original = HardRiskEngine.to_phase3_evaluation
    with SQLiteEventJournal(tmp_path / "construction-race.db") as a, SQLiteEventJournal(tmp_path / "construction-race.db") as b:
        def interleave(*args, **kwargs):
            result = original(*args, **kwargs)
            b.append(result.model_copy(update={"event_id": uuid4(), "correlation_id": uuid4()}))
            return result

        monkeypatch.setattr(HardRiskEngine, "to_phase3_evaluation", staticmethod(interleave))
        with pytest.raises(JournalRevisionMismatchError):
            record_risk_decision(a, decision, occurred_at=fx.BASE)
        assert a.count() == 1


def test_session_revision_must_precede_risk_evaluation_after_preliminary_writes(tmp_path, monkeypatch):
    path = tmp_path / "evaluation-race.db"
    _, _, _, _, unrelated_decision = risk_case()
    unrelated = HardRiskEngine.to_phase3_evaluation(unrelated_decision, occurred_at=fx.BASE)
    original = HardRiskEngine.evaluate
    with SQLiteEventJournal(path) as journal, SQLiteEventJournal(path) as other:
        spy = SubmissionSpy()
        session = fx.session(journal, decisions=fx.scripted(fx.buy_decision()),
                             data_feed=fx.feed("2700"), simulator=spy)

        def concurrent_write(engine, request, state, **kwargs):
            preliminary = {e.event_type for e in journal.read_range()}
            assert {"strategy.decision.recorded", "trade.intent.created", "validity.disposition.recorded"} <= preliminary
            result = original(engine, request, state, **kwargs)
            other.append(unrelated)
            return result

        monkeypatch.setattr(HardRiskEngine, "evaluate", concurrent_write)
        with pytest.raises(JournalRevisionMismatchError):
            session.run()
        assert spy.requests == []
        assert len(session._pending.items()) == 1
        assert not any(e.event_type == "risk.decision.recorded" for e in journal.read_range())


class SubmissionSpy:
    """Records calls only; produces no orders, fills, or native engine state."""
    def __init__(self):
        self.requests = []

    def simulate(self, requests):
        self.requests.extend(requests)
        return SimulationOutcome()


class IndependentConnections:
    """Test-only adapter: each calling thread owns its SQLite connection."""
    def __init__(self, path):
        self.path = path
        with SQLiteEventJournal(path):
            pass

    def __getattr__(self, name):
        def invoke(*args, **kwargs):
            with SQLiteEventJournal(self.path) as journal:
                return getattr(journal, name)(*args, **kwargs)
        return invoke


class UncertainCommit:
    """Both events persist, but the acknowledgement is deliberately lost."""
    def __init__(self, journal):
        self.journal = journal

    def __getattr__(self, name):
        return getattr(self.journal, name)

    def append(self, event):
        result = self.journal.append(event)
        if event.event_type == "risk.evaluation.recorded":
            raise OSError("injected lost commit acknowledgement")
        return result

    def append_batch(self, events):
        self.journal.append_batch(events)
        raise OSError("injected lost commit acknowledgement")

    def append_batch_if_revision(self, events, **kwargs):
        self.journal.append_batch_if_revision(events, **kwargs)
        raise OSError("injected lost commit acknowledgement")


def test_missing_append_acknowledgement_stops_without_assuming_rollback(tmp_path):
    with SQLiteEventJournal(tmp_path / "missing-ack.db") as journal:
        class MissingAcknowledgement(UncertainCommit):
            def append_batch_if_revision(self, events, **kwargs):
                self.journal.append_batch_if_revision(events, **kwargs)
                return None

        spy = SubmissionSpy()
        session = fx.session(MissingAcknowledgement(journal),
                             decisions=fx.scripted(fx.buy_decision()),
                             data_feed=fx.feed("2700"), simulator=spy)
        with pytest.raises(JournalSchemaError, match="uncertain"):
            session.run()
        assert spy.requests == []
        assert len(session._pending.items()) == 1
        assert len([e for e in journal.read_range() if e.event_type.startswith("risk.")]) == 2
        replacement = fx.session(journal, decisions=fx.scripted(fx.buy_decision()),
                                 data_feed=fx.feed("2700"), simulator=spy)
        with pytest.raises(PaperStateError, match="stopped"):
            replacement.run()
        assert spy.requests == []


def test_ownership_is_held_through_submission_and_return(tmp_path):
    journal = IndependentConnections(tmp_path / "whole-session.db")
    other = fx.session(journal, decisions=fx.scripted(fx.hold_decision()),
                       data_feed=fx.feed("2700"), simulator=SubmissionSpy())

    class ProbeSubmission(SubmissionSpy):
        def simulate(self, requests):
            with pytest.raises(PaperStateError, match="owned"):
                other.run()
            return super().simulate(requests)

    spy = ProbeSubmission()
    session = fx.session(journal, decisions=fx.scripted(fx.buy_decision()),
                         data_feed=fx.feed("2700"), simulator=spy)
    session.run()
    assert len(spy.requests) == 1
    # A normal completion releases the lease, unlike a failed risk audit.
    other.decisions = fx.scripted(fx.buy_decision())
    other.run()


def test_memory_journal_wrapper_cannot_bypass_stopped_underlying_connection():
    with SQLiteEventJournal(":memory:") as journal:
        spy = SubmissionSpy()
        session = fx.session(UncertainCommit(journal), decisions=fx.scripted(fx.buy_decision()),
                             data_feed=fx.feed("2700"), simulator=spy)
        with pytest.raises(OSError, match="lost commit"):
            session.run()
        replacement = fx.session(journal, decisions=fx.scripted(fx.buy_decision()),
                                 data_feed=fx.feed("2700"), simulator=spy)
        with pytest.raises(PaperStateError, match="stopped"):
            replacement.run()
        assert spy.requests == []


@pytest.mark.parametrize("entry", ["_process", "_run_owned", "_process_owned"])
def test_stopped_session_private_pipeline_cannot_evaluate_again(tmp_path, monkeypatch, entry):
    with SQLiteEventJournal(tmp_path / "private-bypass.db") as journal:
        session = fx.session(UncertainCommit(journal), decisions=fx.scripted(fx.buy_decision()),
                             data_feed=fx.feed("2700"), simulator=SubmissionSpy())
        with pytest.raises(OSError):
            session.run()
        before = session._pending.items()

        def forbidden(*args, **kwargs):
            pytest.fail("stopped session evaluated another risk request")

        monkeypatch.setattr(HardRiskEngine, "evaluate", forbidden)
        with pytest.raises(PaperStateError, match="ownership"):
            if entry == "_process_owned":
                from crypto_trader.paper.accounting import PositionLedger
                observation = fx.feed("2700").entries[0]
                session._process_owned(index=0, observation=observation, decision=fx.buy_decision(),
                                       now=fx.BASE, market_state=fx.validity_state(fx.feed("2700")),
                                       ledger=PositionLedger(Decimal("100000")),
                                       requests=[], safety_events=[])
            else:
                getattr(session, entry)()
        assert session._pending.items() == before


@pytest.mark.parametrize("failure", ["build", "second_insert", "uncertain_commit"])
@pytest.mark.parametrize("new_instance", [False, True], ids=["same-session-object", "new-object-same-journal"])
def test_audit_failure_must_stop_session_without_retry_or_reservation_reset(
    tmp_path, monkeypatch, record_property, failure, new_instance,
):
    with SQLiteEventJournal(tmp_path / "stop.db") as journal:
        spy = SubmissionSpy()
        session = fx.session(journal, decisions=fx.scripted(fx.buy_decision()),
                             data_feed=fx.feed("2700"), simulator=spy)
        original_evaluate = HardRiskEngine.evaluate
        evaluations = []
        prior = PendingIntent(uuid4(), "BTCUSDT", "previous", Side.BUY,
                              Decimal("1"), Decimal("100"))

        def counted(engine, request, state, **kwargs):
            evaluations.append(request.intent_id)
            result = original_evaluate(engine, request, state, **kwargs)
            if len(evaluations) == 1:
                engine._pending.register(prior)
            return result

        monkeypatch.setattr(HardRiskEngine, "evaluate", counted)
        original_convert = HardRiskEngine.to_phase3_evaluation
        if failure == "build":
            def broken(*args, **kwargs):
                raise OSError("injected audit construction failure")
            monkeypatch.setattr(HardRiskEngine, "to_phase3_evaluation", staticmethod(broken))
        elif failure == "second_insert":
            reject_audit_insert(journal)
        else:
            session.journal = UncertainCommit(journal)

        with pytest.raises((OSError, JournalSchemaError)):
            session.run()
        assert spy.requests == [] and len(evaluations) == 1
        before = session._pending.items()
        assert session._pending.has_intent(prior.intent_id)
        assert len(before) == 2
        risk_types = [e.event_type for e in journal.read_range() if e.event_type.startswith("risk.")]
        record_property("risk_types_after_failure", ",".join(risk_types))
        if failure == "uncertain_commit":
            assert sorted(risk_types) == ["risk.decision.recorded", "risk.evaluation.recorded"]

        # Remove the injected fault; stopped session must still refuse any retry.
        monkeypatch.setattr(HardRiskEngine, "to_phase3_evaluation", staticmethod(original_convert))
        if failure == "second_insert":
            journal._conn.execute("DROP TRIGGER reject_risk_audit")
        session.journal = journal
        target = fx.session(journal, decisions=fx.scripted(fx.buy_decision()),
                            data_feed=fx.feed("2700"), simulator=spy) if new_instance else session
        later_error = None
        try:
            target.run()
        except PaperStateError as exc:
            later_error = exc
        witness = dict(refused=isinstance(later_error, PaperStateError),
                       evaluations=len(evaluations), requests=len(spy.requests),
                       prior_preserved=session._pending.has_intent(prior.intent_id))
        record_property("retry_witness", str(witness))
        assert witness == dict(refused=True, evaluations=1, requests=0, prior_preserved=True)
        assert session._pending.items() == before


def test_reentrant_run_must_be_refused_before_decision_or_state_reset(tmp_path, record_property):
    journal = IndependentConnections(tmp_path / "reentrant.db")
    spy = SubmissionSpy()
    session = fx.session(journal, decisions=fx.scripted(fx.hold_decision()),
                         data_feed=fx.feed("2700"), simulator=spy)
    calls = []
    nested_error = []
    prior = PendingIntent(uuid4(), "BTCUSDT", "previous", Side.BUY, Decimal("1"), Decimal("100"))

    class ReenteringDecisions:
        def decide(self, observation):
            calls.append(observation.market_reference)
            if len(calls) == 1:
                session._pending.register(prior)
                try:
                    session.run()
                except PaperStateError as exc:
                    nested_error.append(exc)
            return fx.hold_decision()

    session.decisions = ReenteringDecisions()
    session.run()
    witness = dict(refused=len(nested_error) == 1, decisions=len(calls),
                   prior_preserved=session._pending.has_intent(prior.intent_id))
    record_property("reentrant_witness", str(witness))
    assert witness == dict(refused=True, decisions=1, prior_preserved=True)
    assert spy.requests == []


def test_other_paper_object_cannot_send_conflicting_order_while_audit_is_pending(
    tmp_path, monkeypatch, record_property,
):
    journal = IndependentConnections(tmp_path / "held-audit.db")
    spy = SubmissionSpy()
    first = fx.session(journal, decisions=fx.scripted(fx.buy_decision()),
                       data_feed=fx.feed("2700"), simulator=spy)
    second_entry = replace(fx.feed("2700").entries[0], market_reference="independent#sell")

    class OppositeDecision:
        def decide(self, observation):
            return fx.sell_decision()

    second = fx.session(journal, decisions=OppositeDecision(),
                        data_feed=FrozenMarketDataFeed((second_entry,)), simulator=spy)
    entered, release = Event(), Event()
    original = HardRiskEngine.to_phase3_evaluation
    first_intent = []
    first_errors = []

    def hold_first_audit(decision, **kwargs):
        if decision.side is Side.BUY:
            first_intent.append(decision.intent_id)
            assert first._pending.has_intent(decision.intent_id)
            entered.set()
            assert release.wait(5), "test orchestration timed out"
        return original(decision, **kwargs)

    monkeypatch.setattr(HardRiskEngine, "to_phase3_evaluation", staticmethod(hold_first_audit))

    def run_first():
        try:
            first.run()
        except Exception as exc:
            first_errors.append(exc)

    worker = Thread(target=run_first, daemon=True)
    worker.start()
    assert entered.wait(5), "first risk approval did not reach audit construction"
    second_error = None
    try:
        second.run()
    except PaperStateError as exc:
        second_error = exc
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and first_errors == []
    witness = dict(refused=isinstance(second_error, PaperStateError),
                   submitted_sides=[r.side.value for r in spy.requests],
                   first_pending_preserved=first._pending.has_intent(first_intent[0]))
    record_property("held_audit_conflict_witness", str(witness))
    assert witness == dict(refused=True, submitted_sides=["buy"], first_pending_preserved=True)


@pytest.mark.parametrize("new_instance", [False, True], ids=["same-session-object", "two-objects-same-journal"])
def test_concurrent_run_must_refuse_before_decision_or_reservation_reset(tmp_path, record_property, new_instance):
    journal = IndependentConnections(tmp_path / "concurrent.db")
    entered, release = Event(), Event()
    spy = SubmissionSpy()
    session = fx.session(journal, decisions=fx.scripted(fx.hold_decision()),
                         data_feed=fx.feed("2700"), simulator=spy)
    calls, first_errors = [], []
    prior = PendingIntent(uuid4(), "BTCUSDT", "previous", Side.BUY, Decimal("1"), Decimal("100"))

    class PausedDecisions:
        def decide(self, observation):
            calls.append(observation.market_reference)
            if len(calls) == 1:
                session._pending.register(prior)
                entered.set()
                assert release.wait(5), "test orchestration timed out"
            return fx.hold_decision()

    session.decisions = PausedDecisions()
    target = fx.session(journal, decisions=session.decisions,
                        data_feed=fx.feed("2700"), simulator=spy) if new_instance else session

    def first_run():
        try:
            session.run()
        except Exception as exc:
            first_errors.append(exc)

    worker = Thread(target=first_run, daemon=True)
    worker.start()
    assert entered.wait(5), "first session did not reach controlled boundary"
    second_error = None
    try:
        target.run()
    except PaperStateError as exc:
        second_error = exc
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and first_errors == []
    witness = dict(refused=isinstance(second_error, PaperStateError), decisions=len(calls),
                   prior_preserved=session._pending.has_intent(prior.intent_id))
    record_property("concurrent_witness", str(witness))
    assert witness == dict(refused=True, decisions=1, prior_preserved=True)
    assert spy.requests == []
