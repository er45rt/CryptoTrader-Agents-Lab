"""Offline deterministic Crew policy tests. No venue, credentials, network or POST."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from crypto_trader.crew_intent_policy import (
    ApprovedIntentConfig,
    HostDecisionTimes,
    InstrumentFilters,
    VerifiedMarketQuote,
    make_policy,
)
from crypto_trader.execution import ExecutionSide
from crypto_trader.llm.schemas import TradingPlan, parse_trading_plan
from crypto_trader.production.errors import ProductionStateError
from crypto_trader.risk import RiskLimits
from crypto_trader.tests.test_r03a1b_crew_gateway import plan
from crypto_trader.tests.test_r03a1b_origin_binding import NOW


@pytest.fixture
def inputs():
    limits = RiskLimits(
        limits_version="offline", max_order_notional_quote=Decimal("500"),
        max_symbol_exposure_quote=Decimal("600"), max_gross_exposure_quote=Decimal("700"),
        max_net_exposure_quote=Decimal("700"), max_strategy_exposure_quote=Decimal("500"),
        loss_limit_quote=Decimal("300"), max_drawdown_pct=Decimal("10"),
        concentration_limit_pct=Decimal("50"), max_open_orders=2, max_open_positions=2,
    )
    config = ApprovedIntentConfig(
        venue="BINANCE", symbol="BTCUSDT", strategy_id="offline.r03",
        approved_notional_quote=Decimal("270"), max_snapshot_age=timedelta(seconds=8),
        intent_valid_for=timedelta(seconds=5),
    )
    quote = VerifiedMarketQuote(
        venue="BINANCE", symbol="BTCUSDT", reference_id="offline-l2-seq-15",
        event_ts=NOW - timedelta(seconds=2), received_at=NOW - timedelta(seconds=1),
        best_bid=Decimal("26999"), best_ask=Decimal("27000"),
    )
    times = HostDecisionTimes(started_at=NOW - timedelta(seconds=1),
                              completed_at=NOW)
    filters = InstrumentFilters(
        venue="BINANCE", symbol="BTCUSDT", tick_size=Decimal("0.01"),
        quantity_step=Decimal("0.0001"), minimum_quantity=Decimal("0.0001"),
        minimum_notional_quote=Decimal("5"),
    )
    return dict(config=config, market_quote=quote, host_times=times,
                instrument_filters=filters, risk_limits=limits)


def test_approved_quote_budget_and_verified_ask_determine_buy_independently_of_ai_size(inputs):
    policy = make_policy(**inputs)
    normal = parse_trading_plan(plan())
    huge_advice = normal.model_copy(update={"position_size_pct": 100.0})
    first, second = policy(normal), policy(huge_advice)
    assert first.quantity == second.quantity == Decimal("0.0100")
    assert first.limit_price == second.limit_price == Decimal("27000.00")
    assert first.reference_price == Decimal("27000")
    assert first.side is ExecutionSide.BUY
    assert first.market_reference == "offline-l2-seq-15"
    assert first.valid_until <= inputs["market_quote"].event_ts + timedelta(seconds=8)
    assert first.intent_id != second.intent_id
    assert first.quantity * first.limit_price <= inputs["config"].approved_notional_quote


def test_directional_sell_uses_verified_bid_and_never_spends_ai_percent(inputs):
    bearish = TradingPlan.model_validate({
        **parse_trading_plan(plan()).model_dump(),
        "bias": "bearish", "stop_loss": 27500.0,
    })
    issued = make_policy(**inputs)(bearish)
    assert issued.side is ExecutionSide.SELL
    assert issued.limit_price == Decimal("26999.00")
    assert issued.quantity == Decimal("0.0100")


@pytest.mark.parametrize("key,updated,part", [
    ("config", {"symbol": "ETHUSDT"}, "symbol"),
    ("config", {"venue": "OTHER"}, "venue"),
    ("config", {"approved_notional_quote": Decimal("0")}, "budget"),
    ("config", {"approved_notional_quote": Decimal("NaN")}, "budget"),
    ("config", {"approved_notional_quote": Decimal("600")}, "risk"),
    ("config", {"max_snapshot_age": timedelta(seconds=0)}, "snapshot"),
    ("config", {"intent_valid_for": timedelta(seconds=0)}, "validity"),
    ("market_quote", {"symbol": "ETHUSDT"}, "symbol"),
    ("market_quote", {"venue": "OTHER"}, "venue"),
    ("market_quote", {"reference_id": ""}, "reference"),
    ("market_quote", {"best_ask": Decimal("NaN")}, "quote"),
    ("market_quote", {"best_bid": Decimal("27100")}, "quote"),
    ("market_quote", {"event_ts": NOW - timedelta(seconds=15)}, "stale"),
    ("market_quote", {"event_ts": NOW + timedelta(seconds=1)}, "future"),
    ("market_quote", {"received_at": NOW + timedelta(seconds=1)}, "future"),
    ("host_times", {"started_at": NOW + timedelta(seconds=1)}, "time"),
    ("instrument_filters", {"symbol": "ETHUSDT"}, "symbol"),
    ("instrument_filters", {"venue": "OTHER"}, "venue"),
    ("instrument_filters", {"tick_size": Decimal("0")}, "filter"),
    ("instrument_filters", {"quantity_step": Decimal("NaN")}, "filter"),
    ("instrument_filters", {"minimum_notional_quote": Decimal("500")}, "minimum"),
])
def test_missing_or_untrusted_authority_fails_closed(inputs, key, updated, part):
    broken = dict(inputs)
    broken[key] = replace(inputs[key], **updated)
    with pytest.raises(ProductionStateError, match=part):
        make_policy(**broken)(parse_trading_plan(plan()))


def test_price_must_stay_inside_advisory_entry_zone_even_after_tick_rounding(inputs):
    narrow = parse_trading_plan(plan()).model_copy(update={
        "entry_low": 27000.001, "entry_high": 27000.05,
    })
    with pytest.raises(ProductionStateError, match="entry"):
        make_policy(**inputs)(narrow)


def test_invalid_directional_stop_and_unactionable_plan_fail_closed(inputs):
    original = parse_trading_plan(plan())
    for changed in (
        original.model_copy(update={"bias": "neutral"}),
        original.model_copy(update={"confidence": "low"}),
        original.model_copy(update={"stop_loss": 29000.0}),
    ):
        with pytest.raises(ProductionStateError):
            make_policy(**inputs)(changed)


def test_non_trading_policy_is_factory_only_and_does_not_call_venue(inputs):
    policy = make_policy(**inputs)
    assert callable(policy)
    assert policy(parse_trading_plan(plan())).origin_decision_event_id is None
