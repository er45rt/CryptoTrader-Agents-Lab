"""Phase 10: shadow/paper session tests (decision path, isolation, report contents)."""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from crypto_trader.events import (
    EXECUTION_FILL_EVENT_TYPE,
    ORDER_STATE_EVENT_TYPE,
    RISK_DECISION_EVENT_TYPE,
    STRATEGY_DECISION_EVENT_TYPE,
    TRADE_INTENT_EVENT_TYPE,
    VALIDITY_EVENT_TYPE,
)
from crypto_trader.paper import (
    PaperStateError,
    RunReport,
    ScriptedSimulationRunner,
    SimulationOutcome,
    TradingMode,
    report_from_journal,
)
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.safety import SafetyMode
from crypto_trader.tests import paper_fixtures as fx


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as instance:
        yield instance


def event_types(journal) -> list[str]:
    return [event.event_type for event in journal.read_range()]


# --- shadow ---------------------------------------------------------------

def test_shadow_mode_records_what_would_have_been_submitted(journal):
    session = fx.session(
        journal,
        mode=TradingMode.SHADOW,
        decisions=fx.scripted(fx.buy_decision(quantity="1")),
        data_feed=fx.feed("2700.00"),
    )

    report = session.run()

    assert report.mode == "shadow"
    assert [record.submission for record in report.decisions] == ["would_submit"]
    assert report.orders == ()
    assert report.fills == ()
    assert report.positions == {}
    assert report.validity_counts == {"accepted": 1}
    assert report.risk_counts == {"allowed": 1}
    # The decision path is journaled; NO order or fill event exists in shadow mode.
    assert sorted(event_types(journal)) == sorted(
        [
        STRATEGY_DECISION_EVENT_TYPE,
        TRADE_INTENT_EVENT_TYPE,
        VALIDITY_EVENT_TYPE,
        RISK_DECISION_EVENT_TYPE,
        "risk.evaluation.recorded",
        ]
    )
    assert ORDER_STATE_EVENT_TYPE not in event_types(journal)
    assert EXECUTION_FILL_EVENT_TYPE not in event_types(journal)


def test_shadow_mode_never_touches_the_simulator(journal):
    class ExplodingSimulator:
        def simulate(self, requests):  # pragma: no cover - must never be called
            raise AssertionError("shadow mode must not run the simulator")

    session = fx.session(
        journal,
        mode=TradingMode.SHADOW,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=fx.feed("2700.00"),
        simulator=ExplodingSimulator(),
    )

    assert session.run().orders == ()


# --- paper ----------------------------------------------------------------

def test_paper_mode_places_simulated_orders_and_journals_them(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1")),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
    )

    report = session.run()

    assert report.mode == "paper"
    assert [record.submission for record in report.decisions] == ["submitted_to_simulator"]
    assert len(report.orders) == 1
    assert len(report.fills) == 1
    order = report.orders[0]
    fill = report.fills[0]
    assert order.state == "filled"
    assert order.filled_quantity == Decimal("1")
    assert order.remaining_quantity == Decimal("0")
    assert fill.price == Decimal("2700.00")
    assert fill.quantity == Decimal("1")
    assert report.positions == {"ETHUSDT": Decimal("1")}
    assert report.realized_pnl_quote == Decimal("0")
    assert report.unrealized_pnl_quote == Decimal("0")
    assert report.equity_quote == Decimal("100000")
    assert ORDER_STATE_EVENT_TYPE in event_types(journal)
    assert EXECUTION_FILL_EVENT_TYPE in event_types(journal)


def test_paper_mode_preserves_intent_and_correlation_identity(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
    )

    report = session.run()

    record = report.decisions[0]
    assert isinstance(record.intent_id, UUID)
    assert report.orders[0].intent_id == record.intent_id
    assert report.fills[0].intent_id == record.intent_id
    assert report.orders[0].correlation_id == fx.SESSION_ID
    assert report.fills[0].correlation_id == fx.SESSION_ID
    intent_events = [
        event
        for event in journal.read_by_correlation(fx.SESSION_ID)
        if event.event_type == TRADE_INTENT_EVENT_TYPE
    ]
    assert len(intent_events) == 1
    assert report.session_id == fx.SESSION_ID


def test_paper_mode_journals_every_stage_under_one_correlation(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
    )
    session.run()

    events = journal.read_by_correlation(fx.SESSION_ID)

    assert events, "the session must journal under its correlation id"
    assert all(event.correlation_id == fx.SESSION_ID for event in events)
    assert {event.event_type for event in events} >= {
        STRATEGY_DECISION_EVENT_TYPE,
        TRADE_INTENT_EVENT_TYPE,
        VALIDITY_EVENT_TYPE,
        RISK_DECISION_EVENT_TYPE,
        ORDER_STATE_EVENT_TYPE,
        EXECUTION_FILL_EVENT_TYPE,
    }


def test_partial_simulated_fill_is_reported_with_exact_decimals(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1.5")),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(fill_fraction=Decimal("0.4")),
    )

    report = session.run()

    assert report.orders[0].state == "partially_filled"
    assert report.orders[0].filled_quantity == Decimal("0.6")
    assert report.orders[0].remaining_quantity == Decimal("0.9")
    assert report.positions == {"ETHUSDT": Decimal("0.6")}


def test_hold_decision_creates_no_intent_or_execution(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.hold_decision()),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
    )

    report = session.run()

    assert [record.submission for record in report.decisions] == ["hold"]
    assert report.orders == ()
    assert event_types(journal) == [STRATEGY_DECISION_EVENT_TYPE]


# --- stopping conditions --------------------------------------------------

def test_risk_rejection_stops_before_the_simulator(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="10", limit_price="2700.00")),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
        limits_override=fx.limits(max_order_notional_quote="5000"),
    )

    report = session.run()

    assert [record.submission for record in report.decisions] == ["stopped_by_risk"]
    assert "max_order_notional_exceeded" in report.decisions[0].risk_reason_codes
    assert report.orders == ()
    assert report.risk_counts == {"rejected": 1}
    assert ORDER_STATE_EVENT_TYPE not in event_types(journal)


def test_safety_halt_blocks_new_risk_inside_the_session(journal):
    safety = fx.SafetyController(correlation_id=fx.SESSION_ID)
    safety.set_mode(SafetyMode.HALT_NEW_RISK, reason_code="operator_halt", reason="halted")
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
        safety=safety,
    )

    report = session.run()

    assert [record.submission for record in report.decisions] == ["blocked_by_safety"]
    assert "trading_halted" in report.decisions[0].risk_reason_codes
    assert report.safety_blocked == 1
    assert report.safety_events == ("halt_new_risk",)
    assert report.orders == ()


def test_stale_decision_is_stopped_by_validity_and_recorded(journal):
    stale_time = fx.BASE + timedelta(seconds=120)
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(decided_at=stale_time)),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
    )

    report = session.run()

    assert [record.submission for record in report.decisions] == ["stopped_by_validity"]
    assert report.decisions[0].validity_disposition == "stale"
    assert report.validity_counts == {"stale": 1}
    assert report.stale_decisions == ("public.kline.1m#0",)
    assert report.orders == ()


def test_market_moved_decision_is_stopped_by_validity(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(
            fx.buy_decision(limit_price="2700.00"), fx.buy_decision(limit_price="2700.00")
        ),
        data_feed=fx.feed("2700.00", "2900.00"),
        simulator=ScriptedSimulationRunner(),
    )

    report = session.run()

    assert report.validity_counts == {"accepted": 1, "market_moved": 1}
    assert len(report.decisions) == 2
    assert [record.submission for record in report.decisions] == [
        "submitted_to_simulator",
        "stopped_by_validity",
    ]
    # Only the accepted intent reached the simulator.
    assert len(report.orders) == 1
    assert report.fills[0].intent_id == report.decisions[0].intent_id


# --- report ---------------------------------------------------------------

def test_report_is_json_serializable_and_secret_free(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
    )

    report = session.run()
    payload = json.loads(report.to_json())
    lowered = report.to_json().lower()

    assert payload["mode"] == "paper"
    assert payload["observations_processed"] == 1
    for token in ("api_key", "apikey", "secret", "hmac", "x-mbx", "withdraw"):
        assert token not in lowered
    assert any("not a performance claim" in note for note in payload["notes"])


def test_report_records_the_retained_limitations(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
    )

    notes = " ".join(session.run().notes)

    assert "hidden/iceberg" in notes
    assert "PARTIAL" in notes
    assert "SIMULATED ACCOUNTING" in notes


def test_report_write_creates_a_reviewable_artifact(tmp_path: Path, journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
    )
    report = session.run()

    target = report.write(tmp_path / "run-report.json")

    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["session_id"] == str(fx.SESSION_ID)


def test_report_semantic_hash_ignores_wall_clock_fields(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
    )
    report = session.run()

    assert report.semantic_hash() == report.semantic_hash()
    import dataclasses

    changed = dataclasses.replace(report, started_at=fx.BASE + timedelta(days=3))
    assert changed.semantic_hash() == report.semantic_hash()


def test_journal_rebuild_matches_the_live_report(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1"), fx.sell_decision(quantity="1")),
        data_feed=fx.feed("2700.00", "2701.00"),
        simulator=ScriptedSimulationRunner(),
    )
    report = session.run()

    rebuilt = report_from_journal(
        journal.read_range(),
        session_id=fx.SESSION_ID,
        mode="paper",
        symbol="ETHUSDT",
        strategy_id="strategy-paper-1",
    )

    assert rebuilt.observations_processed == report.observations_processed
    assert rebuilt.validity_counts == report.validity_counts
    assert rebuilt.risk_counts == report.risk_counts
    assert [order.state for order in rebuilt.orders] == [order.state for order in report.orders]
    assert [fill.quantity for fill in rebuilt.fills] == [fill.quantity for fill in report.fills]


def test_paper_mode_requires_a_simulator(journal):
    with pytest.raises(PaperStateError):
        fx.session(
            journal,
            decisions=fx.scripted(fx.buy_decision()),
            data_feed=fx.feed("2700.00"),
            simulator=None,
        )


def test_run_rejects_an_out_of_range_start_index(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
    )

    with pytest.raises(PaperStateError):
        session.run(start_index=5)


def test_report_is_a_run_report_instance(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.hold_decision()),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
    )

    assert isinstance(session.run(), RunReport)


def test_paper_risk_equity_includes_unrealized_and_retains_peak(journal):
    """Risk and ledger must value the same position at the same historical mark."""
    from crypto_trader.paper.accounting import PositionLedger

    session = fx.session(
        journal,
        decisions=fx.scripted(fx.hold_decision()),
        data_feed=fx.feed("2800.00"),
        simulator=ScriptedSimulationRunner(),
    )
    ledger = PositionLedger(Decimal("100000"))
    ledger.apply_fill(
        symbol="ETHUSDT", side="buy",
        quantity=Decimal("1"), price=Decimal("2700"), fee=Decimal("0"),
    )
    high_mark = fx.feed("2800.00").observations()[0]
    low_mark = fx.feed("2650.00").observations()[0]
    high = session._risk_state(ledger, high_mark, high_mark.observed_at)
    low = session._risk_state(ledger, low_mark, low_mark.observed_at)
    assert high.equity_quote == Decimal("100100")
    assert high.peak_equity_quote == Decimal("100100")
    assert low.equity_quote == Decimal("99950")
    assert low.peak_equity_quote == Decimal("100100")


def test_paper_partial_fill_reduces_only_outstanding_reservation(journal):
    from crypto_trader.execution.ports import ExecutionObservation, OrderLifecycleState
    from crypto_trader.risk import PendingIntent, Side
    from uuid import UUID

    session = fx.session(
        journal,
        decisions=fx.scripted(fx.hold_decision()),
        data_feed=fx.feed("2700.00"),
        simulator=ScriptedSimulationRunner(),
    )
    intent = UUID(int=12345)
    session._reservations[intent] = (Side.BUY, Decimal("1"), Decimal("2700"))
    session._pending.register(PendingIntent(
        intent_id=intent, symbol="ETHUSDT", strategy_id=session.config.strategy_id,
        side=Side.BUY, quantity=Decimal("1"), notional=Decimal("2700"),
    ))
    partial = ExecutionObservation(
        order_reference="SIM-PARTIAL", intent_id=intent,
        correlation_id=session.config.session_id, symbol="ETHUSDT",
        state=OrderLifecycleState.PARTIALLY_FILLED,
        filled_quantity=Decimal("0.3"), remaining_quantity=Decimal("0.7"),
        last_fill_quantity=Decimal("0.3"), last_fill_price=Decimal("2700"),
    )
    session._reconcile_temporal_reservations(
        SimulationOutcome(observations=(partial,))
    )
    assert session._reservations[intent][1] == Decimal("0.7")
    assert session._pending.items()[0].quantity == Decimal("0.7")
    assert session._pending.items()[0].notional == Decimal("1890")


# --- Phase10 temporal integrity regression: journal sequence and partial quantities ---

def _temporal_partial_validation_candidate(journal, *, remaining="0.7", filled="0.3"):
    from crypto_trader.execution import ExecutionRequest, ExecutionOrderType, ExecutionSide
    from crypto_trader.execution.ports import ExecutionObservation, OrderLifecycleState
    from crypto_trader.execution.nautilus import outbound
    from crypto_trader.risk import Side

    session = fx.session(
        journal, decisions=fx.scripted(fx.hold_decision()),
        data_feed=fx.feed("2700.00"), simulator=ScriptedSimulationRunner(),
    )
    request = ExecutionRequest.create(
        intent_id=UUID(int=19271), correlation_id=fx.SESSION_ID,
        symbol="ETHUSDT", side=ExecutionSide.BUY, order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("1"), limit_price=Decimal("2700"),
    )
    t0 = int(fx.BASE.timestamp() * 1_000_000_000)
    session._reservations[request.intent_id] = (Side.BUY, Decimal("1"), Decimal("2700"))
    session._temporal_decided_ns[request.intent_id] = t0
    observation = ExecutionObservation(
        order_reference="SIM-PARTIAL-INTEGRITY", intent_id=request.intent_id,
        correlation_id=fx.SESSION_ID, symbol="ETHUSDT",
        state=OrderLifecycleState.PARTIALLY_FILLED,
        filled_quantity=Decimal(filled), remaining_quantity=Decimal(remaining),
        last_fill_quantity=Decimal("0.3"), last_fill_price=Decimal("2700"),
        fee=Decimal("0.01"), ts_ns=t0 + 10_000_000,
    )
    state_event = outbound.to_phase3_order_state_event(
        observation, event_id=UUID(int=19272)
    )
    fill_event = outbound.to_phase3_fill_event(
        observation, event_id=UUID(int=19273)
    )
    return session, request, observation, state_event, fill_event, t0 + 10_000_000


def test_temporal_journal_must_preserve_native_lifecycle_then_fill_order(journal):
    """A matching multiset is not enough: durable order must mirror native callbacks."""
    session, req, obs, state_event, fill_event, horizon = (
        _temporal_partial_validation_candidate(journal)
    )
    with pytest.raises(PaperStateError, match="order|sequence|journal"):
        session._validate_temporal_delta(
            SimulationOutcome(
                observations=(obs,), phase3_events=(fill_event, state_event),
            ),
            [req], boundary_ns=horizon,
        )
    assert not event_types(journal)
    assert session._reservations[req.intent_id][1] == Decimal("1")


@pytest.mark.parametrize(
    ("remaining", "filled"),
    [("0.9", "0.3"), ("0.7", "0.4")],
    ids=["wrong-remaining", "wrong-cumulative"],
)
def test_temporal_partial_quantities_must_match_reservation_before_journal(
    journal, remaining, filled,
):
    """Native leaves and cumulative fills must equal the tracked order quantities."""
    session, req, obs, state_event, fill_event, horizon = (
        _temporal_partial_validation_candidate(
            journal, remaining=remaining, filled=filled,
        )
    )
    with pytest.raises(PaperStateError, match="quantity|remaining|cumulative"):
        session._validate_temporal_delta(
            SimulationOutcome(
                observations=(obs,), phase3_events=(state_event, fill_event),
            ),
            [req], boundary_ns=horizon,
        )
    assert not event_types(journal)
    assert session._reservations[req.intent_id][1] == Decimal("1")


def test_temporal_valid_partial_quantities_and_order_are_accepted(journal):
    session, req, obs, state_event, fill_event, horizon = (
        _temporal_partial_validation_candidate(journal)
    )
    closed, ids = session._validate_temporal_delta(
        SimulationOutcome(
            observations=(obs,), phase3_events=(state_event, fill_event),
        ),
        [req], boundary_ns=horizon,
    )
    assert not closed
    assert ids == {state_event.event_id, fill_event.event_id}
    assert not event_types(journal)
