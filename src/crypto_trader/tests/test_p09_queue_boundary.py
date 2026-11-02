"""P-09: Paper's native bridge cannot downgrade queue-aware execution."""
import pytest

from crypto_trader.execution import InstrumentSpec, QueueAwareConfigurationError
from crypto_trader.execution.nautilus import NautilusSimulationRunner, QueueAwareSettings
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx


@pytest.mark.parametrize(
    "settings",
    [
        {"queue_aware": False},
        {"queue_settings": QueueAwareSettings(True, True, "L1_MBP")},
    ],
)
def test_p09_native_paper_rejects_queue_downgrade_before_side_effects(tmp_path, settings):
    simulator = NautilusSimulationRunner(instrument=InstrumentSpec(symbol="ETHUSDT"), **settings)
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        session = fx.session(
            journal,
            decisions=fx.scripted(fx.hold_decision()),
            data_feed=fx.feed("2700.00"),
            simulator=simulator,
        )
        with pytest.raises(QueueAwareConfigurationError):
            session.run()
        assert tuple(journal.read_range()) == ()
        assert getattr(simulator, "_temporal_runtime", None) is None
