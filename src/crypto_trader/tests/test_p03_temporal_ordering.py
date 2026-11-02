"""P-03 B1: deterministic temporal decisions require strictly increasing market time.

Legacy frozen-input snapshots may contain distinct events at tied timestamps.
Only the time-stepped Nautilus Paper path must reject them before any journal
write or native engine start; this does not rewrite the historical feed contract.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from crypto_trader.execution import InstrumentSpec
from crypto_trader.execution.nautilus import NautilusSimulationRunner
from crypto_trader.paper import FrozenMarketDataFeed, PaperStateError, TradingMode
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx


@pytest.fixture
def tied_feed():
    entries = tuple(fx.feed("2700.00", "2701.00").entries)
    return FrozenMarketDataFeed(
        entries=(entries[0], replace(entries[1], observed_at=entries[0].observed_at)),
        feed_id="tied-events-distinct-refs",
    )


def test_p03_native_temporal_rejects_tied_market_time_before_journal(tmp_path: Path, tied_feed):
    simulator = NautilusSimulationRunner(instrument=InstrumentSpec(symbol="ETHUSDT"))
    with SQLiteEventJournal(tmp_path / "tied.db") as journal:
        session = fx.session(
            journal, data_feed=tied_feed,
            decisions=fx.scripted(fx.buy_decision(), fx.hold_decision()),
            simulator=simulator,
        )
        with pytest.raises(PaperStateError, match="strictly increasing"):
            session.run()
        assert journal.count() == 0
        assert getattr(simulator, "_temporal_runtime", None) is None


def test_p03_legacy_shadow_can_read_tied_distinct_market_references(tmp_path: Path, tied_feed):
    with SQLiteEventJournal(tmp_path / "shadow.db") as journal:
        session = fx.session(
            journal, mode=TradingMode.SHADOW, data_feed=tied_feed,
            decisions=fx.scripted(fx.hold_decision(), fx.hold_decision()),
        )
        report = session.run()
        assert report.observations_processed == 2
        assert len(tuple(journal.read_by_correlation(fx.SESSION_ID))) == 2
