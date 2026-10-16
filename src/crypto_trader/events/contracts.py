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
10. ``safety.operator_command.recorded`` (Phase 13) - one auditable, idempotent operator
    break-glass command and its deterministic disposition
11. ``stream.lifecycle.recorded``      (Phase 15) - one deterministic market-data stream
    lifecycle transition (disconnect/reconnect/resubscribe/resync), secret-free
12. ``order.submission.attempted``     (Phase 15 durable ambiguity) - one durable fact that a
    venue submission was attempted, recorded BEFORE the external transport call, carrying the
    stable venue-facing client order identity and the immutable economic anchors
13. ``venue.trade.recorded``           (G4 production-ledger reader compatibility) - immutable
    venue trade metadata linked to one existing execution.fill.recorded event
14. ``account.equity.recorded``        (G5b canonical risk-state prerequisite) - durable account
    equity baseline/rebaseline roots and observed high-water-mark updates
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
    "AttemptTimeInForce",
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
    "ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE",
    "EXECUTION_FILL_EVENT_TYPE",
    "VENUE_TRADE_EVENT_TYPE",
    "ACCOUNT_EQUITY_EVENT_TYPE",
    "VALIDITY_EVENT_TYPE",
    "StrategyDecisionPayload",
    "TradeIntentPayload",
    "StrategyDecisionRecorded",
    "TradeIntentCreated",
    "RiskDecisionRecorded",
    "OrderStateChanged",
    "OrderSubmissionAttemptPayload",
    "OrderSubmissionAttempted",
    "ExecutionFillRecorded",
    "VenueTradeRecordPayload",
    "VenueTradeRecorded",
    "AccountEquityRecordPayload",
    "AccountEquityRecorded",
    "RecoveryStateChanged",
    "V1_CONTRACTS",
    "ValidityDisposition",
    "ValidityDispositionPayload",
    "ValidityDispositionRecorded",
    "RiskEvaluationPayload",
    "RiskEvaluationRecorded",
    "RiskEvaluationPayloadV2",
    "RiskEvaluationRecordedV2",
    "RiskRuleResultPayloadV2",
    "V2_CONTRACTS",
    "RiskRuleResultPayload",
    "RiskRuleUnit",
    "SAFETY_EVENT_TYPE",
    "SafetyMode",
    "SafetyStateChangePayload",
    "SafetyStateChanged",
    "SafetyTrigger",
    "OPERATOR_COMMAND_EVENT_TYPE",
    "OperatorCommandRecordPayload",
    "OperatorCommandRecorded",
    "OperatorCommandRecordPayloadV2",
    "OperatorCommandRecordedV2",
    "OperatorCommandType",
    "STREAM_LIFECYCLE_EVENT_TYPE",
    "StreamLifecyclePayload",
    "StreamLifecycleRecorded",
    "StreamLifecycleState",
    "StreamReasonCode",
    "StreamStepResult",
]

STRATEGY_DECISION_EVENT_TYPE = "strategy.decision.recorded"
TRADE_INTENT_EVENT_TYPE = "trade.intent.created"
RISK_DECISION_EVENT_TYPE = "risk.decision.recorded"
ORDER_STATE_EVENT_TYPE = "order.state.changed"
#: Phase 15 durability additive contract: the durable pre-send submission attempt fact.
ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE = "order.submission.attempted"
EXECUTION_FILL_EVENT_TYPE = "execution.fill.recorded"
#: G4 additive contract: immutable venue trade metadata linked to one fill event.
VENUE_TRADE_EVENT_TYPE = "venue.trade.recorded"
#: G5b additive contract: durable account-equity baseline / peak chain.
ACCOUNT_EQUITY_EVENT_TYPE = "account.equity.recorded"
RECOVERY_EVENT_TYPE = "recovery.state.changed"
#: Phase 6 additive contract: the deterministic validity/staleness disposition of an intent.
VALIDITY_EVENT_TYPE = "validity.disposition.recorded"
#: Phase 7 additive contract: the full deterministic Hard Risk rule audit for an intent.
RISK_EVALUATION_EVENT_TYPE = "risk.evaluation.recorded"
#: Phase 9 additive contract: a deterministic operational safety-mode transition.
SAFETY_EVENT_TYPE = "safety.state.changed"
#: Phase 13 additive contract: one operator break-glass command and its recorded disposition.
OPERATOR_COMMAND_EVENT_TYPE = "safety.operator_command.recorded"
#: Phase 15 additive contract: one deterministic market-data stream lifecycle transition.
STREAM_LIFECYCLE_EVENT_TYPE = "stream.lifecycle.recorded"

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

#: The operational break-glass command vocabulary (Phase 13). The first five are the break-glass
#: actions this phase must support; the last three are the mandatory lifecycle commands that gate
#: any return to ``normal`` (reconciliation, explicit operator acknowledgement, restore).
OperatorCommandType = Literal[
    "halt_new_risk",
    "cancel_open",
    "reduce_only",
    "enter_degraded",
    "force_recovery_required",
    "reconcile",
    "acknowledge",
    "restore_normal",
]


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


# --- 4b. Durable submission attempt (Phase 15 durability, additive) --------

#: Time-in-force vocabulary of the durable attempt record. Lowercase, engine-independent; the
#: venue adapters translate it to the venue's own uppercase form at the transport boundary.
AttemptTimeInForce = Literal["gtc", "ioc", "fok"]


class OrderSubmissionAttemptPayload(EventPayload):
    """The durable, pre-send submission attempt fact for one order.

    This record is written BEFORE the external transport call. It carries exactly the facts a
    restart needs to correlate an unresolved attempt with the venue and to validate that whatever
    the venue reports later is the same economic order:

    * ``venue_reference`` - the stable client order identity sent to the venue (the idempotency
      key and the key an authoritative per-order lookup uses after a restart);
    * ``intent_id`` - the originating intent identity, which is NOT recoverable from the client
      order identity alone;
    * the immutable economic anchors ``side`` / ``order_type`` / ``quantity`` / ``limit_price`` /
      ``time_in_force``;
    * ``venue_producer`` - an explicit provenance anchor naming the producer whose lifecycle and
      fill records for this order are authoritative venue evidence. Authority is read from this
      durable fact, never inferred from timestamps or from a producer-string convention.

    The event envelope carries ``order_id`` (the deterministic system order identity), ``symbol``,
    ``correlation_id`` and ``occurred_at``. ``sequence_number`` is deliberately left unset: no
    recovery decision may depend on it.
    """

    venue_reference: str = Field(
        min_length=1,
        max_length=128,
        description="Stable client order identity sent to the venue (idempotency key).",
    )
    intent_id: UUID = Field(description="Originating intent identity (not derivable from the key).")
    side: Literal["buy", "sell"] = Field(description="Direction of the attempted order.")
    order_type: Literal["market", "limit"] = Field(description="Attempted order type.")
    quantity: Decimal = Field(
        gt=0, description="Immutable original quantity (the Q anchor for quantity invariants)."
    )
    limit_price: Decimal | None = Field(
        default=None, gt=0, description="Immutable limit price; required for limit attempts only."
    )
    time_in_force: AttemptTimeInForce = Field(description="Time in force of the attempted order.")
    venue_producer: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Producer identifier whose lifecycle/fill records for this order are authoritative "
            "venue evidence (explicit provenance; never inferred from time)."
        ),
    )

    @model_validator(mode="after")
    def _validate_order_type_fields(self) -> "OrderSubmissionAttemptPayload":
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit_price is required when order_type is 'limit'.")
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("limit_price must be omitted when order_type is 'market'.")
        return self

    @field_validator("venue_reference", "venue_producer")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("durable attempt identity fields must not be blank.")
        return value


class OrderSubmissionAttempted(DomainEvent):
    """A submission attempt was durably recorded before the external transport call."""

    event_type: Literal["order.submission.attempted"] = ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: OrderSubmissionAttemptPayload


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


# --- 5b. Venue trade metadata (G4 production-ledger compatibility) -------

class VenueTradeRecordPayload(EventPayload):
    """Immutable venue trade metadata linked to one execution fill event."""

    venue_reference: str = Field(min_length=1, max_length=128)
    venue_order_id: str = Field(min_length=1, max_length=128)
    venue_trade_id: str = Field(min_length=1, max_length=128)
    fill_event_id: UUID
    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    quote_quantity: Decimal = Field(gt=0)
    commission: Decimal = Field(ge=0)
    commission_asset: str = Field(min_length=1, max_length=64)
    trade_time: datetime
    is_buyer: bool
    is_maker: bool

    @field_validator("venue_reference", "venue_order_id", "venue_trade_id", "commission_asset")
    @classmethod
    def _trade_identity_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("venue trade identity fields must not be blank.")
        return value

    @field_validator("venue_order_id", "venue_trade_id")
    @classmethod
    def _canonical_positive_numeric_id(cls, value: str) -> str:
        if (
            not value.isascii()
            or not value.isdigit()
            or value.startswith("0")
        ):
            raise ValueError("venue order/trade ids must be canonical positive ASCII integers.")
        return value

    @field_validator("trade_time")
    @classmethod
    def _trade_time_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("venue trade time must be timezone-aware.")
        return value.astimezone(timezone.utc)


class VenueTradeRecorded(DomainEvent):
    """One immutable venue trade linked to its execution.fill.recorded event."""

    event_type: Literal["venue.trade.recorded"] = VENUE_TRADE_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: VenueTradeRecordPayload

    @model_validator(mode="after")
    def _require_fill_linkage(self) -> "VenueTradeRecorded":
        if self.order_id is None:
            raise ValueError("venue trade records require an order_id.")
        if self.causation_id != self.payload.fill_event_id:
            raise ValueError("venue trade causation_id must equal fill_event_id.")
        if self.occurred_at != self.payload.trade_time:
            raise ValueError("venue trade occurred_at must equal the venue trade time.")
        return self


# --- 5c. Account equity baseline / peak chain (G5b) ----------------------

class AccountEquityRecordPayload(EventPayload):
    """Durable account-equity chain root or high-water-mark update."""

    kind: Literal["baseline", "peak", "rebaseline"]
    environment: str = Field(min_length=1, max_length=128)
    venue_account_id: str = Field(min_length=1, max_length=128)
    base_asset: str = Field(min_length=1, max_length=32)
    quote_asset: str = Field(min_length=1, max_length=32)
    base_balance: Decimal = Field(ge=0)
    quote_balance: Decimal = Field(ge=0)
    mark_price: Decimal = Field(gt=0)
    mark_reference: str = Field(min_length=1, max_length=256)
    mark_event_ts: datetime
    equity_quote: Decimal = Field(gt=0)
    peak_equity_quote: Decimal = Field(gt=0)
    baseline_event_id: UUID
    previous_event_id: UUID | None = None
    fence_term: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=512)

    @field_validator("environment", "mark_reference", "reason")
    @classmethod
    def _equity_text_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("account equity text fields must not be blank.")
        return value

    @field_validator("venue_account_id")
    @classmethod
    def _canonical_account_id(cls, value: str) -> str:
        if not value.isascii() or not value.isdigit() or value.startswith("0"):
            raise ValueError("venue_account_id must be a canonical positive ASCII integer.")
        return value

    @field_validator("base_asset", "quote_asset")
    @classmethod
    def _canonical_asset(cls, value: str) -> str:
        if not value.isascii() or value != value.upper() or not value.isalnum():
            raise ValueError("account equity assets must be uppercase ASCII alphanumeric codes.")
        return value

    @field_validator("mark_event_ts")
    @classmethod
    def _mark_time_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("mark_event_ts must be timezone-aware.")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _equity_invariants(self) -> "AccountEquityRecordPayload":
        if self.base_asset == self.quote_asset:
            raise ValueError("base_asset and quote_asset must differ.")
        expected_equity = self.base_balance * self.mark_price + self.quote_balance
        if self.equity_quote != expected_equity:
            raise ValueError("equity_quote must equal base_balance * mark_price + quote_balance.")
        if self.peak_equity_quote != self.equity_quote:
            raise ValueError("each equity-chain observation must record peak equal to observed equity.")
        if self.kind == "baseline" and self.previous_event_id is not None:
            raise ValueError("baseline cannot have a previous_event_id.")
        if self.kind in {"peak", "rebaseline"} and self.previous_event_id is None:
            raise ValueError("peak and rebaseline require previous_event_id.")
        return self


class AccountEquityRecorded(DomainEvent):
    """One durable account-equity baseline, rebaseline or peak observation."""

    event_type: Literal["account.equity.recorded"] = ACCOUNT_EQUITY_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: AccountEquityRecordPayload

    @model_validator(mode="after")
    def _require_equity_chain_identity(self) -> "AccountEquityRecorded":
        if self.symbol is None:
            raise ValueError("account equity records require symbol on the envelope.")
        if self.symbol != f"{self.payload.base_asset}{self.payload.quote_asset}":
            raise ValueError("account equity envelope symbol must match base_asset + quote_asset.")
        if self.payload.previous_event_id == self.event_id:
            raise ValueError("account equity previous_event_id cannot self-link to event_id.")
        if self.payload.mark_event_ts > self.occurred_at:
            raise ValueError("account equity mark_event_ts cannot be later than occurred_at.")
        if self.kind_is_root and self.payload.baseline_event_id != self.event_id:
            raise ValueError("baseline_event_id must equal event_id for a chain root.")
        if not self.kind_is_root and self.payload.baseline_event_id == self.event_id:
            raise ValueError("peak events cannot identify themselves as the baseline.")
        return self

    @property
    def kind_is_root(self) -> bool:
        return self.payload.kind in {"baseline", "rebaseline"}


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


class RiskRuleResultPayloadV2(RiskRuleResultPayload):
    """V2 adds base-asset quantities without reinterpreting any V1 unit."""

    unit: Literal["quote_currency", "count", "percent", "base_quantity"] = Field(
        description="Explicit economic unit of 'actual' and 'limit'."
    )


class RiskEvaluationPayloadV2(RiskEvaluationPayload):
    """V2 rule audit; all inherited fields retain their V1 meanings."""

    rule_results: list[RiskRuleResultPayloadV2] = Field(
        min_length=1, description="Per-rule results, in evaluation order."
    )


class RiskEvaluationRecordedV2(RiskEvaluationRecorded):
    """Explicit V2 audit. V1 stays registered and is never silently upgraded."""

    schema_version: Literal[2] = 2
    payload: RiskEvaluationPayloadV2


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


# --- 10. Operator break-glass command record (Phase 13, additive) -----------

class OperatorCommandRecordPayload(EventPayload):
    """One operational break-glass command and its deterministic disposition.

    The record is the operator-command audit trail: every break-glass request is journaled exactly
    once and re-delivering the same ``command_id`` produces no second effect (the journal is the
    idempotency ledger, keyed on ``command_id``).

    ``correlation_id`` and ``causation_id`` live on the Phase 3 envelope, exactly as for every other
    contract, so a command record carries its workflow trace and its cause without duplicating them
    in the payload. ``command_digest`` is the deterministic fingerprint of the command's *semantic*
    content (type, operator, reason, source): a re-delivery matches and is idempotent, while a
    different request reusing the same ``command_id`` is a conflict and is refused.

    The payload never contains credential material: the operator label is a name/label, never a
    token, and the command model rejects credential-shaped free text before it reaches the journal.
    """

    command_id: UUID = Field(description="Operator-supplied idempotency key of this command.")
    command_type: OperatorCommandType = Field(description="Requested operational action.")
    requested_at: datetime = Field(description="Timezone-aware request time, normalized to UTC.")
    operator_label: str = Field(min_length=1, description="Operator name or label (never a secret).")
    operator_identity: str | None = Field(
        default=None, description="Optional stronger operator identity; omit when unavailable."
    )
    reason: str = Field(min_length=1, description="Why the operator issued the command.")
    source: str = Field(min_length=1, description="Control-channel identifier that delivered it.")
    previous_safety_state: SafetyMode = Field(description="Safety mode before the command.")
    resulting_safety_state: SafetyMode = Field(description="Safety mode after the command.")
    disposition: Literal["accepted", "rejected"] = Field(
        description="Whether the command was applied or refused."
    )
    reason_code: str = Field(min_length=1, description="Deterministic machine-readable outcome code.")
    command_digest: str = Field(min_length=16, description="Fingerprint of the semantic content.")
    mode_path: list[str] = Field(
        min_length=1,
        description="Safety modes this command traversed, starting with the previous mode.",
    )
    venue_control: Literal[
        "not_requested", "confirmed", "ambiguous", "unavailable", "rejected"
    ] = Field(description="Venue-side control disposition for this command.")
    reconciliation_status: Literal[
        "not_required", "pending", "resolved", "failed"
    ] = Field(description="Reconciliation requirement standing after the command.")
    epoch_opened: bool = Field(description="Whether this command opened a break-glass epoch.")
    epoch_closed: bool = Field(description="Whether this command closed the break-glass epoch.")
    fence_term: int = Field(ge=0, description="Ownership fencing term after the command.")

    @field_validator("requested_at")
    @classmethod
    def _require_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware.")
        return value.astimezone(timezone.utc)

    @field_validator("operator_label", "reason", "source", "reason_code", "operator_identity")
    @classmethod
    def _non_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("operator command text fields must not be blank.")
        return value


class OperatorCommandRecorded(DomainEvent):
    """An operator break-glass command was recorded with its deterministic disposition."""

    event_type: Literal["safety.operator_command.recorded"] = OPERATOR_COMMAND_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: OperatorCommandRecordPayload


class OperatorCommandRecordPayloadV2(OperatorCommandRecordPayload):
    """V2 adds the order references the venue confirmed cancelled; V1 fields keep their meanings.

    A new process reconciling the epoch reads these references back from the journal, so the venue
    evidence it queries never depends on the memory of the process that cancelled.
    """

    cancelled_order_references: list[str] = Field(
        description="Client order references the venue confirmed cancelled, in venue order."
    )

    @field_validator("cancelled_order_references")
    @classmethod
    def _usable_references(cls, value: list[str]) -> list[str]:
        if any(not reference.strip() for reference in value):
            raise ValueError("cancelled order references must not be blank.")
        if len(set(value)) != len(value):
            raise ValueError("cancelled order references must be unique.")
        return value


class OperatorCommandRecordedV2(OperatorCommandRecorded):
    """Explicit V2 command record. V1 stays registered and is never silently upgraded."""

    schema_version: Literal[2] = 2
    payload: OperatorCommandRecordPayloadV2


# --- 11. Market-data stream lifecycle (Phase 15, additive) ------------------

#: The stream lifecycle vocabulary. ``ready`` is reachable only after a fresh synchronized
#: snapshot exists; every other state keeps new risk blocked.
StreamLifecycleState = Literal[
    "connected",
    "stale",
    "reconnecting",
    "resubscribing",
    "resyncing",
    "ready",
]

#: Deterministic stream reasons. The two disconnect reasons stay distinct: an explicit close is
#: never conflated with a socket that stays logically open and simply stops sending data.
StreamReasonCode = Literal[
    "CLEAN_CLOSE",
    "SILENT_TIMEOUT",
    "TRANSPORT_ERROR",
    "SUBSCRIPTION_FAILED",
    "RESYNC_FAILED",
    "RECONNECT_BUDGET_EXHAUSTED",
]

#: Outcome of one recovery step (resubscription / state resynchronization).
StreamStepResult = Literal["pending", "succeeded", "failed"]


class StreamLifecyclePayload(EventPayload):
    """One deterministic market-data stream lifecycle transition.

    The record is secret-free by construction: it carries identifiers, timestamps, outcome enums
    and counters only. Session identifiers are generated locally and never derived from a
    credential. No API key, authorization header, signed payload or credential material has a
    field here, and the module rejects credential-shaped text before it reaches the journal.
    """

    state: StreamLifecycleState = Field(description="Lifecycle state after the transition.")
    previous_state: StreamLifecycleState = Field(
        description="Lifecycle state before the transition."
    )
    reason_code: StreamReasonCode | None = Field(
        default=None, description="Deterministic reason when a failure drove the transition."
    )
    session_id: str | None = Field(
        default=None,
        max_length=128,
        description="Current stream session identity (locally generated; never a credential).",
    )
    previous_session_id: str | None = Field(
        default=None, max_length=128, description="Session identity that was replaced, if any."
    )
    disconnect_at: datetime | None = Field(
        default=None, description="When the current disconnect was detected."
    )
    reconnect_at: datetime | None = Field(
        default=None, description="When the current reconnect completed, when applicable."
    )
    resubscription_result: StreamStepResult | None = Field(
        default=None, description="Outcome of the resubscription step for this transition."
    )
    resync_result: StreamStepResult | None = Field(
        default=None, description="Outcome of the state-resynchronization step for this transition."
    )
    attempt: int = Field(ge=0, description="Recovery attempt number (0 before any retry).")
    delay_seconds: float = Field(
        ge=0.0, description="Deterministic backoff delay applied before this transition."
    )
    new_risk_permitted: bool = Field(
        description="Whether the state after this transition permits new risk (ready only)."
    )

    @field_validator("disconnect_at", "reconnect_at")
    @classmethod
    def _require_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stream lifecycle timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc)

    @field_validator("session_id", "previous_session_id")
    @classmethod
    def _non_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("stream session identifiers must not be blank.")
        return value


class StreamLifecycleRecorded(DomainEvent):
    """A deterministic market-data stream lifecycle transition was recorded."""

    event_type: Literal["stream.lifecycle.recorded"] = STREAM_LIFECYCLE_EVENT_TYPE
    schema_version: Literal[1] = 1
    payload: StreamLifecyclePayload


#: Every V1 contract known to this build, in pipeline order.
V1_CONTRACTS: tuple[type[DomainEvent], ...] = (
    StrategyDecisionRecorded,
    TradeIntentCreated,
    RiskDecisionRecorded,
    OrderStateChanged,
    OrderSubmissionAttempted,
    ExecutionFillRecorded,
    VenueTradeRecorded,
    AccountEquityRecorded,
    RecoveryStateChanged,
    ValidityDispositionRecorded,
    RiskEvaluationRecorded,
    SafetyStateChanged,
    OperatorCommandRecorded,
    StreamLifecycleRecorded,
)

V2_CONTRACTS: tuple[type[DomainEvent], ...] = (RiskEvaluationRecordedV2, OperatorCommandRecordedV2)
