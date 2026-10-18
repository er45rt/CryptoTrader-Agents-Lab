"""P-06: persisted remaining quantities survive partial-fill interruption and replay.

Synthetic offline execution only. Resuming the native Nautilus engine without a
verified engine snapshot is deliberately NOT allowed; this tests durable JOURNAL
replay, rather than inventing the missing engine state.
"""
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_trader.events import (
    EXECUTION_FILL_EVENT_TYPE, ORDER_STATE_EVENT_TYPE, TRADE_INTENT_EVENT_TYPE,
)
from crypto_trader.paper import (
    PaperStateError, ScriptedSimulationRunner, SimulationOutcome, report_from_journal,
)
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx


def replay(journal):
    return report_from_journal(
        journal.read_range(), session_id=fx.SESSION_ID,
        mode="paper", symbol="ETHUSDT", strategy_id="strategy-paper-1",
    )


class AcceptedWithoutFill:
    """Produce a durable accepted order while deliberately withholding all fills."""

    def simulate(self, requests):
        simulated = ScriptedSimulationRunner(fill_fraction=Decimal("0.4")).simulate(requests)
        return SimulationOutcome(
            observations=simulated.observations[:2],
            phase3_events=simulated.phase3_events[:2],
        )


def test_p06_accepted_unfilled_order_replays_original_leaves_after_restart(tmp_path: Path):
    path = tmp_path / "accepted.db"
    with SQLiteEventJournal(path) as journal:
        fx.session(
            journal, decisions=fx.scripted(fx.buy_decision(quantity="1.5")),
            data_feed=fx.feed("2700.00"), simulator=AcceptedWithoutFill(),
        ).run()
        initial = tuple(event.event_id for event in journal.read_range())

    with SQLiteEventJournal(path) as reopened:
        reconstructed = replay(reopened)
        assert len(reconstructed.orders) == 1
        assert reconstructed.orders[0].state == "accepted"
        assert reconstructed.orders[0].filled_quantity == Decimal("0")
        assert reconstructed.orders[0].remaining_quantity == Decimal("1.5")
        assert reconstructed.fills == ()
        assert tuple(event.event_id for event in reopened.read_range()) == initial


def test_p06_crash_after_partial_fill_then_restart_replays_exact_leaves(
    tmp_path: Path, monkeypatch,
):
    path = tmp_path / "partial-crash.db"
    with SQLiteEventJournal(path) as journal:
        original_append = journal.append

        def interrupt_after_durable_fill(event):
            original_append(event)
            if event.event_type == EXECUTION_FILL_EVENT_TYPE:
                raise OSError("injected crash after persisted partial fill")

        monkeypatch.setattr(journal, "append", interrupt_after_durable_fill)
        session = fx.session(
            journal, decisions=fx.scripted(fx.buy_decision(quantity="1.5")),
            data_feed=fx.feed("2700.00"),
            simulator=ScriptedSimulationRunner(fill_fraction=Decimal("0.4")),
        )
        with pytest.raises(OSError, match="injected crash"):
            session.run()
        before = tuple(event.event_id for event in journal.read_range())
        assert any(event.event_type == EXECUTION_FILL_EVENT_TYPE for event in journal.read_range())

    with SQLiteEventJournal(path) as reopened:
        report = replay(reopened)
        assert report.observations_processed == 1
        assert report.orders[0].state == "partially_filled"
        assert report.orders[0].filled_quantity == Decimal("0.6")
        assert report.orders[0].remaining_quantity == Decimal("0.9")
        assert len(report.fills) == 1
        assert report.fills[0].remaining_quantity == Decimal("0.9")
        assert tuple(event.event_id for event in reopened.read_range()) == before
        # Reprocessing the same scripted window is idempotent by durable event ID.
        resumed = fx.session(
            reopened, decisions=fx.scripted(fx.buy_decision(quantity="1.5")),
            data_feed=fx.feed("2700.00"),
            simulator=ScriptedSimulationRunner(fill_fraction=Decimal("0.4")),
        )
        assert resumed.resume_index() == 1
        resumed.run(start_index=0)
        after = tuple(event.event_id for event in reopened.read_range())
        assert after == before
        assert replay(reopened).orders[0].remaining_quantity == Decimal("0.9")


def test_p06_unknown_legacy_unfilled_quantity_fails_closed(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "legacy.db") as journal:
        fx.session(
            journal, decisions=fx.scripted(fx.buy_decision(quantity="1.5")),
            data_feed=fx.feed("2700.00"), simulator=AcceptedWithoutFill(),
        ).run()
        events = list(journal.read_range())
        legacy = [
            event.model_copy(update={"causation_id": None})
            if event.event_type == ORDER_STATE_EVENT_TYPE else event
            for event in events
        ]
        with pytest.raises(PaperStateError, match="without intent provenance"):
            report_from_journal(
                legacy, session_id=fx.SESSION_ID, mode="paper",
                symbol="ETHUSDT", strategy_id="strategy-paper-1",
            )


def test_p06_order_causation_links_to_durable_intent_event(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "identity.db") as journal:
        fx.session(
            journal, decisions=fx.scripted(fx.buy_decision(quantity="1.5")),
            data_feed=fx.feed("2700.00"), simulator=AcceptedWithoutFill(),
        ).run()
        events = tuple(journal.read_range())
        origins = {event.event_id for event in events
                   if event.event_type == TRADE_INTENT_EVENT_TYPE}
        states = [event for event in events
                  if event.event_type == ORDER_STATE_EVENT_TYPE]
        assert len(origins) == 1 and states
        assert all(event.causation_id in origins for event in states)
