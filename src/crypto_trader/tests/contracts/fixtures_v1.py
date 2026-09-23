"""Fixed V1 event fixtures (Phase 3).

These fixtures are canonical, JSON-compatible snapshots of V1 events with deterministic
ids and timestamps. Contract tests decode them and compare the re-serialized result with
the fixture, so any accidental contract change (field rename, type change, default
change) fails loudly as drift.

No real credentials, accounts, orders, or market data appear here - every value is
synthetic. Amounts are strings because ``Decimal`` serializes as a JSON string, and
timestamps are UTC ISO-8601 with a ``Z`` suffix, matching Pydantic's JSON output.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "CORRELATION_ID",
    "FIXTURES",
    "FIXTURE_CAUSAL_TRADE_INTENT",
    "FIXTURE_ORDER_STATE_CHANGED",
    "FIXTURE_ORDER_SUBMISSION_ATTEMPTED",
    "FIXTURE_PARTIAL_FILL",
    "FIXTURE_RECOVERY_STATE_CHANGED",
    "FIXTURE_RISK_DECISION",
    "FIXTURE_ROOT_STRATEGY_DECISION",
    "fixture",
    "fixture_json",
    "fixture_names",
]

# Deterministic ids shared across fixtures so the causal chain is stable.
CORRELATION_ID = "11111111-1111-4111-8111-111111111111"
ROOT_EVENT_ID = "22222222-2222-4222-8222-222222222222"
TRADE_INTENT_EVENT_ID = "33333333-3333-4333-8333-333333333333"
ORDER_ID = "44444444-4444-4444-8444-444444444444"


# 1. Normal root event: a strategy decision with no causation.
FIXTURE_ROOT_STRATEGY_DECISION: dict[str, Any] = {
    "event_id": ROOT_EVENT_ID,
    "event_type": "strategy.decision.recorded",
    "schema_version": 1,
    "occurred_at": "2026-01-01T12:00:00Z",
    "correlation_id": CORRELATION_ID,
    "causation_id": None,
    "producer": "agent.trading_plan_strategist",
    "strategy_id": "strat-synthetic-1",
    "order_id": None,
    "symbol": "SYNTHUSDT",
    "sequence_number": None,
    "payload": {
        "action": "buy",
        "confidence": "medium",
        "rationale": "Synthetic fixture rationale: momentum above support.",
    },
}

# 2. Causal child event: same correlation, causation_id points at the root event_id.
FIXTURE_CAUSAL_TRADE_INTENT: dict[str, Any] = {
    "event_id": TRADE_INTENT_EVENT_ID,
    "event_type": "trade.intent.created",
    "schema_version": 1,
    "occurred_at": "2026-01-01T12:00:05Z",
    "correlation_id": CORRELATION_ID,
    "causation_id": ROOT_EVENT_ID,
    "producer": "agent.trading_plan_strategist",
    "strategy_id": "strat-synthetic-1",
    "order_id": None,
    "symbol": "SYNTHUSDT",
    "sequence_number": None,
    "payload": {
        "side": "buy",
        "quantity": "1.50000000",
        "order_type": "limit",
        "limit_price": "100.25000000",
        "rationale": "Synthetic fixture rationale: enter at support.",
    },
}

# 3. Risk decision (completes the intent -> risk boundary).
FIXTURE_RISK_DECISION: dict[str, Any] = {
    "event_id": "55555555-5555-4555-8555-555555555555",
    "event_type": "risk.decision.recorded",
    "schema_version": 1,
    "occurred_at": "2026-01-01T12:00:07Z",
    "correlation_id": CORRELATION_ID,
    "causation_id": TRADE_INTENT_EVENT_ID,
    "producer": "engine.risk",
    "strategy_id": "strat-synthetic-1",
    "order_id": None,
    "symbol": "SYNTHUSDT",
    "sequence_number": None,
    "payload": {
        "approved": True,
        "reason": "Synthetic fixture rationale: within size and loss limits.",
        "approved_quantity": "1.00000000",
    },
}

# 4. Order-related event carrying an entity sequence number.
FIXTURE_ORDER_STATE_CHANGED: dict[str, Any] = {
    "event_id": "66666666-6666-4666-8666-666666666666",
    "event_type": "order.state.changed",
    "schema_version": 1,
    "occurred_at": "2026-01-01T12:00:10Z",
    "correlation_id": CORRELATION_ID,
    "causation_id": "55555555-5555-4555-8555-555555555555",
    "producer": "engine.order_manager",
    "strategy_id": "strat-synthetic-1",
    "order_id": ORDER_ID,
    "symbol": "SYNTHUSDT",
    "sequence_number": 1,
    "payload": {
        "state": "submitted",
        "reason": None,
    },
}

# 5. Partial fill: remaining quantity is still open.
FIXTURE_PARTIAL_FILL: dict[str, Any] = {
    "event_id": "77777777-7777-4777-8777-777777777777",
    "event_type": "execution.fill.recorded",
    "schema_version": 1,
    "occurred_at": "2026-01-01T12:00:15Z",
    "correlation_id": CORRELATION_ID,
    "causation_id": "66666666-6666-4666-8666-666666666666",
    "producer": "engine.execution",
    "strategy_id": "strat-synthetic-1",
    "order_id": ORDER_ID,
    "symbol": "SYNTHUSDT",
    "sequence_number": 2,
    "payload": {
        "quantity": "0.40000000",
        "price": "100.25000000",
        "fee": "0.00400000",
        "fee_asset": "SYNTHUSDT",
        "remaining_quantity": "0.60000000",
        "liquidity": "taker",
    },
}

# 6. Recovery/reconciliation state change.
FIXTURE_RECOVERY_STATE_CHANGED: dict[str, Any] = {
    "event_id": "88888888-8888-4888-8888-888888888888",
    "event_type": "recovery.state.changed",
    "schema_version": 1,
    "occurred_at": "2026-01-01T12:00:20Z",
    "correlation_id": CORRELATION_ID,
    "causation_id": "77777777-7777-4777-8777-777777777777",
    "producer": "engine.recovery",
    "strategy_id": "strat-synthetic-1",
    "order_id": ORDER_ID,
    "symbol": "SYNTHUSDT",
    "sequence_number": None,
    "payload": {
        "state": "detected",
        "reason": "Synthetic fixture rationale: sequence gap observed for this order.",
        "affected_order_ids": [ORDER_ID],
    },
}

# 7. Durable pre-send submission attempt (Phase 15 durability, additive contract).
FIXTURE_ORDER_SUBMISSION_ATTEMPTED: dict[str, Any] = {
    "event_id": "99999999-9999-4999-8999-999999999999",
    "event_type": "order.submission.attempted",
    "schema_version": 1,
    "occurred_at": "2026-01-01T12:00:01Z",
    "correlation_id": CORRELATION_ID,
    "causation_id": None,
    "producer": "application.production",
    "strategy_id": "strat-synthetic-1",
    "order_id": ORDER_ID,
    "symbol": "SYNTHUSDT",
    "sequence_number": None,
    "payload": {
        "venue_reference": "ct-44444444444444444444",
        "intent_id": "55555555-5555-4555-8555-555555555555",
        "side": "buy",
        "order_type": "limit",
        "quantity": "1.00000000",
        "limit_price": "100.25000000",
        "time_in_force": "gtc",
        "venue_producer": "infrastructure.binance_testnet",
    },
}

FIXTURES: dict[str, dict[str, Any]] = {
    "root_strategy_decision": FIXTURE_ROOT_STRATEGY_DECISION,
    "causal_trade_intent": FIXTURE_CAUSAL_TRADE_INTENT,
    "risk_decision": FIXTURE_RISK_DECISION,
    "order_submission_attempted": FIXTURE_ORDER_SUBMISSION_ATTEMPTED,
    "order_state_changed": FIXTURE_ORDER_STATE_CHANGED,
    "partial_fill": FIXTURE_PARTIAL_FILL,
    "recovery_state_changed": FIXTURE_RECOVERY_STATE_CHANGED,
}


def fixture_names() -> tuple[str, ...]:
    """All fixture names, sorted for deterministic iteration."""
    return tuple(sorted(FIXTURES))


def fixture(name: str) -> dict[str, Any]:
    """Return a deep copy of one fixture so tests cannot mutate the canon."""
    try:
        return json.loads(json.dumps(FIXTURES[name]))
    except KeyError as exc:  # pragma: no cover - defensive
        raise KeyError(f"Unknown fixture '{name}'. Available: {', '.join(fixture_names())}.") from exc


def fixture_json(name: str) -> str:
    """Return one fixture serialized as JSON text."""
    return json.dumps(fixture(name))
