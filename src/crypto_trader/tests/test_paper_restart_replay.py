"""Phase 10: restart-during-session and deterministic paper-session replay."""

from decimal import Decimal
from pathlib import Path

import pytest

from crypto_trader.events import STRATEGY_DECISION_EVENT_TYPE
from crypto_trader.paper import ScriptedSimulationRunner, TradingMode, report_from_journal
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as instance:
        yield instance


def decisions_for_all(entries: int):
    return fx.scripted(*[fx.buy_decision(quantity="1") for _ in range(entries)])


def test_resume_index_is_derived_from_the_recorded_journal(journal):
    partial_feed = fx.feed("2700.00", "2701.00")
    session = fx.session(
        journal,
        decisions=decisions_for_all(2),
        data_feed=partial_feed,
        simulator=ScriptedSimulationRunner(),
    )
    session.run()

    resumed = fx.session(
        journal,
        decisions=decisions_for_all(4),
        data_feed=fx.feed("2700.00", "2701.00", "2702.00", "2703.00"),
        simulator=ScriptedSimulationRunner(),
    )

    assert resumed.resume_index() == 2


def test_restart_mid_session_completes_without_duplicating_effects(journal):
    full_feed = fx.feed("2700.00", "2701.00", "2702.00", "2703.00")

    first = fx.session(
        journal,
        decisions=decisions_for_all(4),
        data_feed=fx.feed("2700.00", "2701.00"),
        simulator=ScriptedSimulationRunner(),
    )
    first_report = first.run()

    second = fx.session(
        journal,
        decisions=decisions_for_all(4),
        data_feed=full_feed,
        simulator=ScriptedSimulationRunner(),
    )
    second_report = second.run(start_index=second.resume_index())

    assert first_report.observations_processed == 2
    assert second_report.observations_processed == 2
    combined = [
        record.market_reference
        for record in (*first_report.decisions, *second_report.decisions)
    ]
    assert combined == [f"public.kline.1m#{index}" for index in range(4)]
    decision_events = [
        event
        for event in journal.read_range()
        if event.event_type == STRATEGY_DECISION_EVENT_TYPE
    ]
    assert len(decision_events) == 4
    assert len({event.event_id for event in decision_events}) == 4


def test_restart_reprocessing_the_same_window_does_not_duplicate_events(journal):
    feed = fx.feed("2700.00")
    first = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=feed,
        simulator=ScriptedSimulationRunner(),
    )
    first.run()
    before = journal.count()

    # A process restart that has not yet learned its cursor re-processes the same observation:
    # every event id is derived from the session id + market reference, so the journal is stable.
    again = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=feed,
        simulator=ScriptedSimulationRunner(),
    )
    again.run()

    assert journal.count() == before
    assert again.resume_index() == 1


def test_same_inputs_produce_the_same_semantic_report_hash(tmp_path: Path):
    def run_once(name: str):
        with SQLiteEventJournal(tmp_path / f"{name}.db") as journal:
            session = fx.session(
                journal,
                decisions=fx.scripted(
                    fx.buy_decision(quantity="1"),
                    fx.hold_decision(),
                    fx.sell_decision(quantity="1"),
                ),
                data_feed=fx.feed("2700.00", "2701.00", "2702.00"),
                simulator=ScriptedSimulationRunner(),
            )
            return session.run()

    first = run_once("first")
    second = run_once("second")

    assert first.semantic_hash() == second.semantic_hash()
    assert first.to_json() == second.to_json()


def test_replaying_a_recorded_session_matches_the_live_report(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(
            fx.buy_decision(quantity="1"), fx.buy_decision(quantity="0.5")
        ),
        data_feed=fx.feed("2700.00", "2701.00"),
        simulator=ScriptedSimulationRunner(),
    )
    live = session.run()

    replay = report_from_journal(
        journal.read_range(),
        session_id=fx.SESSION_ID,
        mode="paper",
        symbol="ETHUSDT",
        strategy_id="strategy-paper-1",
    )

    assert replay.observations_processed == live.observations_processed
    assert replay.validity_counts == live.validity_counts
    assert replay.risk_counts == live.risk_counts
    assert [order.state for order in replay.orders] == [order.state for order in live.orders]
    assert [fill.quantity for fill in replay.fills] == [fill.quantity for fill in live.fills]
    assert [fill.price for fill in replay.fills] == [fill.price for fill in live.fills]


def test_shadow_session_restart_is_also_idempotent(journal):
    session = fx.session(
        journal,
        mode=TradingMode.SHADOW,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=fx.feed("2700.00"),
    )
    session.run()
    before = journal.count()

    fx.session(
        journal,
        mode=TradingMode.SHADOW,
        decisions=fx.scripted(fx.buy_decision()),
        data_feed=fx.feed("2700.00"),
    ).run()

    assert journal.count() == before
