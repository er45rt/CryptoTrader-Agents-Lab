"""Phase 10: paper mode against the real offline queue-aware Nautilus simulator.

Offline only: a simulated in-process venue, frozen public-shaped market data, no network, no
credential, no live venue. The paper session itself stays engine-independent; the engine is wired
in as infrastructure through ``NautilusSimulationRunner``.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from crypto_trader.execution import (
    BookLevel,
    InstrumentSpec,
    MarketBookUpdate,
    MarketTrade,
    QueueAwareConfigurationError,
)
from crypto_trader.execution.nautilus import NautilusSimulationRunner, QueueAwareSettings
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx

INSTRUMENT = InstrumentSpec(symbol="ETHUSDT")
PRICE = Decimal("2700.00")
TS_BASE_NS = 1_767_225_600_000_000_000
MS = 1_000_000


def l2_fixture() -> tuple[tuple[MarketBookUpdate, ...], tuple[MarketTrade, ...]]:
    """A frozen L2 market-by-price fixture with explicit aggressor-side trades."""
    book = MarketBookUpdate(
        levels=[
            BookLevel(side="BUY", price=PRICE, size=Decimal("100.00000000"), order_id=1),
            BookLevel(
                side="SELL", price=Decimal("2701.00"), size=Decimal("100.00000000"), order_id=2
            ),
        ],
        ts_ns=TS_BASE_NS,
        sequence=1,
    )
    trades = (
        # Clears the displayed queue ahead, then fills our order.
        MarketTrade(
            price=PRICE,
            size=Decimal("100.00000000"),
            aggressor="SELLER",
            ts_ns=TS_BASE_NS + 10 * MS,
            trade_id="PF-1",
        ),
        MarketTrade(
            price=PRICE,
            size=Decimal("1.00000000"),
            aggressor="SELLER",
            ts_ns=TS_BASE_NS + 20 * MS,
            trade_id="PF-2",
        ),
    )
    return (book,), trades


def runner(**overrides) -> NautilusSimulationRunner:
    book_updates, trades = l2_fixture()
    return NautilusSimulationRunner(
        instrument=INSTRUMENT,
        book_updates=book_updates,
        trades=trades,
        **overrides,
    )


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as instance:
        yield instance


def test_paper_session_fills_through_the_queue_aware_simulator(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1")),
        data_feed=fx.feed("2700.00"),
        simulator=runner(),
    )

    report = session.run()

    assert report.mode == "paper"
    assert report.queue_aware is True
    assert report.book_type == "L2_MBP"
    assert report.engine_version == "1.231.0"
    assert len(report.orders) == 1
    assert len(report.fills) == 1
    assert report.orders[0].state == "filled"
    assert report.fills[0].price == PRICE
    assert report.fills[0].quantity == Decimal("1.00000000")
    assert report.positions == {"ETHUSDT": Decimal("1.00000000")}


def test_paper_session_report_contains_no_engine_types(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1")),
        data_feed=fx.feed("2700.00"),
        simulator=runner(),
    )

    report = session.run()
    payload = report.to_json()

    for token in ("nautilus", "OrderBookDeltas", "InstrumentId", "BacktestEngine"):
        assert token.lower() not in payload.lower()


def test_paper_session_journals_engine_produced_phase3_contracts(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1")),
        data_feed=fx.feed("2700.00"),
        simulator=runner(),
    )

    report = session.run()
    types = {event.event_type for event in journal.read_range()}

    assert {"order.state.changed", "execution.fill.recorded"} <= types
    order_events = [
        event for event in journal.read_range() if event.event_type == "order.state.changed"
    ]
    assert all(event.order_id is not None for event in order_events)
    assert all(event.correlation_id == fx.SESSION_ID for event in order_events)
    assert report.fills[0].correlation_id == fx.SESSION_ID


def test_queue_aware_configuration_is_enforced_by_the_adapter():
    with pytest.raises(QueueAwareConfigurationError):
        runner(queue_settings=QueueAwareSettings(True, True, "L1_MBP")).simulate(())


def test_runner_requires_a_non_queue_aware_opt_in_for_other_book_types():
    # Explicitly disabling queue-awareness is allowed, but it must be explicit.
    outcome = runner(
        queue_aware=False, queue_settings=QueueAwareSettings(False, False, "L1_MBP")
    ).simulate(())

    assert outcome.queue_aware is False
    assert outcome.book_type is None
    assert outcome.observations == ()
