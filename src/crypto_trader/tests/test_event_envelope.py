"""Envelope-level contract tests: validation, serialization, idempotency, ordering."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from crypto_trader.events import (
    EventEnvelope,
    ExecutionFillRecorded,
    FillRecordPayload,
    OrderStateChanged,
    OrderStateChangePayload,
    StrategyDecisionPayload,
    StrategyDecisionRecorded,
    TradeIntentCreated,
    TradeIntentPayload,
    is_valid_event_type,
)

UTC = timezone.utc
CORRELATION = uuid.UUID("11111111-1111-4111-8111-111111111111")


def make_decision(**overrides):
    fields = {
        "occurred_at": datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        "correlation_id": CORRELATION,
        "producer": "agent.trading_plan_strategist",
        "symbol": "SYNTHUSDT",
        "strategy_id": "strat-1",
        "payload": StrategyDecisionPayload(
            action="buy", confidence="medium", rationale="synthetic rationale"
        ),
    }
    fields.update(overrides)
    return StrategyDecisionRecorded(**fields)


# --- Base validation -------------------------------------------------------

def test_valid_event_constructs_with_expected_metadata():
    event = make_decision()

    assert event.event_type == "strategy.decision.recorded"
    assert event.schema_version == 1
    assert isinstance(event.event_id, UUID)
    assert event.correlation_id == CORRELATION
    assert event.causation_id is None
    assert event.occurred_at.tzinfo is not None
    assert event.occurred_at.utcoffset() == timedelta(0)
    assert event.symbol == "SYNTHUSDT"
    assert event.sequence_number is None


def test_event_id_is_generated_and_unique_per_logical_event():
    first, second = make_decision(), make_decision()

    assert isinstance(first.event_id, UUID)
    assert first.event_id != second.event_id  # new logical event => new identity


def test_event_id_can_be_supplied_explicitly():
    fixed = uuid4()
    assert make_decision(event_id=fixed).event_id == fixed


@pytest.mark.parametrize(
    "naive_input",
    [
        datetime(2026, 1, 1, 12, 0, 0),
        "2026-01-01T12:00:00",
    ],
)
def test_naive_timestamp_is_rejected(naive_input):
    with pytest.raises(ValidationError) as excinfo:
        make_decision(occurred_at=naive_input)

    assert "timezone-aware" in str(excinfo.value)


def test_non_utc_timestamp_is_normalized_to_utc():
    plus_five = timezone(timedelta(hours=5))
    event = make_decision(occurred_at=datetime(2026, 1, 1, 17, 0, 0, tzinfo=plus_five))

    assert event.occurred_at.utcoffset() == timedelta(0)
    assert event.occurred_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("bad_version", [0, -1])
def test_invalid_schema_version_is_rejected(bad_version):
    with pytest.raises(ValidationError):
        make_decision(schema_version=bad_version)


@pytest.mark.parametrize("bad_sequence", [0, -1, -100])
def test_invalid_sequence_number_is_rejected(bad_sequence):
    with pytest.raises(ValidationError):
        OrderStateChanged(
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            correlation_id=CORRELATION,
            producer="engine.order_manager",
            order_id=uuid4(),
            sequence_number=bad_sequence,
            payload=OrderStateChangePayload(state="submitted"),
        )


@pytest.mark.parametrize(
    "bad_type",
    ["Order State Changed", "order-state-changed", "order..changed", "Order.changed", ""],
)
def test_invalid_event_type_naming_is_rejected(bad_type):
    with pytest.raises(ValidationError):
        EventEnvelope(
            event_type=bad_type,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            correlation_id=CORRELATION,
            producer="engine.test",
        )


def test_event_type_helper_matches_naming_convention():
    assert is_valid_event_type("order.state.changed")
    assert is_valid_event_type("execution.fill.recorded")
    assert not is_valid_event_type("Order.State.Changed")
    assert not is_valid_event_type("singlesegment")


def test_invalid_producer_is_rejected():
    with pytest.raises(ValidationError):
        make_decision(producer="Bad Producer")


def test_invalid_symbol_is_rejected():
    with pytest.raises(ValidationError):
        make_decision(symbol="ethusdt")


def test_self_causation_is_rejected():
    same = uuid4()
    with pytest.raises(ValidationError) as excinfo:
        make_decision(event_id=same, causation_id=same)

    assert "self-causation" in str(excinfo.value)


def test_future_schema_version_on_a_v1_contract_is_rejected():
    with pytest.raises(ValidationError):
        make_decision(schema_version=2)  # the V1 contract is a Literal[1]


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        make_decision(broker_ack_id="not-part-of-the-contract")


def test_envelope_is_immutable():
    event = make_decision()

    with pytest.raises(ValidationError):
        event.event_type = "order.state.changed"


# --- Serialization ---------------------------------------------------------

def test_json_round_trip_preserves_types_and_values():
    event = TradeIntentCreated(
        occurred_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        correlation_id=CORRELATION,
        causation_id=uuid4(),
        producer="agent.trading_plan_strategist",
        symbol="SYNTHUSDT",
        payload=TradeIntentPayload(
            side="buy",
            quantity=Decimal("1.50000000"),
            order_type="limit",
            limit_price=Decimal("100.25000000"),
        ),
    )

    decoded = TradeIntentCreated.model_validate_json(event.to_json())

    assert decoded == event
    assert isinstance(decoded.payload.quantity, Decimal)
    assert decoded.payload.quantity == Decimal("1.50000000")
    assert isinstance(decoded.event_id, UUID)
    assert decoded.event_id == event.event_id
    assert decoded.payload.limit_price == Decimal("100.25000000")


def test_decimal_serializes_as_json_string_not_binary_float():
    event = ExecutionFillRecorded(
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        correlation_id=CORRELATION,
        producer="engine.execution",
        order_id=uuid4(),
        payload=FillRecordPayload(quantity=Decimal("0.30000000"), price=Decimal("100.25000000")),
    )
    raw = json.loads(event.to_json())

    assert raw["payload"]["quantity"] == "0.30000000"
    assert isinstance(raw["payload"]["quantity"], str)
    assert isinstance(raw["payload"]["price"], str)


def test_finite_state_values_serialize_as_strings():
    payload = OrderStateChangePayload(state="partially_filled")
    raw = json.loads(payload.model_dump_json())

    assert raw == {"state": "partially_filled", "reason": None}


def test_uuid_and_timestamp_round_trip():
    event = make_decision()
    raw = json.loads(event.to_json())
    decoded = StrategyDecisionRecorded.model_validate_json(event.to_json())

    assert raw["event_id"] == str(event.event_id)
    assert decoded.event_id == event.event_id
    assert decoded.occurred_at == event.occurred_at
    assert decoded.occurred_at.utcoffset() == timedelta(0)


# --- Idempotency semantics -------------------------------------------------

def test_redelivery_of_the_same_event_reuses_the_same_event_id():
    event = make_decision()

    first_delivery = event.to_json()
    second_delivery = event.to_json()  # a publisher retry re-serializes the same event

    assert first_delivery == second_delivery
    assert (
        StrategyDecisionRecorded.model_validate_json(first_delivery).event_id
        == StrategyDecisionRecorded.model_validate_json(second_delivery).event_id
        == event.event_id
    )


def test_copying_an_event_for_redelivery_preserves_identity():
    event = make_decision()
    copy = event.model_copy(deep=True)

    assert copy.event_id == event.event_id
    assert copy == event


def test_two_new_events_with_identical_payloads_get_distinct_ids():
    first, second = make_decision(), make_decision()

    assert first.payload == second.payload
    assert first.event_id != second.event_id


# --- Correlation / causation ----------------------------------------------

def test_correlation_is_preserved_and_causation_links_parent_to_child():
    root = make_decision()
    child = TradeIntentCreated(
        occurred_at=datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC),
        correlation_id=root.correlation_id,
        causation_id=root.event_id,
        producer="agent.trading_plan_strategist",
        symbol="SYNTHUSDT",
        payload=TradeIntentPayload(side="buy", quantity=Decimal("1"), order_type="market"),
    )
    grandchild = OrderStateChanged(
        occurred_at=datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC),
        correlation_id=child.correlation_id,
        causation_id=child.event_id,
        producer="engine.order_manager",
        order_id=uuid4(),
        payload=OrderStateChangePayload(state="submitted"),
    )

    assert {root.correlation_id, child.correlation_id, grandchild.correlation_id} == {CORRELATION}
    assert child.causation_id == root.event_id
    assert grandchild.causation_id == child.event_id
    assert root.causation_id is None


# --- Ordering / sequence ---------------------------------------------------

def test_sequence_number_is_preserved_for_entity_scoped_ordering():
    event = OrderStateChanged(
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        correlation_id=CORRELATION,
        producer="engine.order_manager",
        order_id=uuid4(),
        sequence_number=7,
        payload=OrderStateChangePayload(state="accepted"),
    )

    decoded = OrderStateChanged.model_validate_json(event.to_json())

    assert decoded.sequence_number == 7


def test_sequence_number_is_optional_at_the_base_layer():
    assert make_decision().sequence_number is None
