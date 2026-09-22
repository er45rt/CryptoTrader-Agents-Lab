"""Registry, decoding, and versioning tests for the event contract layer."""

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from crypto_trader.events import (
    ExecutionFillRecorded,
    EventContractError,
    EventEnvelope,
    EventRegistry,
    EventValidationError,
    DuplicateEventRegistrationError,
    OrderStateChanged,
    RecoveryStateChanged,
    RiskDecisionRecorded,
    StrategyDecisionRecorded,
    TradeIntentCreated,
    UnknownEventTypeError,
    UnsupportedSchemaVersionError,
    V1_CONTRACTS,
    build_default_registry,
    decode_event,
    get_default_registry,
    reset_default_registry,
)
from crypto_trader.tests.contracts import fixtures_v1

UTC = timezone.utc
EXPECTED_CONTRACT_MODELS = set(V1_CONTRACTS)


@pytest.fixture(autouse=True)
def _clean_default_registry():
    reset_default_registry()
    yield
    reset_default_registry()


# --- Registration ----------------------------------------------------------

def test_default_registry_registers_every_v1_contract():
    registry = build_default_registry()

    assert set(registry.registered_event_types()) == {
        "strategy.decision.recorded",
        "trade.intent.created",
        "risk.decision.recorded",
        "order.state.changed",
        "execution.fill.recorded",
        "recovery.state.changed",
        # Phase 6 additive contract (existing schemas unchanged).
        "validity.disposition.recorded",
    }
    assert registry.registered_pairs() == tuple(sorted(registry.registered_pairs()))
    for contract in V1_CONTRACTS:
        assert registry.supported_versions(contract.model_fields["event_type"].default) == (1,)


def test_valid_registration_and_explicit_lookup():
    registry = EventRegistry()
    registry.register("order.state.changed", 1, OrderStateChanged)

    assert registry.resolve("order.state.changed", 1) is OrderStateChanged
    assert registry.registered_pairs() == (("order.state.changed", 1),)


def test_register_model_derives_type_and_version():
    registry = EventRegistry()
    registry.register_model(StrategyDecisionRecorded)

    assert registry.resolve("strategy.decision.recorded", 1) is StrategyDecisionRecorded


def test_duplicate_registration_is_rejected():
    registry = EventRegistry()
    registry.register("order.state.changed", 1, OrderStateChanged)

    with pytest.raises(DuplicateEventRegistrationError):
        registry.register("order.state.changed", 1, OrderStateChanged)

    with pytest.raises(DuplicateEventRegistrationError):
        registry.register_model(OrderStateChanged)


@pytest.mark.parametrize(
    "event_type,version,model",
    [
        ("Order.State.Changed", 1, OrderStateChanged),
        ("single", 1, OrderStateChanged),
        ("order.state.changed", 0, OrderStateChanged),
        ("order.state.changed", -1, OrderStateChanged),
        ("trade.intent.created", 1, OrderStateChanged),  # declaration mismatch
        ("trade.intent.created", 2, TradeIntentCreated),  # declaration mismatch
        ("trade.intent.created", 1, int),
    ],
)
def test_invalid_registration_is_rejected(event_type, version, model):
    with pytest.raises(EventContractError):
        EventRegistry().register(event_type, version, model)


def test_register_model_requires_declared_defaults():
    with pytest.raises(EventContractError):
        EventRegistry().register_model(EventEnvelope)


# --- Decoding --------------------------------------------------------------

@pytest.mark.parametrize(
    "fixture_name,expected_model",
    [
        ("root_strategy_decision", StrategyDecisionRecorded),
        ("causal_trade_intent", TradeIntentCreated),
        ("risk_decision", RiskDecisionRecorded),
        ("order_state_changed", OrderStateChanged),
        ("partial_fill", ExecutionFillRecorded),
        ("recovery_state_changed", RecoveryStateChanged),
    ],
)
def test_decode_returns_the_registered_model(fixture_name, expected_model):
    decoded = decode_event(fixtures_v1.fixture_json(fixture_name))

    assert type(decoded) is expected_model
    assert decoded.model_dump(mode="json") == fixtures_v1.fixture(fixture_name)


def test_decode_accepts_mapping_and_bytes():
    from_mapping = decode_event(fixtures_v1.fixture("partial_fill"))
    from_bytes = decode_event(fixtures_v1.fixture_json("partial_fill").encode("utf-8"))

    assert from_mapping == from_bytes


def test_unknown_event_type_is_rejected_with_typed_error():
    payload = fixtures_v1.fixture("root_strategy_decision")
    payload["event_type"] = "unknown.event.recorded"

    with pytest.raises(UnknownEventTypeError) as excinfo:
        decode_event(payload)

    assert "unknown.event.recorded" in str(excinfo.value)
    assert "strategy.decision.recorded" in str(excinfo.value)  # lists known types


def test_unsupported_schema_version_is_rejected_and_never_parsed_as_v1():
    payload = fixtures_v1.fixture("root_strategy_decision")
    payload["schema_version"] = 2

    with pytest.raises(UnsupportedSchemaVersionError) as excinfo:
        decode_event(payload)

    message = str(excinfo.value)
    assert "Supported versions: 1" in message
    assert "never parsed as v1" in message


def test_unsupported_version_for_unknown_type_reports_unknown_type():
    payload = fixtures_v1.fixture("root_strategy_decision")
    payload["event_type"] = "unknown.event.recorded"
    payload["schema_version"] = 3

    with pytest.raises(UnknownEventTypeError):
        decode_event(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["payload"].update(quantity="not-a-number"),
        lambda p: p["payload"].pop("price"),
        lambda p: p["payload"].update(broker_ack_id="transport-specific"),
        lambda p: p.update(producer="Bad Producer"),
        lambda p: p.update(symbol="ethusdt"),
    ],
)
def test_malformed_payload_is_rejected(mutate):
    payload = fixtures_v1.fixture("partial_fill")
    mutate(payload)

    with pytest.raises(EventValidationError) as excinfo:
        decode_event(payload)

    assert excinfo.value.details  # validation details are surfaced


def test_invalid_json_is_rejected():
    with pytest.raises(EventValidationError):
        decode_event("{not valid json")


def test_non_object_json_is_rejected():
    with pytest.raises(EventValidationError):
        decode_event("[1, 2, 3]")


def test_missing_event_type_or_version_is_rejected():
    without_type = fixtures_v1.fixture("root_strategy_decision")
    without_type.pop("event_type")
    with pytest.raises(EventValidationError):
        decode_event(without_type)

    without_version = fixtures_v1.fixture("root_strategy_decision")
    without_version.pop("schema_version")
    with pytest.raises(EventValidationError):
        decode_event(without_version)

    bad_version_type = fixtures_v1.fixture("root_strategy_decision")
    bad_version_type["schema_version"] = "1"
    with pytest.raises(EventValidationError):
        decode_event(bad_version_type)


# --- Isolation / determinism ----------------------------------------------

def test_isolated_registry_is_independent_of_the_default():
    isolated = EventRegistry()
    payload = fixtures_v1.fixture_json("root_strategy_decision")

    with pytest.raises(UnknownEventTypeError):
        isolated.decode(payload)  # nothing registered here

    assert type(decode_event(payload)) is StrategyDecisionRecorded  # default unaffected

    isolated.register_model(StrategyDecisionRecorded)
    assert type(isolated.decode(payload)) is StrategyDecisionRecorded
    assert "risk.decision.recorded" not in isolated.registered_event_types()


def test_registry_is_deterministic_across_instances():
    first, second = build_default_registry(), build_default_registry()

    assert first.registered_pairs() == second.registered_pairs()
    payload = fixtures_v1.fixture_json("recovery_state_changed")
    assert first.decode(payload) == second.decode(payload)


def test_decode_event_helper_uses_the_provided_registry():
    custom = EventRegistry()
    custom.register_model(OrderStateChanged)
    payload = fixtures_v1.fixture_json("order_state_changed")

    assert type(decode_event(payload, registry=custom)) is OrderStateChanged

    with pytest.raises(UnknownEventTypeError):
        decode_event(fixtures_v1.fixture_json("partial_fill"), registry=custom)


def test_default_registry_is_lazy_and_resettable():
    first = get_default_registry()
    assert first is get_default_registry()

    reset_default_registry()
    second = get_default_registry()

    assert second is not first
    assert second.registered_pairs() == first.registered_pairs()


# --- Versioning semantics --------------------------------------------------

def test_v1_fixtures_remain_parseable():
    for name in fixtures_v1.fixture_names():
        decoded = decode_event(fixtures_v1.fixture_json(name))
        assert decoded.schema_version == 1


def test_optional_fields_may_be_omitted_and_defaults_apply():
    """Backward-compatible additive fields must not break older payloads."""
    payload = fixtures_v1.fixture("partial_fill")
    payload["payload"].pop("liquidity")
    payload["payload"].pop("remaining_quantity")
    payload["payload"].pop("fee_asset")

    decoded = decode_event(payload)

    assert decoded.payload.liquidity is None
    assert decoded.payload.remaining_quantity is None
    assert decoded.payload.fee_asset is None
    assert decoded.payload.fee == Decimal("0.00400000")


def test_unknown_fields_are_rejected_rather_than_silently_ignored():
    """A newer payload is refused explicitly, never mis-parsed as the older contract."""
    payload = fixtures_v1.fixture("partial_fill")
    payload["payload"]["venue_order_id"] = "future-field"

    with pytest.raises(EventValidationError):
        decode_event(payload)


def test_events_carry_only_transport_agnostic_metadata():
    """No broker/broker-ack fields exist on the envelope."""
    field_names = set(EventEnvelope.model_fields)

    assert field_names == {
        "event_id",
        "event_type",
        "schema_version",
        "occurred_at",
        "correlation_id",
        "causation_id",
        "producer",
        "strategy_id",
        "order_id",
        "symbol",
        "sequence_number",
    }
    for forbidden in ("partition", "offset", "topic", "broker", "ack", "receipt_handle", "redis_key"):
        assert not any(forbidden in name for name in field_names)


def test_registry_does_not_expose_broker_operations():
    """The registry is a contract map, not a dispatcher/bus."""
    public_api = {name for name in dir(EventRegistry) if not name.startswith("_")}

    for forbidden in ("publish", "subscribe", "dispatch", "emit", "consume", "start", "run"):
        assert forbidden not in public_api


def test_decoded_events_are_immutable():
    decoding_uuid = uuid.uuid4()
    payload = fixtures_v1.fixture("root_strategy_decision")
    payload["event_id"] = str(decoding_uuid)
    decoded = decode_event(payload)

    assert decoded.event_id == decoding_uuid
    with pytest.raises(Exception):
        decoded.event_type = "order.state.changed"


def test_risk_and_intent_contracts_decode_with_decimal_precision_intact():
    intent = decode_event(fixtures_v1.fixture_json("causal_trade_intent"))
    risk = decode_event(fixtures_v1.fixture_json("risk_decision"))

    assert intent.payload.quantity == Decimal("1.50000000")
    assert intent.payload.limit_price == Decimal("100.25000000")
    assert risk.payload.approved_quantity == Decimal("1.00000000")
    assert json.loads(intent.to_json())["payload"]["quantity"] == "1.50000000"
