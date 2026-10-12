"""P-02 independent offline native Nautilus L2_MBP fill boundary fixtures."""
from decimal import Decimal
from uuid import UUID

import pytest

from crypto_trader.execution import (
    BookLevel, ExecutionOrderType, ExecutionRequest, ExecutionSide,
    InstrumentSpec, MarketBookUpdate, MarketTrade, OrderLifecycleState,
)
from crypto_trader.execution.nautilus import NautilusSimulationRunner

BOOK_TS = 1_767_225_600_000_000_000
PRICE = Decimal("2700.00")
INSTRUMENT = InstrumentSpec(symbol="ETHUSDT")


def _request() -> ExecutionRequest:
    return ExecutionRequest.create(
        correlation_id=UUID("44444444-4444-4444-8444-444444444444"),
        symbol="ETHUSDT", side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("1.00000000"), limit_price=PRICE,
    )


def _book() -> MarketBookUpdate:
    return MarketBookUpdate(
        levels=[
            BookLevel(side="BUY", price=PRICE, size=Decimal("100.00000000"), order_id=1),
            BookLevel(side="SELL", price=Decimal("2701.00"),
                      size=Decimal("100.00000000"), order_id=2),
        ], ts_ns=BOOK_TS, sequence=1,
    )


def _trade(size: str, offset_ms: int, identity: str) -> MarketTrade:
    return MarketTrade(
        price=PRICE, size=Decimal(size), aggressor="SELLER",
        ts_ns=BOOK_TS + offset_ms * 1_000_000, trade_id=identity,
    )


@pytest.mark.parametrize("after_queue,expected_filled", [
    (None, Decimal("0")),
    ("0.30000000", Decimal("0.30000000")),
    ("1.00000000", Decimal("1.00000000")),
])
def test_p02_real_native_engine_queue_states(after_queue, expected_filled):
    trades = [_trade("100.00000000", 10, "QUEUE-CLEAR")]
    if after_queue is not None:
        trades.append(_trade(after_queue, 20, "OWN-LIQUIDITY"))
    outcome = NautilusSimulationRunner(
        instrument=INSTRUMENT, book_updates=(_book(),), trades=tuple(trades),
    ).simulate((_request(),))
    assert outcome.engine_version == "1.231.0"
    assert outcome.queue_aware is True
    assert outcome.book_type == "L2_MBP"
    fills = [o for o in outcome.observations if o.last_fill_quantity is not None]
    actual = sum((o.last_fill_quantity for o in fills), Decimal("0"))
    assert actual == expected_filled
    if after_queue is None:
        assert not fills
    else:
        assert fills
    if after_queue is None:
        assert outcome.observations[-1].state == OrderLifecycleState.ACCEPTED
    elif expected_filled < Decimal("1"):
        assert outcome.observations[-1].state == OrderLifecycleState.PARTIALLY_FILLED
        assert outcome.observations[-1].remaining_quantity == Decimal("0.70000000")
    else:
        assert outcome.observations[-1].state == OrderLifecycleState.FILLED
        assert outcome.observations[-1].remaining_quantity == Decimal("0")
