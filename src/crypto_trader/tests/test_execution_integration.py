"""Gate 8/9: deterministic OFFLINE end-to-end integration through the Phase 4 boundary.

dependencies -> our engine-independent request -> Phase 4 boundary -> Nautilus 1.231.0
-> simulated lifecycle/fill -> outbound translation -> our representation / Phase 3 events.

No network, no credentials, no live venue. Simulated in-process venue only.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from crypto_trader.events import ExecutionFillRecorded, OrderStateChanged
from crypto_trader.execution import (
    BookLevel,
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionRuntimeState,
    ExecutionSide,
    InstrumentSpec,
    MarketBookUpdate,
    MarketTrade,
    OrderLifecycleState,
    QueueAwareConfigurationError,
    UnsupportedExecutionRequestError,
)
from crypto_trader.execution.nautilus import (
    NautilusExecutionRuntime,
    QueueAwareSettings,
)

INSTRUMENT = InstrumentSpec(symbol="ETHUSDT")
PRICE = Decimal("2700.00")
TS_BASE_NS = 1_767_225_600_000_000_000
MS = 1_000_000


def book(ahead: str = "100.00000000", *, ts_ms: int = 0) -> MarketBookUpdate:
    return MarketBookUpdate(
        levels=[
            BookLevel(side="BUY", price=PRICE, size=Decimal(ahead), order_id=1),
            BookLevel(side="SELL", price=Decimal("2701.00"), size=Decimal("100.00000000"), order_id=2),
        ],
        ts_ns=TS_BASE_NS + ts_ms * MS,
        sequence=1,
    )


def trade(size: str, *, ts_ms: int, aggressor: str = "SELLER", trade_id: str = "T") -> MarketTrade:
    return MarketTrade(
        price=PRICE,
        size=Decimal(size),
        aggressor=aggressor,
        ts_ns=TS_BASE_NS + ts_ms * MS,
        trade_id=trade_id,
    )


def limit_request(quantity: str = "50.00000000", price: str = "2700.00") -> ExecutionRequest:
    return ExecutionRequest.create(
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal(quantity),
        limit_price=Decimal(price),
    )


# --- A. normal accepted/filled path ---

def test_A_normal_order_is_accepted_and_filled_end_to_end():
    runtime = NautilusExecutionRuntime(INSTRUMENT)
    request = limit_request()
    runtime.start()
    try:
        runtime.submit(request)
        runtime.feed_market_data(
            book_updates=[book()],
            trades=[
                trade("100.00000000", ts_ms=10, trade_id="A1"),  # clears the displayed queue
                trade("50.00000000", ts_ms=20, trade_id="A2"),   # fills our order
            ],
        )
        runtime.run()

        observations = runtime.observations()
        states = [o.state for o in observations]
        fills = [o for o in observations if o.last_fill_quantity is not None]

        assert OrderLifecycleState.SUBMITTED in states
        assert OrderLifecycleState.ACCEPTED in states
        assert states[-1] is OrderLifecycleState.FILLED
        assert fills[-1].filled_quantity == Decimal("50.00000000")
        assert fills[-1].remaining_quantity == Decimal("0.00000000")
        assert fills[-1].last_fill_price == PRICE
        assert all(o.correlation_id == request.correlation_id for o in observations)
        assert all(o.intent_id == request.intent_id for o in observations)
    finally:
        runtime.stop()

    assert runtime.state() is ExecutionRuntimeState.STOPPED


def test_A_phase3_events_are_produced_with_identity_preserved():
    runtime = NautilusExecutionRuntime(INSTRUMENT)
    request = limit_request()
    runtime.start()
    try:
        runtime.submit(request)
        runtime.feed_market_data(
            book_updates=[book()],
            trades=[trade("100.00000000", ts_ms=10, trade_id="A1"), trade("50.00000000", ts_ms=20, trade_id="A2")],
        )
        runtime.run()
        events = runtime.phase3_events()
    finally:
        runtime.stop()

    state_events = [e for e in events if isinstance(e, OrderStateChanged)]
    fill_events = [e for e in events if isinstance(e, ExecutionFillRecorded)]

    assert state_events, "expected Phase 3 order-state events"
    assert fill_events, "expected a Phase 3 fill event"
    assert {e.correlation_id for e in events} == {request.correlation_id}
    assert {str(e.order_id) for e in events} == {str(state_events[0].order_id)}
    assert fill_events[-1].payload.quantity == Decimal("50.00000000")
    assert fill_events[-1].payload.price == PRICE


# --- B. partial fill path under the proven queue-aware configuration ---

def test_B_partial_fill_is_reported_with_correct_quantities():
    runtime = NautilusExecutionRuntime(INSTRUMENT, queue_aware=True)
    request = limit_request()
    runtime.start()
    try:
        runtime.submit(request)
        runtime.feed_market_data(
            book_updates=[book()],
            trades=[
                trade("100.00000000", ts_ms=10, trade_id="B1"),  # clears the displayed queue
                trade("30.00000000", ts_ms=20, trade_id="B2"),   # only 30 available -> partial
            ],
        )
        runtime.run()
        observations = runtime.observations()
    finally:
        runtime.stop()

    partials = [o for o in observations if o.state is OrderLifecycleState.PARTIALLY_FILLED]
    assert partials, "expected a partially filled observation"
    assert partials[-1].filled_quantity == Decimal("30.00000000")
    assert partials[-1].remaining_quantity == Decimal("20.00000000")
    assert partials[-1].last_fill_quantity == Decimal("30.00000000")


# --- C. unsupported input fails closed ---

def test_C_unsupported_request_fails_closed_before_any_engine_order():
    runtime = NautilusExecutionRuntime(INSTRUMENT)
    runtime.start()
    try:
        with pytest.raises(UnsupportedExecutionRequestError):
            runtime.submit(limit_request(quantity="1.123456789"))  # precision the boundary never rounds

        with pytest.raises(UnsupportedExecutionRequestError):
            runtime.submit(
                ExecutionRequest.create(
                    correlation_id=uuid4(),
                    symbol="ETHUSDT",
                    side=ExecutionSide.BUY,
                    order_type=ExecutionOrderType.MARKET,
                    quantity=Decimal("1.00000000"),
                    limit_price=Decimal("2700.00"),  # market + price is contradictory
                )
            )
    finally:
        runtime.stop()

    assert runtime.observations() == ()


# --- D. incorrect queue-aware configuration fails fast ---

def test_D_queue_aware_runtime_rejects_untested_configuration():
    runtime = NautilusExecutionRuntime(
        INSTRUMENT,
        queue_aware=True,
        queue_settings=QueueAwareSettings(queue_position=True, liquidity_consumption=True, book_type="L1_MBP"),
    )

    with pytest.raises(QueueAwareConfigurationError):
        runtime.start()

    assert runtime.state() is ExecutionRuntimeState.STOPPED


def test_D_runtime_records_the_exact_supported_engine_version():
    runtime = NautilusExecutionRuntime(INSTRUMENT)
    runtime.start()
    try:
        assert runtime.engine_version() == "1.231.0"
    finally:
        runtime.stop()


# --- no engine types escape ---

def test_observations_and_events_contain_no_engine_types():
    runtime = NautilusExecutionRuntime(INSTRUMENT)
    runtime.start()
    try:
        runtime.submit(limit_request())
        runtime.feed_market_data(
            book_updates=[book()],
            trades=[trade("100.00000000", ts_ms=10, trade_id="A1"), trade("50.00000000", ts_ms=20, trade_id="A2")],
        )
        runtime.run()
        observations = runtime.observations()
        events = runtime.phase3_events()
    finally:
        runtime.stop()

    for observation in observations:
        for value in vars(observation).values():
            module = type(value).__module__
            assert not module.startswith("nautilus_trader"), f"engine type leaked: {module}"
    for event in events:
        assert not type(event).__module__.startswith("nautilus_trader")
