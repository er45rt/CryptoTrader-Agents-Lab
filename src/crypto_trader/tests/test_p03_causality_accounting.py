"""P-03: causal frozen/public observations and exact simulated accounting.

Purely offline. Reject invalid marks/non-finite fills and duplicate input before
mutating any replay cursor, event journal, or simulated position.
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from crypto_trader.paper.accounting import PositionLedger
from crypto_trader.paper.errors import PaperConfigError, PaperStateError
from crypto_trader.paper.feed import FrozenMarketDataFeed, MarketObservation, PublicMarketDataFeed
from crypto_trader.tests.paper_fixtures import BASE


def _entry(index: int, *, reference: str | None = None, seconds: int | None = None):
    return MarketObservation(
        symbol="ETHUSDT", price=Decimal("2700"), observed_at=BASE + timedelta(
            seconds=index if seconds is None else seconds
        ), market_reference=reference or f"frozen#{index}",
    )


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "-1", "0"])
def test_p03_nonfinite_and_nonpositive_fill_quantity_rejected_without_mutation(bad):
    ledger = PositionLedger(Decimal("10000"))
    with pytest.raises(PaperStateError):
        ledger.apply_fill(symbol="ETHUSDT", side="buy", quantity=Decimal(bad),
                          price=Decimal("100"))
    assert ledger.positions() == {}


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "-1", "0"])
def test_p03_nonfinite_and_nonpositive_price_rejected_without_mutation(bad):
    ledger = PositionLedger(Decimal("10000"))
    with pytest.raises(PaperStateError):
        ledger.apply_fill(symbol="ETHUSDT", side="buy", quantity=Decimal("1"),
                          price=Decimal(bad))
    assert ledger.positions() == {}


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "-1"])
def test_p03_nonfinite_and_negative_fee_rejected_without_mutation(bad):
    ledger = PositionLedger(Decimal("10000"))
    with pytest.raises(PaperStateError):
        ledger.apply_fill(symbol="ETHUSDT", side="buy", quantity=Decimal("1"),
                          price=Decimal("100"), fee=Decimal(bad))
    assert ledger.positions() == {}


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "0", "-1"])
def test_p03_starting_equity_requires_finite_positive_decimal(bad):
    with pytest.raises(PaperStateError):
        PositionLedger(Decimal(bad))


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "0", "-1", None])
def test_p03_open_position_missing_or_bad_mark_fails_closed(bad):
    ledger = PositionLedger(Decimal("10000"))
    ledger.apply_fill(symbol="ETHUSDT", side="buy", quantity=Decimal("1"),
                      price=Decimal("100"))
    marks = {} if bad is None else {"ETHUSDT": Decimal(bad)}
    for calc in (ledger.gross_exposure_quote, ledger.unrealized_pnl_quote, ledger.equity_quote):
        with pytest.raises(PaperStateError):
            calc(marks)


def test_p03_exact_fees_roundtrip_partial_close_and_flip():
    ledger = PositionLedger(Decimal("10000"))
    ledger.apply_fill(symbol="ETHUSDT", side="buy", quantity=Decimal("2"),
                      price=Decimal("100"), fee=Decimal("0.20"))
    ledger.apply_fill(symbol="ETHUSDT", side="sell", quantity=Decimal("1"),
                      price=Decimal("110"), fee=Decimal("0.10"))
    position = ledger.position("ETHUSDT")
    assert position.quantity == Decimal("1")
    assert position.average_cost == Decimal("100")
    assert position.realized_pnl_quote == Decimal("9.70")
    assert ledger.fees_quote() == Decimal("0.30")
    assert ledger.gross_exposure_quote({"ETHUSDT": Decimal("105")}) == Decimal("105")
    assert ledger.unrealized_pnl_quote({"ETHUSDT": Decimal("105")}) == Decimal("5")
    assert ledger.equity_quote({"ETHUSDT": Decimal("105")}) == Decimal("10014.70")

    ledger.apply_fill(symbol="ETHUSDT", side="sell", quantity=Decimal("2"),
                      price=Decimal("120"), fee=Decimal("0.20"))
    assert ledger.position("ETHUSDT").quantity == Decimal("-1")
    assert ledger.position("ETHUSDT").average_cost == Decimal("120")
    assert ledger.realized_pnl_quote() == Decimal("29.50")
    assert ledger.fees_quote() == Decimal("0.50")
    assert ledger.equity_quote({"ETHUSDT": Decimal("115")}) == Decimal("10034.50")


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_p03_observation_rejects_nonfinite_price_or_volume(bad):
    with pytest.raises(PaperConfigError):
        MarketObservation("ETHUSDT", Decimal(bad), BASE, "frozen#0")
    with pytest.raises(PaperConfigError):
        MarketObservation("ETHUSDT", Decimal("2700"), BASE, "frozen#0", Decimal(bad))


def test_p03_frozen_feed_rejects_duplicate_reference_even_with_later_timestamp():
    with pytest.raises(PaperConfigError, match="duplicate"):
        FrozenMarketDataFeed(entries=(_entry(0), _entry(1, reference="frozen#0")))


def test_p03_frozen_feed_preserves_tied_timestamp_with_distinct_reference():
    # Existing frozen fixtures permit same-timestamp market events; unique
    # references preserve deterministic identity without inventing a time order.
    feed = FrozenMarketDataFeed(entries=(_entry(0), _entry(1, seconds=0)))
    assert len(feed.entries) == 2


@pytest.mark.parametrize("second", [
    _entry(1, reference="frozen#0"),
    _entry(2, seconds=0),
    _entry(2, seconds=-1),
])
def test_p03_live_feed_overlap_is_atomic(second):
    counter = iter([(_entry(0),), (second,)])
    feed = PublicMarketDataFeed(symbol="ETHUSDT", fetch_batch=lambda *_: next(counter))
    assert feed.pull() == 1
    assert feed.cursor == 1
    with pytest.raises(PaperConfigError):
        feed.pull()
    assert feed.cursor == 1
    assert tuple(feed.observations()) == (_entry(0),)


def test_p03_public_batch_duplicate_does_not_partially_advance():
    batches = iter([( _entry(0), ), (_entry(1), _entry(2, reference="frozen#1"))])
    feed = PublicMarketDataFeed(symbol="ETHUSDT", fetch_batch=lambda *_: next(batches))
    assert feed.pull() == 1
    with pytest.raises(PaperConfigError, match="duplicate"):
        feed.pull()
    assert feed.cursor == 1
    assert len(feed.observations()) == 1
