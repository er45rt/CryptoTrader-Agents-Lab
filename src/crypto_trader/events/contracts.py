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

Additive contracts added by later phases (existing schemas unchanged):

7. ``validity.disposition.recorded`` (Phase 6) - deterministic validity/staleness verdict
8. ``risk.evaluation.recorded``      (Phase 7) - deterministic Hard Risk rule audit
9. ``safety.state.changed``          (Phase 9) - operational safety-mode transition
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .base import DomainEvent, EventPayload

__all__ = [
    "FillRecordPayload",
    "OrderStateChangePayload",
    "RECOVERY_EVENT_TYPE",
    "RISK_EVALUATION_EVENT_TYPE",
    "RISK_DECISION_EVENT_TYPE",
    "RecoveryState",
    "RecoveryStateChangePayload",
    "RiskDecisionPayload",
    "STRATEGY_DECISION_EVENT_TYPE",
    "TRADE_INTENT_EVENT_TYPE",
    "ORDER_STATE_EVENT_TYPE",
    "EXECUTION_FILL_EVENT_TYPE",
    "VALIDITY_EVENT_TYPE",
    "StrategyDecisionPayload",
    "TradeIntentPayload",
    "StrategyDecisionRecorded",
    "TradeIntentCreated",
    "RiskDecisionRecorded",
    "OrderStateChanged",
    "ExecutionFillRecorded",
    "RecoveryStateChanged",
    "V1_CONTRACTS",
    "ValidityDisposition",
    "ValidityDispositionPayload",
    "ValidityDispositionRecorded",
    "RiskEvaluationPayload",
    "RiskEvaluationRecorded",
    "RiskRuleResultPayload",
    "RiskRuleUnit",
    "SAFETY_EVENT_TYPE",
    "SafetyMode",
    "SafetyStateChangePayload",
    "SafetyStateChanged",
    "SafetyTrigger",
]

STRATEGY_DECISION_EVENT_TYPE = "strategy.decision.recorded"
TRADE_INTENT_EVENT_TYPE = "trade.intent.created"
RISK_DECISION_EVENT_TYPE = "risk.decision.recorded"
ORDER_STATE_EVENT_TYPE = "order.state.changed"
EXECUTION_FILL_EVENT_TYPE = "execution.fill.recorded"
RECOVERY_EVENT_TYPE = "recovery.state.changed"
#: Phase 6 additive contract: the deterministic validity/staleness disposition of an intent.
VALIDITY_EVENT_TYPE = "validity.disposition.recorded"
#: Phase 7 additive contract: the full deterministic Hard Risk rule audit for an intent.
RISK_EVALUATION_EVENT_TYPE = "risk.evaluation.recorded"
#: Phase 9 additive contract: a deterministic operational safety-mode transition.
SAFETY_EVENT_TYPE = "safety.state.changed"

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

#: Validity dispositions (Phase 6). Precedence is documented on the validity gate.
ValidityDisposition = Literal[
    "accepted",
    "expired",
    "stale",
    "market_moved",
    "superseded",
    "backpressure_rejected",
]

#: Operational safety modes (Phase 9). Ordered from least to most restrictive below.
SafetyMode = Literal[
    "normal",
    "reduce_only",
    "halt_new_risk",
    "cancel_open",
    "degraded",
    "recovery_required",
]

#: What caused a safety-mode transition.
SafetyTrigger = Literal["operator", "risk", "recovery", "break_glass", "system"]


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


# --- 7. Decision validity / staleness (Phase 6, additive) ------------------

class ValidityDispositionPayload(EventPayload):
    """The deterministic validity verdict for one trade intent.

    Freshness is measured from the ORIGINAL market observation (``market_event_ts``); it never
    resets when the model finishes or the intent is created. This is an additive contract and
    does not overload ``risk.decision.recorded``.
    """

    disposition: ValidityDisposition = Field(description="Validity verdict for this intent.")
    reason_code: str = Field(min_length=1, description="Stable machine-readable reason.")
    intent_id: UUID = Field(description="Intent this decision applies to.")
    policy_id: str = Field(min_length=1, description="Identity of the explicit validity policy used.")
    decision_age_seconds: float = Field(
        ge=0, description="Decision age measured from the original market observation."
    )
    market_event_ts: datetime = Field(description="Timestamp of the original market observation.")
    decision_completed_at: datetime = Field(
        description="When the decision was produced; informational, never resets freshness."
    )
    market_move_bps: float | None = Field(
        default=None, description="Absolute reference-market move in basis points, when measured."
    )
    notes: str | None = Field(default=None, description="Optional human-readable context.")

    @field_validator("market_event_ts", "decision_completed_at")
    @classmethod
    def _require_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("validity timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc)

    @field_validator("decision_age_seconds")
    @classmethod
    def _finite_age(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("decision_age_seconds must be finite.")
        return value

    @field_validator("market_move_bps")
    @classmethod
    def _finite_move(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("market_move_bps must be finite when present.")
        return value


class ValidityDispositionRecorded(DomainEvent):
    """A deterministic validity/staleness decision was recorded for an intent."""

    event_type: Literal["validity.disposition.recorded"] = VALIDITY_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: ValidityDispositionPayload


# --- 8. Deterministic Hard Risk evaluation (Phase 7, additive) --------------

#: Explicit units used by risk rule results. No implicit conversions are performed anywhere.
RiskRuleUnit = Literal["quote_currency", "count", "percent"]


class RiskRuleResultPayload(EventPayload):
    """One deterministic risk rule outcome."""

    rule_id: str = Field(min_length=1, description="Stable rule identifier.")
    allowed: bool = Field(description="Whether the rule permitted the intent.")
    reason_code: str = Field(min_length=1, description="Deterministic reason code for the outcome.")
    actual: Decimal = Field(description="Measured value in the rule's declared unit.")
    limit: Decimal = Field(description="Configured limit in the rule's declared unit.")
    unit: RiskRuleUnit = Field(description="Explicit unit of 'actual' and 'limit'.")


class RiskEvaluationPayload(EventPayload):
    """The complete deterministic Hard Risk audit for one intent.

    Units are explicit: quote-currency amounts are Decimals in the quote currency (for example
    USDT), percentages are Decimals expressed as percent (0-100), counts are integers.
    """

    limits_version: str = Field(min_length=1, description="Identity/version of the rule set used.")
    intent_id: UUID = Field(description="Intent this evaluation applies to.")
    symbol: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    quantity: Decimal = Field(gt=0, description="Requested base-asset quantity.")
    price: Decimal = Field(gt=0, description="Reference/limit price in quote currency.")
    notional: Decimal = Field(gt=0, description="quantity * price, in quote currency.")
    decision: Literal["allowed", "rejected"]
    reason_codes: list[str] = Field(min_length=1, description="Deterministic reasons for the decision.")
    rule_results: list[RiskRuleResultPayload] = Field(
        min_length=1, description="Per-rule results, in evaluation order."
    )
    reduce_only_mode: bool = Field(default=False, description="Whether reduce-only mode was active.")
    risk_actions: list[Literal["halt_new_risk"]] = Field(
        default_factory=list,
        description="Deterministic safety actions requested by this evaluation (never liquidation).",
    )

    @field_validator("reason_codes")
    @classmethod
    def _non_empty_reasons(cls, value: list[str]) -> list[str]:
        if not value or any(not reason for reason in value):
            raise ValueError("reason_codes must contain at least one non-empty code.")
        return value


class RiskEvaluationRecorded(DomainEvent):
    """A deterministic Hard Risk evaluation was recorded for an intent."""

    event_type: Literal["risk.evaluation.recorded"] = RISK_EVALUATION_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: RiskEvaluationPayload


# --- 9. Operational safety state (Phase 9, additive) -----------------------

class SafetyStateChangePayload(EventPayload):
    """A deterministic operational safety-mode transition.

    The payload records the mode and why it changed. Derived policy (whether new risk is
    allowed, whether cancels are required) is intentionally NOT embedded here: it is a pure
    function of ``mode`` and lives in ``crypto_trader.safety``, so the record cannot drift from
    the policy.
    """

    mode: SafetyMode = Field(description="Safety mode after the transition.")
    previous_mode: SafetyMode = Field(description="Safety mode before the transition.")
    reason_code: str = Field(min_length=1, description="Stable machine-readable reason.")
    reason: str = Field(min_length=1, description="Human-readable reason for the transition.")
    trigger: SafetyTrigger = Field(description="What caused the transition.")
    kill_switch_engaged: bool = Field(
        default=False, description="Whether the kill switch is engaged after this transition."
    )


class SafetyStateChanged(DomainEvent):
    """An operational safety-mode transition was recorded."""

    event_type: Literal["safety.state.changed"] = SAFETY_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: SafetyStateChangePayload


#: Every V1 contract known to this build, in pipeline order.
V1_CONTRACTS: tuple[type[DomainEvent], ...] = (
    StrategyDecisionRecorded,
    TradeIntentCreated,
    RiskDecisionRecorded,
    OrderStateChanged,
    ExecutionFillRecorded,
    RecoveryStateChanged,
    ValidityDispositionRecorded,
    RiskEvaluationRecorded,
    SafetyStateChanged,
)
