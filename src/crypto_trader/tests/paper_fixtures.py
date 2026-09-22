"""Shared deterministic fixtures for the Phase 10 shadow/paper tests.

Not a test module (pytest does not collect it): it only builds deterministic inputs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from crypto_trader.paper import (
    DecisionSource,
    FrozenMarketDataFeed,
    MarketObservation,
    ScriptedDecisionSource,
    SessionConfig,
    StrategyDecision,
    TradingMode,
    TradingSession,
    load_session_config,
)
from crypto_trader.risk import RiskLimits, load_limits
from crypto_trader.safety import SafetyController
from crypto_trader.validity import DecisionValidityGate, load_policy
from crypto_trader.validity import (
    MarketObservation as ValidityMarketObservation,
    ReplayMarketStateProvider,
    ValidityRequest,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SESSION_ID = UUID("44444444-4444-4444-8444-444444444444")

LIMIT_CONFIG: dict[str, Any] = {
    "limits_version": "risk-v1",
    "max_order_notional_quote": "5000",
    "max_symbol_exposure_quote": "20000",
    "max_gross_exposure_quote": "50000",
    "max_net_exposure_quote": "30000",
    "max_strategy_exposure_quote": "40000",
    "loss_limit_quote": "1000",
    "max_drawdown_pct": "10",
    "concentration_limit_pct": "50",
    "max_open_orders": 5,
    "max_open_positions": 3,
}

VALIDITY_CONFIG: dict[str, Any] = {
    "policy_id": "validity-v1",
    "max_age_seconds": 30,
    "max_market_move_bps": "50",
}


def feed(*prices: str, start: datetime = BASE) -> FrozenMarketDataFeed:
    """A frozen public market-data feed with one observation per price."""
    entries = tuple(
        MarketObservation(
            symbol="ETHUSDT",
            price=Decimal(price),
            observed_at=start + timedelta(seconds=index),
            market_reference=f"public.kline.1m#{index}",
            volume=Decimal("10"),
        )
        for index, price in enumerate(prices)
    )
    return FrozenMarketDataFeed(entries=entries, feed_id="paper-fixture-v1")


def buy_decision(
    *,
    quantity: str = "1",
    limit_price: str = "2700.00",
    decided_at: datetime | None = None,
    rationale: str = "synthetic momentum signal",
) -> StrategyDecision:
    return StrategyDecision(
        action="buy",
        confidence="medium",
        rationale=rationale,
        quantity=Decimal(quantity),
        order_type="limit",
        limit_price=Decimal(limit_price),
        decided_at=decided_at,
    )


def sell_decision(*, quantity: str = "1", limit_price: str = "2700.00") -> StrategyDecision:
    return StrategyDecision(
        action="sell",
        confidence="medium",
        rationale="synthetic exit signal",
        quantity=Decimal(quantity),
        order_type="limit",
        limit_price=Decimal(limit_price),
    )


def hold_decision() -> StrategyDecision:
    return StrategyDecision(
        action="hold", confidence="low", rationale="no edge", quantity=Decimal("0")
    )


def scripted(*decisions: StrategyDecision, default: StrategyDecision | None = None) -> DecisionSource:
    return ScriptedDecisionSource(
        script={
            f"public.kline.1m#{index}": decision for index, decision in enumerate(decisions)
        },
        default=default,
    )


def config(
    mode: TradingMode = TradingMode.PAPER,
    *,
    shadow_submits: bool = False,
    **overrides: Any,
) -> SessionConfig:
    return load_session_config(
        {
            "mode": mode.value,
            "symbol": "ETHUSDT",
            "strategy_id": "strategy-paper-1",
            "session_id": SESSION_ID,
            "shadow_submits": shadow_submits,
            **overrides,
        }
    )


def limits(**overrides: Any) -> RiskLimits:
    return load_limits({**LIMIT_CONFIG, **overrides})


def validity_observation(entry: MarketObservation) -> ValidityMarketObservation:
    """Adapt a paper feed entry into the validity layer's observation shape."""
    return ValidityMarketObservation(
        symbol=entry.symbol,
        price=entry.price,
        market_event_ts=entry.observed_at,
        local_ingest_ts=entry.observed_at,
        market_reference=entry.market_reference,
    )


def validity_state(data_feed) -> ReplayMarketStateProvider:
    """A deterministic validity market-state provider over a frozen feed."""
    return ReplayMarketStateProvider(
        [validity_observation(entry) for entry in data_feed.observations()]
    )


def validity_request(
    *,
    decided_at: datetime | None = None,
    reference_price: str = "2700.00",
    market_reference: str = "public.kline.1m#0",
    scope: str = "strategy-paper-1",
    valid_until: datetime | None = None,
) -> ValidityRequest:
    """A validity request anchored at BASE (freshness is measured from the observation)."""
    completed = decided_at or BASE
    return ValidityRequest(
        correlation_id=SESSION_ID,
        symbol="ETHUSDT",
        decision_started_at=BASE,
        decision_completed_at=completed,
        market_event_ts=BASE,
        reference_price=Decimal(reference_price),
        market_reference=market_reference,
        decision_scope=scope,
        valid_until=valid_until,
    )


def session(
    journal,
    *,
    mode: TradingMode = TradingMode.PAPER,
    decisions: DecisionSource,
    data_feed,
    simulator=None,
    cfg: SessionConfig | None = None,
    safety: SafetyController | None = None,
    limits_override: RiskLimits | None = None,
) -> TradingSession:
    return TradingSession(
        config=cfg or config(mode),
        journal=journal,
        feed=data_feed,
        decisions=decisions,
        validity_gate=DecisionValidityGate(load_policy(VALIDITY_CONFIG)),
        limits=limits_override or limits(),
        safety=safety or SafetyController(correlation_id=SESSION_ID),
        simulator=simulator,
    )
