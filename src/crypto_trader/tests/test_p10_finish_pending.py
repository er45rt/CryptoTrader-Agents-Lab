"""P-10: a final queued intent cannot disappear behind a successful report."""
from datetime import timedelta

import pytest

from crypto_trader.execution import InstrumentSpec
from crypto_trader.execution.nautilus import NautilusSimulationRunner
from crypto_trader.paper import PaperStateError
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx
from crypto_trader.tests.test_paper_nautilus import aligned_runner


@pytest.mark.parametrize("ending", ["last_tick", "after_last_tick", "empty_tape"])
def test_p10_final_unsubmitted_intent_fails_closed(tmp_path, ending):
    simulator = (
        NautilusSimulationRunner(instrument=InstrumentSpec(symbol="ETHUSDT"))
        if ending == "empty_tape" else aligned_runner()
    )
    start = fx.BASE + timedelta(milliseconds=30 if ending == "last_tick" else 1000)
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        session = fx.session(
            journal,
            decisions=fx.scripted(fx.buy_decision(quantity="1")),
            data_feed=fx.feed("2700.00", start=start),
            simulator=simulator,
        )
        with pytest.raises(PaperStateError, match="unsubmitted queued intents"):
            session.run()
        events = tuple(journal.read_range())
        assert sum(event.event_type == "trade.intent.created" for event in events) == 1
        assert not any(event.event_type in {"order.state.changed", "execution.fill.recorded"}
                       for event in events)
        assert getattr(simulator, "_temporal_runtime", None) is None


def test_p10_fully_submitted_and_filled_tape_still_completes(tmp_path):
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        session = fx.session(
            journal,
            decisions=fx.scripted(fx.buy_decision(quantity="1")),
            data_feed=fx.feed("2700.00"),
            simulator=aligned_runner(),
        )
        report = session.run()
        assert len(report.orders) == len(report.fills) == 1
        assert session._reservations == {}
