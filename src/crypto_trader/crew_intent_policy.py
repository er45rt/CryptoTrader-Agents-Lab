"""Opt-in, deterministic Crew advisory -> ProductionIntent policy (offline trial only).

Inputs MUST come from the trusted host, independently of CrewAI's plan. Neither this
module nor its caller is authorized to place a trade: it has no I/O, API, market
fetching, account discovery, order submission, or broker credentials. Production
Validity, Hard Risk, admission and venue fencing remain separate mandatory gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Callable
from uuid import uuid4

from crypto_trader.execution import ExecutionOrderType, ExecutionSide
from crypto_trader.llm.schemas import TradingPlan
from crypto_trader.production.errors import ProductionStateError
from crypto_trader.production.runtime import ProductionIntent
from crypto_trader.risk import RiskLimits

__all__ = [
    "ApprovedIntentConfig", "VerifiedMarketQuote", "HostDecisionTimes",
    "InstrumentFilters", "make_policy",
]


@dataclass(frozen=True)
class ApprovedIntentConfig:
    """Host-approved QUOTE-currency budget and bounded age/validity; no defaults."""

    venue: str
    symbol: str
    strategy_id: str
    approved_notional_quote: Decimal
    max_snapshot_age: timedelta
    intent_valid_for: timedelta


@dataclass(frozen=True)
class VerifiedMarketQuote:
    """Host-verified, symbol/venue-pinned bid/ask and provenance reference."""

    venue: str
    symbol: str
    reference_id: str
    event_ts: datetime
    received_at: datetime
    best_bid: Decimal
    best_ask: Decimal


@dataclass(frozen=True)
class HostDecisionTimes:
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True)
class InstrumentFilters:
    """Exchange filter values verified externally by the host; never model text."""

    venue: str
    symbol: str
    tick_size: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional_quote: Decimal


def _positive_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ProductionStateError(f"{name} requires a finite positive Decimal.")
    return value


def _aware(value: object, name: str) -> datetime:
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ProductionStateError(f"{name} time must be timezone-aware.")
    return value


def _seconds(value: object, name: str) -> timedelta:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ProductionStateError(f"{name} must be a positive bounded duration.")
    return value


def _plan_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (float, int, Decimal)):
        raise ProductionStateError(f"plan {name} is not a number.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProductionStateError(f"plan {name} is not decimal-convertible.") from exc
    if not result.is_finite() or result <= 0:
        raise ProductionStateError(f"plan {name} is not finite and positive.")
    return result


def make_policy(
    *, config: ApprovedIntentConfig, market_quote: VerifiedMarketQuote,
    host_times: HostDecisionTimes, instrument_filters: InstrumentFilters,
    risk_limits: RiskLimits,
) -> Callable[[TradingPlan], ProductionIntent]:
    """Freeze host-provided facts and return a deterministic, no-submission policy.

    No monetary amount or venue comes from the LLM or a hidden default.
    Validity and portfolio/equity risk MUST still be rechecked at admission.
    """

    def policy(plan: TradingPlan) -> ProductionIntent:
        if (not isinstance(config, ApprovedIntentConfig)
                or not isinstance(market_quote, VerifiedMarketQuote)
                or not isinstance(host_times, HostDecisionTimes)
                or not isinstance(instrument_filters, InstrumentFilters)
                or not isinstance(risk_limits, RiskLimits)):
            raise ProductionStateError("host policy requires explicit approved input types.")
        if not isinstance(plan, TradingPlan):
            raise ProductionStateError("policy requires a validated TradingPlan.")
        if (not isinstance(config.symbol, str) or not config.symbol
                or not isinstance(plan.symbol, str) or plan.symbol != config.symbol
                or market_quote.symbol != config.symbol
                or instrument_filters.symbol != config.symbol):
            raise ProductionStateError("symbol mismatch in deterministic host policy.")
        if (not isinstance(config.venue, str) or not config.venue
                or market_quote.venue != config.venue
                or instrument_filters.venue != config.venue):
            raise ProductionStateError("venue mismatch in deterministic host policy.")
        if not isinstance(config.strategy_id, str) or not config.strategy_id:
            raise ProductionStateError("strategy identity missing.")
        budget = _positive_decimal(config.approved_notional_quote, "approved budget")
        max_age = _seconds(config.max_snapshot_age, "snapshot maximum age")
        valid_for = _seconds(config.intent_valid_for, "intent validity")
        if risk_limits.trading_halted or risk_limits.reduce_only_mode:
            raise ProductionStateError("risk posture disallows fresh Crew intents.")
        static_caps = (
            risk_limits.max_order_notional_quote,
            risk_limits.max_symbol_exposure_quote,
            risk_limits.max_gross_exposure_quote,
            risk_limits.max_net_exposure_quote,
            risk_limits.max_strategy_exposure_quote,
        )
        if any(budget > cap for cap in static_caps):
            raise ProductionStateError("approved budget exceeds configured risk ceiling.")
        for name in ("tick_size", "quantity_step", "minimum_quantity", "minimum_notional_quote"):
            _positive_decimal(getattr(instrument_filters, name), f"instrument filter {name}")

        started = _aware(host_times.started_at, "decision start")
        completed = _aware(host_times.completed_at, "decision completion")
        event = _aware(market_quote.event_ts, "market event")
        received = _aware(market_quote.received_at, "market receipt")
        if started > completed:
            raise ProductionStateError("decision time order is invalid.")
        if event > completed or received > completed or event > received:
            raise ProductionStateError("market timestamp is in the future or out of order.")
        if completed - event > max_age:
            raise ProductionStateError("market snapshot is stale.")
        if not isinstance(market_quote.reference_id, str) or not market_quote.reference_id.strip():
            raise ProductionStateError("market reference identity missing.")

        bid = _positive_decimal(market_quote.best_bid, "market quote bid")
        ask = _positive_decimal(market_quote.best_ask, "market quote ask")
        if bid > ask:
            raise ProductionStateError("market quote bid exceeds ask.")
        if plan.bias not in ("bullish", "bearish") or plan.confidence == "low":
            raise ProductionStateError("plan is neutral or low-confidence.")
        low = _plan_decimal(plan.entry_low, "entry_low")
        high = _plan_decimal(plan.entry_high, "entry_high")
        stop = _plan_decimal(plan.stop_loss, "stop_loss")
        if low > high:
            raise ProductionStateError("plan entry interval is reversed.")
        if ((plan.bias == "bullish" and stop >= low)
                or (plan.bias == "bearish" and stop <= high)):
            raise ProductionStateError("plan stop is not on the protective side of entry.")

        side = ExecutionSide.BUY if plan.bias == "bullish" else ExecutionSide.SELL
        raw_price = ask if side is ExecutionSide.BUY else bid
        tick = instrument_filters.tick_size
        # BUY floors the ask and SELL ceils the bid; never widen a proposed
        # limit through rounding, and reject any result outside the plan zone.
        rounding = ROUND_FLOOR if side is ExecutionSide.BUY else ROUND_CEILING
        try:
            price = (raw_price / tick).to_integral_value(rounding=rounding) * tick
            step = instrument_filters.quantity_step
            quantity = (budget / price / step).to_integral_value(rounding=ROUND_FLOOR) * step
        except (InvalidOperation, ZeroDivisionError, OverflowError) as exc:
            raise ProductionStateError("instrument filter calculation is not executable.") from exc
        if price <= 0 or not low <= price <= high:
            raise ProductionStateError("verified market price is outside Crew entry zone.")
        notional = quantity * price
        if quantity < instrument_filters.minimum_quantity:
            raise ProductionStateError("quantity below instrument minimum.")
        if notional < instrument_filters.minimum_notional_quote:
            raise ProductionStateError("notional below instrument minimum.")
        if notional > budget:
            raise ProductionStateError("rounded notional exceeded approved budget.")

        deadline = min(completed + valid_for, event + max_age)
        if deadline <= completed:
            raise ProductionStateError("intent validity expired against verified market age.")
        return ProductionIntent(
            correlation_id=uuid4(), symbol=config.symbol, side=side,
            order_type=ExecutionOrderType.LIMIT, quantity=quantity,
            strategy_id=config.strategy_id, decision_started_at=started,
            decision_completed_at=completed, market_event_ts=event,
            reference_price=raw_price, market_reference=market_quote.reference_id,
            limit_price=price, valid_until=deadline, decision_scope="crew.host_policy",
        )

    return policy
