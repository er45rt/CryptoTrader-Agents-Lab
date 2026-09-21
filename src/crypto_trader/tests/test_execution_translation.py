"""Gate 5/6: inbound and outbound translation tests."""

from datetime import timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from crypto_trader.events import ExecutionFillRecorded, OrderStateChanged
from crypto_trader.execution import (
    ExecutionObservation,
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionSide,
    ExecutionTimeInForce,
    InstrumentSpec,
    OrderLifecycleState,
    TranslationError,
    UnsupportedExecutionRequestError,
)
from crypto_trader.execution.nautilus import inbound, outbound

from nautilus_trader.model.enums import OrderSide, TimeInForce

INSTRUMENT = InstrumentSpec(symbol="ETHUSDT")
TS_NS = 1_767_225_600_010_000_000


def make_request(**overrides) -> ExecutionRequest:
    fields = {
        "correlation_id": uuid4(),
        "symbol": "ETHUSDT",
        "side": ExecutionSide.BUY,
        "order_type": ExecutionOrderType.LIMIT,
        "quantity": Decimal("1.50000000"),
        "limit_price": Decimal("2700.00"),
    }
    fields.update(overrides)
    return ExecutionRequest.create(**fields)


# --- inbound ---

def test_inbound_side_and_time_in_force_mapping_is_explicit():
    assert inbound.SIDE_TO_ENGINE[ExecutionSide.BUY] is OrderSide.BUY
    assert inbound.SIDE_TO_ENGINE[ExecutionSide.SELL] is OrderSide.SELL
    assert inbound.TIF_TO_ENGINE[ExecutionTimeInForce.GTC] is TimeInForce.GTC
    assert inbound.TIF_TO_ENGINE[ExecutionTimeInForce.IOC] is TimeInForce.IOC
    assert inbound.TIF_TO_ENGINE[ExecutionTimeInForce.FOK] is TimeInForce.FOK


def test_correlation_tags_carry_intent_and_correlation_identity():
    request = make_request()

    tags = inbound.correlation_tags(request)

    assert f"intent:{request.intent_id}" in tags
    assert f"corr:{request.correlation_id}" in tags


def test_decimal_quantity_and_price_are_preserved_exactly():
    qty_value = Decimal("1.23000000")
    price_value = Decimal("2700.10")

    assert str(inbound.to_quantity(qty_value, INSTRUMENT)) == "1.23000000"
    assert str(inbound.to_price(price_value, INSTRUMENT)) == "2700.10"


def test_inbound_refuses_unrepresentable_precision():
    with pytest.raises(UnsupportedExecutionRequestError):
        inbound.to_price(Decimal("2700.001"), INSTRUMENT)

    with pytest.raises(UnsupportedExecutionRequestError):
        inbound.to_quantity(Decimal("1.123456789"), INSTRUMENT)


def test_inbound_book_and_trade_translation_uses_public_types():
    from crypto_trader.execution import BookLevel, MarketBookUpdate, MarketTrade

    deltas = inbound.to_book_deltas(
        MarketBookUpdate(
            levels=[BookLevel(side="BUY", price=Decimal("2700.00"), size=Decimal("100.00000000"), order_id=1)],
            ts_ns=TS_NS,
            sequence=1,
        ),
        INSTRUMENT,
    )
    tick = inbound.to_trade_tick(
        MarketTrade(
            price=Decimal("2700.00"),
            size=Decimal("80.00000000"),
            aggressor="SELLER",
            ts_ns=TS_NS,
            trade_id="T1",
        ),
        INSTRUMENT,
    )

    assert str(deltas.instrument_id) == "ETHUSDT.SIM"
    assert len(deltas.deltas) == 1
    assert str(tick.price) == "2700.00"


def test_inbound_refuses_unsupported_book_side_and_aggressor():
    from crypto_trader.execution import BookLevel, MarketBookUpdate, MarketTrade

    with pytest.raises(TranslationError):
        inbound.to_book_deltas(
            MarketBookUpdate(levels=[BookLevel(side="SIDEWAYS", price=Decimal("1"), size=Decimal("1"), order_id=1)], ts_ns=TS_NS),
            INSTRUMENT,
        )

    with pytest.raises(TranslationError):
        inbound.to_trade_tick(
            MarketTrade(price=Decimal("1"), size=Decimal("1"), aggressor="MAYBE", ts_ns=TS_NS, trade_id="T"),
            INSTRUMENT,
        )


# --- outbound ---

@pytest.mark.parametrize(
    "engine_status,expected",
    [
        ("SUBMITTED", OrderLifecycleState.SUBMITTED),
        ("ACCEPTED", OrderLifecycleState.ACCEPTED),
        ("PARTIALLY_FILLED", OrderLifecycleState.PARTIALLY_FILLED),
        ("FILLED", OrderLifecycleState.FILLED),
        ("CANCELED", OrderLifecycleState.CANCELED),
        ("REJECTED", OrderLifecycleState.REJECTED),
        ("EXPIRED", OrderLifecycleState.EXPIRED),
    ],
)
def test_outbound_status_mapping_is_explicit(engine_status, expected):
    assert outbound.state_from_engine_status(engine_status) is expected


@pytest.mark.parametrize("unknown", ["PENDING_UPDATE", "PENDING_CANCEL", "INITIALIZED", "EMULATED"])
def test_outbound_unknown_status_fails_visibly(unknown):
    with pytest.raises(TranslationError) as excinfo:
        outbound.state_from_engine_status(unknown)

    assert unknown in str(excinfo.value)


def test_order_uuid_mapping_is_deterministic_and_identity_preserving():
    first = outbound.deterministic_order_uuid("O-20260101-000000-001-000-1")
    second = outbound.deterministic_order_uuid("O-20260101-000000-001-000-1")
    other = outbound.deterministic_order_uuid("O-20260101-000000-001-000-2")

    assert isinstance(first, UUID)
    assert first == second
    assert first != other


def test_phase3_order_state_event_preserves_correlation_and_symbol():
    request = make_request()
    observation = ExecutionObservation(
        order_reference="O-1",
        intent_id=request.intent_id,
        correlation_id=request.correlation_id,
        symbol="ETHUSDT",
        state=OrderLifecycleState.ACCEPTED,
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("1.50000000"),
        ts_ns=TS_NS,
    )

    event = outbound.to_phase3_order_state_event(observation, sequence_number=1)

    assert isinstance(event, OrderStateChanged)
    assert event.correlation_id == request.correlation_id
    assert event.symbol == "ETHUSDT"
    assert event.payload.state == "accepted"
    assert event.occurred_at.tzinfo == timezone.utc
    assert event.order_id == outbound.deterministic_order_uuid("O-1")
    assert event.sequence_number == 1


def test_phase3_fill_event_preserves_decimal_quantities_and_prices():
    observation = ExecutionObservation(
        order_reference="O-1",
        intent_id=uuid4(),
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        state=OrderLifecycleState.PARTIALLY_FILLED,
        filled_quantity=Decimal("10.00000000"),
        remaining_quantity=Decimal("40.00000000"),
        last_fill_quantity=Decimal("10.00000000"),
        last_fill_price=Decimal("2700.00"),
        fee=Decimal("0.01000000"),
        fee_asset="USDT",
        ts_ns=TS_NS,
    )

    event = outbound.to_phase3_fill_event(observation)

    assert isinstance(event, ExecutionFillRecorded)
    assert event.payload.quantity == Decimal("10.00000000")
    assert event.payload.price == Decimal("2700.00")
    assert event.payload.remaining_quantity == Decimal("40.00000000")
    assert event.payload.fee == Decimal("0.01000000")
    assert event.symbol == "ETHUSDT"


def test_fill_event_translation_requires_a_real_fill():
    observation = ExecutionObservation(
        order_reference="O-1",
        intent_id=uuid4(),
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        state=OrderLifecycleState.ACCEPTED,
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("1"),
        ts_ns=TS_NS,
    )

    with pytest.raises(TranslationError):
        outbound.to_phase3_fill_event(observation)


def test_translation_refuses_to_guess_a_missing_timestamp():
    observation = ExecutionObservation(
        order_reference="O-1",
        intent_id=uuid4(),
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        state=OrderLifecycleState.ACCEPTED,
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("1"),
        ts_ns=0,
    )

    with pytest.raises(TranslationError):
        outbound.to_phase3_order_state_event(observation)
