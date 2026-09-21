"""Minimal typed V1 domain contracts (Phase 3).

These are **contracts only**. They do not send orders, approve risk, call Binance,
calculate fills, update balances, execute trades, persist anything, or start recovery.
They exist so that future OMS / Risk / Simulator / Execution / Recovery components can
agree on the wire shape of each pipeline boundary.

Monetary and quantity fields use :class:`decimal.Decimal` - never binary floats.
Finite state values use ``Literal`` so invalid states fail validation.
Exchange-specific details are intentionally left out; venue adapters can add them in
later versions or in their own payloads.

Stages covered (one contract each):

1. ``strategy.decision.recorded``  - AI/strategy decision
2. ``trade.intent.created``        - trade intent
3. ``risk.decision.recorded``      - risk decision
4. ``order.state.changed``         - order lifecycle state change
5. ``execution.fill.recorded``     - execution/fill record
6. ``recovery.state.changed``      - recovery/reconciliation state change
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .base import DomainEvent, EventPayload

__all__ = [
    "FillRecordPayload",
    "OrderStateChangePayload",
    "RECOVERY_EVENT_TYPE",
    "RISK_DECISION_EVENT_TYPE",
    "RecoveryState",
    "RecoveryStateChangePayload",
    "RiskDecisionPayload",
    "STRATEGY_DECISION_EVENT_TYPE",
    "TRADE_INTENT_EVENT_TYPE",
    "ORDER_STATE_EVENT_TYPE",
    "EXECUTION_FILL_EVENT_TYPE",
    "StrategyDecisionPayload",
    "TradeIntentPayload",
    "StrategyDecisionRecorded",
    "TradeIntentCreated",
    "RiskDecisionRecorded",
    "OrderStateChanged",
    "ExecutionFillRecorded",
    "RecoveryStateChanged",
    "V1_CONTRACTS",
]

STRATEGY_DECISION_EVENT_TYPE = "strategy.decision.recorded"
TRADE_INTENT_EVENT_TYPE = "trade.intent.created"
RISK_DECISION_EVENT_TYPE = "risk.decision.recorded"
ORDER_STATE_EVENT_TYPE = "order.state.changed"
EXECUTION_FILL_EVENT_TYPE = "execution.fill.recorded"
RECOVERY_EVENT_TYPE = "recovery.state.changed"

#: Order lifecycle states. This is a state *contract*, not a workflow engine.
OrderState = Literal[
    "created",
    "submitted",
    "accepted",
    "partially_filled",
    "filled",
    "canceled",
    "rejected",
    "expired",
]

#: Recovery/reconciliation states.
RecoveryState = Literal["detected", "reconciling", "resolved", "failed"]


# --- 1. Strategy / AI decision ---------------------------------------------

class StrategyDecisionPayload(EventPayload):
    """What the strategy layer decided. It carries no execution semantics."""

    action: Literal["buy", "sell", "hold"] = Field(description="Intended action, or hold.")
    confidence: Literal["low", "medium", "high"] = Field(description="Decision confidence.")
    rationale: str = Field(min_length=1, description="Human-readable reasoning summary.")


class StrategyDecisionRecorded(DomainEvent):
    """Root event: a strategy decision was recorded."""

    event_type: Literal["strategy.decision.recorded"] = STRATEGY_DECISION_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: StrategyDecisionPayload


# --- 2. Trade intent -------------------------------------------------------

class TradeIntentPayload(EventPayload):
    """A request to trade. It is an intent, not an order."""

    side: Literal["buy", "sell"] = Field(description="Direction of the intended trade.")
    quantity: Decimal = Field(gt=0, description="Base-asset quantity to trade.")
    order_type: Literal["market", "limit"] = Field(description="Intended order type.")
    limit_price: Decimal | None = Field(
        default=None, gt=0, description="Limit price; required for limit orders only."
    )
    rationale: str | None = Field(default=None, description="Optional intent rationale.")

    @model_validator(mode="after")
    def _validate_order_type_fields(self) -> "TradeIntentPayload":
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit_price is required when order_type is 'limit'.")
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("limit_price must be omitted when order_type is 'market'.")
        return self


class TradeIntentCreated(DomainEvent):
    """A trade intent was created for a symbol."""

    event_type: Literal["trade.intent.created"] = TRADE_INTENT_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: TradeIntentPayload


# --- 3. Risk decision ------------------------------------------------------

class RiskDecisionPayload(EventPayload):
    """The risk layer's verdict. Recording it does not approve any real trade."""

    approved: bool = Field(description="Whether the risk layer permitted the intent.")
    reason: str = Field(min_length=1, description="Why the decision was made.")
    approved_quantity: Decimal | None = Field(
        default=None, gt=0, description="Quantity permitted when approved; null when rejected."
    )

    @model_validator(mode="after")
    def _validate_quantity_consistency(self) -> "RiskDecisionPayload":
        if self.approved and self.approved_quantity is None:
            raise ValueError("approved_quantity is required when approved is True.")
        if not self.approved and self.approved_quantity is not None:
            raise ValueError("approved_quantity must be omitted when approved is False.")
        return self


class RiskDecisionRecorded(DomainEvent):
    """A risk decision was recorded for a trade intent."""

    event_type: Literal["risk.decision.recorded"] = RISK_DECISION_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: RiskDecisionPayload


# --- 4. Order lifecycle ----------------------------------------------------

class OrderStateChangePayload(EventPayload):
    """A transition of an order's lifecycle state."""

    state: OrderState = Field(description="New lifecycle state of the order.")
    reason: str | None = Field(
        default=None, description="Required for terminal failure states."
    )

    @model_validator(mode="after")
    def _require_reason_for_terminal_failures(self) -> "OrderStateChangePayload":
        if self.state in {"rejected", "canceled", "expired"} and not (self.reason or "").strip():
            raise ValueError(f"reason is required when state is '{self.state}'.")
        return self


class OrderStateChanged(DomainEvent):
    """An order changed lifecycle state (the envelope carries ``order_id``)."""

    event_type: Literal["order.state.changed"] = ORDER_STATE_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: OrderStateChangePayload


# --- 5. Execution / fill ---------------------------------------------------

class FillRecordPayload(EventPayload):
    """A recorded fill. Recording a fill does not calculate or mutate balances."""

    quantity: Decimal = Field(gt=0, description="Filled base-asset quantity.")
    price: Decimal = Field(gt=0, description="Fill price.")
    fee: Decimal = Field(default=Decimal("0"), ge=0, description="Fee charged for this fill.")
    fee_asset: str | None = Field(default=None, description="Asset the fee was charged in.")
    remaining_quantity: Decimal | None = Field(
        default=None,
        ge=0,
        description="Quantity still open after this fill; 0 means the order is complete.",
    )
    liquidity: Literal["maker", "taker"] | None = Field(
        default=None, description="Liquidity role of this fill when known."
    )


class ExecutionFillRecorded(DomainEvent):
    """An execution fill was recorded for an order."""

    event_type: Literal["execution.fill.recorded"] = EXECUTION_FILL_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: FillRecordPayload


# --- 6. Recovery / reconciliation -----------------------------------------

class RecoveryStateChangePayload(EventPayload):
    """A recovery/reconciliation state change. No recovery logic runs in Phase 3."""

    state: RecoveryState = Field(description="Recovery state after the change.")
    reason: str = Field(min_length=1, description="Why recovery moved to this state.")
    affected_order_ids: list[UUID] = Field(
        default_factory=list, description="Orders involved in this recovery state change."
    )


class RecoveryStateChanged(DomainEvent):
    """A recovery/reconciliation state change was recorded."""

    event_type: Literal["recovery.state.changed"] = RECOVERY_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: RecoveryStateChangePayload


#: Every V1 contract known to this build, in pipeline order.
V1_CONTRACTS: tuple[type[DomainEvent], ...] = (
    StrategyDecisionRecorded,
    TradeIntentCreated,
    RiskDecisionRecorded,
    OrderStateChanged,
    ExecutionFillRecorded,
    RecoveryStateChanged,
)
