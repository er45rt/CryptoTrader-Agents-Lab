"""V1 domain contract tests: six pipeline boundaries plus fixture drift detection."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from crypto_trader.events import (
    EXECUTION_FILL_EVENT_TYPE,
    ORDER_STATE_EVENT_TYPE,
    RECOVERY_EVENT_TYPE,
    RISK_DECISION_EVENT_TYPE,
    STRATEGY_DECISION_EVENT_TYPE,
    TRADE_INTENT_EVENT_TYPE,
    ExecutionFillRecorded,
    FillRecordPayload,
    OrderStateChanged,
    OrderStateChangePayload,
    RecoveryStateChanged,
    RecoveryStateChangePayload,
    RiskDecisionPayload,
    RiskDecisionRecorded,
    StrategyDecisionPayload,
    StrategyDecisionRecorded,
    TradeIntentCreated,
    TradeIntentPayload,
    V1_CONTRACTS,
    decode_event,
)
from crypto_trader.tests.contracts import fixtures_v1

UTC = timezone.utc
CORRELATION = uuid.UUID(fixtures_v1.CORRELATION_ID)
ORDER_ID = uuid.UUID(fixtures_v1.ORDER_ID)

ENVELOPE_FIELDS = {
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


def base_fields(**overrides):
    fields = {
        "occurred_at": datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        "correlation_id": CORRELATION,
        "producer": "agent.test_producer",
        "symbol": "SYNTHUSDT",
    }
    fields.update(overrides)
    return fields


# --- 1. Strategy decision --------------------------------------------------

def test_strategy_decision_contract_validates():
    event = StrategyDecisionRecorded(
        **base_fields(
            strategy_id="strat-1",
            payload=StrategyDecisionPayload(
                action="buy", confidence="high", rationale="synthetic rationale"
            ),
        )
    )

    assert event.event_type == STRATEGY_DECISION_EVENT_TYPE == "strategy.decision.recorded"
    assert event.schema_version == 1
    assert event.payload.action == "buy"


@pytest.mark.parametrize("bad_action", ["long", "BUY", "", None])
def test_strategy_decision_rejects_unknown_actions(bad_action):
    with pytest.raises(ValidationError):
        StrategyDecisionPayload(action=bad_action, confidence="high", rationale="r")


def test_strategy_decision_requires_a_rationale():
    with pytest.raises(ValidationError):
        StrategyDecisionPayload(action="buy", confidence="high", rationale="")


# --- 2. Trade intent -------------------------------------------------------

def test_trade_intent_contract_validates_limit_and_market():
    limit_intent = TradeIntentCreated(
        **base_fields(
            strategy_id="strat-1",
            payload=TradeIntentPayload(
                side="buy",
                quantity=Decimal("1.5"),
                order_type="limit",
                limit_price=Decimal("100.25"),
            ),
        )
    )
    market_intent = TradeIntentCreated(
        **base_fields(payload=TradeIntentPayload(side="sell", quantity=Decimal("2"), order_type="market"))
    )

    assert limit_intent.event_type == TRADE_INTENT_EVENT_TYPE
    assert limit_intent.payload.limit_price == Decimal("100.25")
    assert market_intent.payload.limit_price is None


def test_trade_intent_rejects_inconsistent_order_type_fields():
    with pytest.raises(ValidationError):
        TradeIntentPayload(side="buy", quantity=Decimal("1"), order_type="limit")

    with pytest.raises(ValidationError):
        TradeIntentPayload(
            side="buy", quantity=Decimal("1"), order_type="market", limit_price=Decimal("10")
        )


@pytest.mark.parametrize("bad_quantity", [Decimal("0"), Decimal("-1")])
def test_trade_intent_rejects_non_positive_quantity(bad_quantity):
    with pytest.raises(ValidationError):
        TradeIntentPayload(side="buy", quantity=bad_quantity, order_type="market")


# --- 3. Risk decision ------------------------------------------------------

def test_risk_decision_contract_validates_both_outcomes():
    approved = RiskDecisionRecorded(
        **base_fields(
            payload=RiskDecisionPayload(
                approved=True, reason="within limits", approved_quantity=Decimal("1.0")
            )
        )
    )
    rejected = RiskDecisionRecorded(
        **base_fields(payload=RiskDecisionPayload(approved=False, reason="exceeds limits"))
    )

    assert approved.event_type == RISK_DECISION_EVENT_TYPE
    assert approved.payload.approved_quantity == Decimal("1.0")
    assert rejected.payload.approved_quantity is None


def test_risk_decision_requires_quantity_consistency():
    with pytest.raises(ValidationError):
        RiskDecisionPayload(approved=True, reason="r")  # missing approved_quantity

    with pytest.raises(ValidationError):
        RiskDecisionPayload(
            approved=False, reason="r", approved_quantity=Decimal("1")
        )  # rejected must not carry a quantity


# --- 4. Order lifecycle ----------------------------------------------------

def test_order_lifecycle_contract_validates():
    event = OrderStateChanged(
        **base_fields(
            order_id=ORDER_ID,
            sequence_number=3,
            producer="engine.order_manager",
            payload=OrderStateChangePayload(state="partially_filled"),
        )
    )

    assert event.event_type == ORDER_STATE_EVENT_TYPE
    assert event.order_id == ORDER_ID
    assert event.sequence_number == 3


@pytest.mark.parametrize("state", ["rejected", "canceled", "expired"])
def test_order_lifecycle_requires_reason_for_terminal_failure_states(state):
    with pytest.raises(ValidationError):
        OrderStateChangePayload(state=state)

    assert OrderStateChangePayload(state=state, reason="synthetic reason").reason


def test_order_lifecycle_rejects_unknown_state():
    with pytest.raises(ValidationError):
        OrderStateChangePayload(state="partially-executed")


# --- 5. Execution / fill ---------------------------------------------------

def test_partial_fill_contract_validates():
    event = ExecutionFillRecorded(
        **base_fields(
            order_id=ORDER_ID,
            sequence_number=2,
            producer="engine.execution",
            payload=FillRecordPayload(
                quantity=Decimal("0.4"),
                price=Decimal("100.25"),
                fee=Decimal("0.004"),
                fee_asset="SYNTHUSDT",
                remaining_quantity=Decimal("0.6"),
                liquidity="taker",
            ),
        )
    )

    assert event.event_type == EXECUTION_FILL_EVENT_TYPE
    assert event.payload.remaining_quantity == Decimal("0.6")  # still partially open


def test_fill_contract_rejects_invalid_numbers():
    with pytest.raises(ValidationError):
        FillRecordPayload(quantity=Decimal("0"), price=Decimal("1"))

    with pytest.raises(ValidationError):
        FillRecordPayload(quantity=Decimal("1"), price=Decimal("1"), fee=Decimal("-0.1"))

    with pytest.raises(ValidationError):
        FillRecordPayload(quantity=Decimal("1"), price=Decimal("1"), remaining_quantity=Decimal("-1"))


def test_fill_contract_defaults_fee_to_zero_and_allows_full_fill():
    payload = FillRecordPayload(
        quantity=Decimal("1"), price=Decimal("100"), remaining_quantity=Decimal("0")
    )

    assert payload.fee == Decimal("0")
    assert payload.remaining_quantity == Decimal("0")  # fully filled


# --- 6. Recovery / reconciliation -----------------------------------------

def test_recovery_state_contract_validates():
    event = RecoveryStateChanged(
        **base_fields(
            order_id=ORDER_ID,
            producer="engine.recovery",
            payload=RecoveryStateChangePayload(
                state="detected", reason="sequence gap", affected_order_ids=[ORDER_ID]
            ),
        )
    )

    assert event.event_type == RECOVERY_EVENT_TYPE
    assert event.payload.affected_order_ids == [ORDER_ID]


def test_recovery_contract_rejects_unknown_state_and_empty_reason():
    with pytest.raises(ValidationError):
        RecoveryStateChangePayload(state="recovered", reason="r")

    with pytest.raises(ValidationError):
        RecoveryStateChangePayload(state="detected", reason="")


def test_recovery_state_contract_defaults_to_no_affected_orders():
    assert RecoveryStateChangePayload(state="reconciling", reason="r").affected_order_ids == []


# --- Fixtures / drift detection -------------------------------------------

@pytest.mark.parametrize(
    "name,expected_model",
    [
        ("root_strategy_decision", StrategyDecisionRecorded),
        ("causal_trade_intent", TradeIntentCreated),
        ("risk_decision", RiskDecisionRecorded),
        ("order_state_changed", OrderStateChanged),
        ("partial_fill", ExecutionFillRecorded),
        ("recovery_state_changed", RecoveryStateChanged),
    ],
)
def test_fixtures_still_match_the_contracts(name, expected_model):
    canon = fixtures_v1.fixture(name)
    decoded = decode_event(fixtures_v1.fixture_json(name))

    assert type(decoded) is expected_model
    # Drift detection: re-serializing the decoded fixture must reproduce it exactly.
    assert decoded.model_dump(mode="json") == canon


def test_fixture_payload_fields_match_the_declared_contracts():
    for name in fixtures_v1.fixture_names():
        canon = fixtures_v1.fixture(name)
        decoded = decode_event(canon)

        assert set(type(decoded).model_fields) == ENVELOPE_FIELDS | {"payload"}
        assert set(type(decoded.payload).model_fields) == set(canon["payload"])


def test_fixtures_form_a_consistent_causal_chain():
    root = decode_event(fixtures_v1.fixture_json("root_strategy_decision"))
    intent = decode_event(fixtures_v1.fixture_json("causal_trade_intent"))
    risk = decode_event(fixtures_v1.fixture_json("risk_decision"))
    order = decode_event(fixtures_v1.fixture_json("order_state_changed"))
    fill = decode_event(fixtures_v1.fixture_json("partial_fill"))
    recovery = decode_event(fixtures_v1.fixture_json("recovery_state_changed"))

    chain = [root, intent, risk, order, fill, recovery]

    assert {event.correlation_id for event in chain} == {CORRELATION}
    assert root.causation_id is None
    assert intent.causation_id == root.event_id
    assert risk.causation_id == intent.event_id
    assert order.causation_id == risk.event_id
    assert fill.causation_id == order.event_id
    assert recovery.causation_id == fill.event_id


def test_order_scoped_sequence_numbers_increase_within_the_fixture_chain():
    order = decode_event(fixtures_v1.fixture_json("order_state_changed"))
    fill = decode_event(fixtures_v1.fixture_json("partial_fill"))

    assert order.order_id == fill.order_id == ORDER_ID
    assert order.sequence_number == 1
    assert fill.sequence_number == 2
    assert fill.payload.remaining_quantity > 0  # partial, not a full fill


def test_every_v1_contract_is_registered_with_a_stable_type_and_version():
    registry_pairs = {contract.model_fields["event_type"].default for contract in V1_CONTRACTS}

    assert registry_pairs == {
        "strategy.decision.recorded",
        "trade.intent.created",
        "risk.decision.recorded",
        "order.state.changed",
        "execution.fill.recorded",
        "recovery.state.changed",
        # Phase 6 additive contract (existing schemas unchanged).
        "validity.disposition.recorded",
        # Phase 7 additive contract (existing schemas unchanged).
        "risk.evaluation.recorded",
        # Phase 9 additive contract (existing schemas unchanged).
        "safety.state.changed",
    }
    assert all(contract.model_fields["schema_version"].default == 1 for contract in V1_CONTRACTS)


def test_decimal_precision_is_exact_for_string_inputs():
    payload = TradeIntentPayload(
        side="buy", quantity="1.50000000", order_type="limit", limit_price="100.25000000"
    )

    assert payload.quantity == Decimal("1.50000000")
    assert payload.limit_price == Decimal("100.25000000")
    assert str(payload.quantity) == "1.50000000"  # no binary-float rounding
