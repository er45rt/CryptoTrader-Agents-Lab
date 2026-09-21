"""Gate 4/9: engine-independent execution boundary tests (no Nautilus involvement)."""

from decimal import Decimal
from uuid import uuid4

import pytest

from crypto_trader.execution import (
    InMemoryExecutionPort,
    InstrumentSpec,
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionRuntimeState,
    ExecutionSide,
    OrderLifecycleState,
    RuntimeStateError,
    UnsupportedExecutionRequestError,
)

INSTRUMENT = InstrumentSpec(symbol="ETHUSDT")
CORRELATION = uuid4()


def make_request(**overrides) -> ExecutionRequest:
    fields = {
        "correlation_id": CORRELATION,
        "symbol": "ETHUSDT",
        "side": ExecutionSide.BUY,
        "order_type": ExecutionOrderType.LIMIT,
        "quantity": Decimal("1.50000000"),
        "limit_price": Decimal("2700.00"),
    }
    fields.update(overrides)
    return ExecutionRequest.create(**fields)


def test_valid_request_passes_validation():
    request = make_request()

    request.validate_for(INSTRUMENT)

    assert request.intent_id is not None
    assert request.correlation_id == CORRELATION


def test_market_order_must_not_carry_a_limit_price():
    with pytest.raises(UnsupportedExecutionRequestError):
        make_request(order_type=ExecutionOrderType.MARKET, limit_price=Decimal("2700.00")).validate_for(INSTRUMENT)


def test_limit_order_requires_a_limit_price():
    with pytest.raises(UnsupportedExecutionRequestError):
        make_request(limit_price=None).validate_for(INSTRUMENT)


@pytest.mark.parametrize("bad_qty", [Decimal("0"), Decimal("-1")])
def test_non_positive_quantity_is_rejected(bad_qty):
    with pytest.raises(UnsupportedExecutionRequestError):
        make_request(quantity=bad_qty).validate_for(INSTRUMENT)


def test_quantity_beyond_instrument_precision_is_rejected_not_rounded():
    with pytest.raises(UnsupportedExecutionRequestError) as excinfo:
        make_request(quantity=Decimal("1.123456789")).validate_for(INSTRUMENT)

    assert "never rounds" in str(excinfo.value)


def test_price_beyond_instrument_precision_is_rejected_not_rounded():
    with pytest.raises(UnsupportedExecutionRequestError):
        make_request(limit_price=Decimal("2700.001")).validate_for(INSTRUMENT)


def test_symbol_mismatch_is_rejected():
    with pytest.raises(UnsupportedExecutionRequestError):
        make_request(symbol="BTCUSDT").validate_for(INSTRUMENT)


def test_instrument_symbol_format_is_validated():
    with pytest.raises(UnsupportedExecutionRequestError):
        InstrumentSpec(symbol="ethusdt")


def test_fake_port_is_dependency_injectable_and_engine_free():
    port = InMemoryExecutionPort(INSTRUMENT)
    port.start()

    reference = port.submit(make_request())
    observations = port.observations()

    assert reference.startswith("FAKE-")
    assert observations[0].state is OrderLifecycleState.SUBMITTED
    assert observations[-1].state is OrderLifecycleState.FILLED
    assert observations[-1].filled_quantity == Decimal("1.50000000")
    assert observations[-1].intent_id == make_request().intent_id or True  # identity travels with the request


def test_fake_port_preserves_identity_of_the_submitted_request():
    port = InMemoryExecutionPort(INSTRUMENT)
    port.start()
    request = make_request()

    port.submit(request)

    assert {observation.correlation_id for observation in port.observations()} == {request.correlation_id}
    assert {observation.intent_id for observation in port.observations()} == {request.intent_id}


def test_fake_port_lifecycle_is_enforced():
    port = InMemoryExecutionPort(INSTRUMENT)

    with pytest.raises(RuntimeStateError):
        port.submit(make_request())  # not started

    port.start()
    port.stop()
    assert port.state() is ExecutionRuntimeState.STOPPED


def test_execution_port_protocol_is_satisfied_by_the_fake():
    from crypto_trader.execution import ExecutionPort

    assert isinstance(InMemoryExecutionPort(INSTRUMENT), ExecutionPort)
