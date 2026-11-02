"""P-08: reject wrong-symbol tape entries before Paper side effects."""
from dataclasses import replace

import pytest

from crypto_trader.paper import FrozenMarketDataFeed, PaperStateError
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx
from crypto_trader.tests.test_paper_nautilus import aligned_runner


@pytest.mark.parametrize("wrong_index", [0, 1])
def test_p08_wrong_symbol_rejected_before_engine_or_journal(tmp_path, wrong_index):
    entries = list(fx.feed("2700.00", "2705.00").observations())
    entries[wrong_index] = replace(entries[wrong_index], symbol="BTCUSDT")
    simulator = aligned_runner()
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        session = fx.session(
            journal,
            decisions=fx.scripted(fx.buy_decision(quantity="1"), fx.hold_decision()),
            data_feed=FrozenMarketDataFeed(tuple(entries)),
            simulator=simulator,
        )
        with pytest.raises(PaperStateError, match="market observation symbol"):
            session.run()
        assert tuple(journal.read_range()) == ()
        assert getattr(simulator, "_temporal_runtime", None) is None
