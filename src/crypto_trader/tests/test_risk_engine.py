"""Phase 7: deterministic Hard Risk engine tests (units, boundaries, determinism)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from crypto_trader.risk import (
    HardRiskEngine,
    PendingIntent,
    PendingIntentRegistry,
    RiskConfigError,
    RiskLimits,
    RiskRequest,
    RiskStateError,
    RiskStateSnapshot,
    Side,
    load_limits,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

LIMIT_CONFIG = {
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


def limits(**overrides) -> RiskLimits:
    return load_limits({**LIMIT_CONFIG, **overrides})


def state(**overrides) -> RiskStateSnapshot:
    fields = {
        "equity_quote": Decimal("10000"),
        "peak_equity_quote": Decimal("10000"),
        "realized_pnl_quote": Decimal("0"),
        "gross_exposure_quote": Decimal("0"),
        "net_exposure_quote": Decimal("0"),
        "open_orders": 0,
        "open_positions": 0,
        "snapshot_ts": BASE,
    }
    fields.update(overrides)
    return RiskStateSnapshot(**fields)


def request(*, quantity: str = "1", price: str = "2700", side: Side = Side.BUY, symbol: str = "ETHUSDT",
            strategy: str = "s1", intent_id=None) -> RiskRequest:
    return RiskRequest(
        correlation_id=uuid4(),
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        strategy_id=strategy,
        intent_id=intent_id or uuid4(),
    )


# --- configuration (fail closed, explicit units) ---

def test_limits_load_from_explicit_configuration():
    loaded = limits()

    assert loaded.limits_version == "risk-v1"
    assert loaded.max_order_notional_quote == Decimal("5000")
    assert loaded.max_drawdown_pct == Decimal("10")
    assert loaded.max_open_orders == 5


@pytest.mark.parametrize("missing", sorted(LIMIT_CONFIG))
def test_missing_mandatory_limit_fails_closed(missing):
    config = {k: v for k, v in LIMIT_CONFIG.items() if k != missing}

    with pytest.raises(RiskConfigError):
        load_limits(config)


def test_unknown_limit_key_fails_closed():
    with pytest.raises(RiskConfigError):
        load_limits({**LIMIT_CONFIG, "max_notional": "1"})


@pytest.mark.parametrize("bad", ["0", "-1", "abc"])
def test_invalid_limit_values_fail_closed(bad):
    with pytest.raises(RiskConfigError):
        load_limits({**LIMIT_CONFIG, "max_order_notional_quote": bad})


def test_engine_requires_resolved_limits():
    with pytest.raises(RiskConfigError):
        HardRiskEngine(None)  # type: ignore[arg-type]


# --- boundary values (explicit units, Decimal only) ---

def test_notional_exactly_at_the_limit_is_allowed_and_above_is_rejected():
    engine = HardRiskEngine(limits())

    at_limit = engine.evaluate(request(quantity="2", price="2500"), state())   # 5000 == limit
    above = engine.evaluate(request(quantity="2", price="2501", strategy="s2"), state())  # 5002 > limit

    assert at_limit.allowed is True
    assert above.allowed is False
    assert "max_order_notional_exceeded" in above.reason_codes


def test_notional_just_below_the_limit_is_allowed():
    engine = HardRiskEngine(limits())

    decision = engine.evaluate(request(quantity="1", price="4999.99"), state())

    assert decision.allowed is True
    assert decision.notional == Decimal("4999.99")


def test_drawdown_percent_boundary_is_deterministic():
    engine = HardRiskEngine(limits())
    at_limit = state(equity_quote=Decimal("9000"), peak_equity_quote=Decimal("10000"))  # 10.0%
    just_below = state(equity_quote=Decimal("9000.01"), peak_equity_quote=Decimal("10000"))

    rejected = engine.evaluate(request(), at_limit)
    allowed = engine.evaluate(request(strategy="s2"), just_below)

    assert rejected.allowed is False
    assert "max_drawdown_exceeded" in rejected.reason_codes
    assert allowed.allowed is True
    drawdown_rule = next(r for r in rejected.rule_results if r.rule_id == "max_drawdown")
    assert drawdown_rule.unit == "percent"
    assert drawdown_rule.actual == Decimal("10.00") or drawdown_rule.actual == Decimal("10")


def test_loss_limit_breach_requests_halt_new_risk_and_never_liquidation():
    engine = HardRiskEngine(limits())
    breached = state(realized_pnl_quote=Decimal("-1000"))  # exactly at the 1000 loss limit

    decision = engine.evaluate(request(), breached)

    assert decision.allowed is False
    assert "loss_limit_breached" in decision.reason_codes
    assert decision.risk_actions == ["halt_new_risk"]
    assert all("liquidat" not in action for action in decision.risk_actions)


def test_multiple_simultaneous_exposures_are_evaluated_together():
    engine = HardRiskEngine(limits())
    crowded = state(
        symbol_exposure_quote={"ETHUSDT": Decimal("19000")},
        strategy_exposure_quote={"s1": Decimal("39000")},
        gross_exposure_quote=Decimal("49000"),
        net_exposure_quote=Decimal("29000"),
    )

    decision = engine.evaluate(request(quantity="1", price="2000"), crowded)  # adds 2000

    assert decision.allowed is False
    assert {"max_symbol_exposure_exceeded", "max_strategy_exposure_exceeded",
            "max_gross_exposure_exceeded", "max_net_exposure_exceeded"} <= set(decision.reason_codes)


def test_open_order_and_position_counts_are_enforced():
    engine = HardRiskEngine(limits())

    decision = engine.evaluate(request(), state(open_orders=5, open_positions=3))

    assert decision.allowed is False
    assert "max_open_orders_exceeded" in decision.reason_codes
    assert "max_open_positions_exceeded" in decision.reason_codes


def test_concentration_limit_uses_percent_of_equity():
    engine = HardRiskEngine(limits())
    crowded = state(symbol_exposure_quote={"ETHUSDT": Decimal("4500")})

    decision = engine.evaluate(request(quantity="1", price="1000"), crowded)  # 5500/10000 = 55%

    assert decision.allowed is False
    rule = next(r for r in decision.rule_results if r.rule_id == "concentration")
    assert rule.unit == "percent"
    assert rule.actual == Decimal("55.00") or rule.actual == Decimal("55")


# --- halt / reduce-only / duplicates ---

def test_trading_halt_blocks_new_risk():
    engine = HardRiskEngine(limits(trading_halted=True))

    decision = engine.evaluate(request(), state())

    assert decision.allowed is False
    assert "trading_halted" in decision.reason_codes


def test_reduce_only_mode_blocks_exposure_increasing_orders():
    engine = HardRiskEngine(limits(reduce_only_mode=True))
    long_state = state(position_quantity={"ETHUSDT": Decimal("1")})

    increasing = engine.evaluate(request(side=Side.BUY), long_state)
    reducing = engine.evaluate(request(side=Side.SELL, quantity="1", strategy="s2"), long_state)

    assert increasing.allowed is False
    assert "reduce_only_violation" in increasing.reason_codes
    assert reducing.allowed is True


def test_reduce_only_rejects_quantity_that_would_cross_through_zero():
    engine = HardRiskEngine(limits(reduce_only_mode=True))
    long_state = state(position_quantity={"ETHUSDT": Decimal("1")})

    decision = engine.evaluate(
        request(side=Side.SELL, quantity="1.00000001", price="1000", strategy="s2"),
        long_state,
    )

    assert decision.allowed is False
    assert "reduce_only_quantity_exceeded" in decision.reason_codes


def test_reduce_only_requires_clear_open_orders_and_pending_intents():
    open_order_engine = HardRiskEngine(limits(reduce_only_mode=True))
    long_with_open_order = state(
        position_quantity={"ETHUSDT": Decimal("1")},
        open_orders=1,
    )
    open_order_decision = open_order_engine.evaluate(
        request(side=Side.SELL, quantity="0.25", price="1000", strategy="s2"),
        long_with_open_order,
    )

    pending = PendingIntentRegistry()
    pending.register(
        PendingIntent(
            intent_id=uuid4(),
            symbol="BTCUSDT",
            strategy_id="other-strategy",
            side=Side.SELL,
            quantity=Decimal("0.1"),
            notional=Decimal("100"),
        )
    )
    pending_engine = HardRiskEngine(
        limits(reduce_only_mode=True),
        pending=pending,
        include_pending_reservations=False,
    )
    pending_decision = pending_engine.evaluate(
        request(side=Side.SELL, quantity="0.25", price="1000", strategy="s2"),
        state(position_quantity={"ETHUSDT": Decimal("1")}),
    )

    assert open_order_decision.allowed is False
    assert "reduce_only_open_orders_present" in open_order_decision.reason_codes
    assert pending_decision.allowed is False
    assert "reduce_only_pending_intents_present" in pending_decision.reason_codes


def test_reduce_only_keeps_strategy_attribution_fail_closed():
    engine = HardRiskEngine(
        limits(reduce_only_mode=True),
        include_pending_reservations=True,
        enforce_attributed_inventory=True,
    )
    attributed_elsewhere = state(
        position_quantity={"ETHUSDT": Decimal("1")},
        strategy_position_quantity={"s1": Decimal("1"), "s2": Decimal("0")},
        strategy_open_sell_quantity={"s1": Decimal("0"), "s2": Decimal("0")},
    )

    decision = engine.evaluate(
        request(side=Side.SELL, quantity="0.25", price="1000", strategy="s2"),
        attributed_elsewhere,
    )

    assert decision.allowed is False
    assert "insufficient_attributed_inventory" in decision.reason_codes


def test_reduce_only_reduction_is_not_blocked_by_limits_for_new_exposure():
    engine = HardRiskEngine(limits(reduce_only_mode=True))
    breached = state(
        equity_quote=Decimal("8000"),
        peak_equity_quote=Decimal("10000"),
        realized_pnl_quote=Decimal("-1000"),
        gross_exposure_quote=Decimal("49000"),
        net_exposure_quote=Decimal("40000"),
        open_positions=3,
        symbol_exposure_quote={"ETHUSDT": Decimal("25000")},
        strategy_exposure_quote={"s2": Decimal("45000")},
        position_quantity={"ETHUSDT": Decimal("1")},
    )

    decision = engine.evaluate(
        request(side=Side.SELL, quantity="0.25", price="1000", strategy="s2"),
        breached,
    )

    assert decision.allowed is True
    assert "halt_new_risk" in decision.risk_actions
    for rule_id in (
        "max_open_positions",
        "max_symbol_exposure",
        "max_strategy_exposure",
        "max_net_exposure",
        "loss_limit",
        "max_drawdown",
        "concentration",
    ):
        rule = next(item for item in decision.rule_results if item.rule_id == rule_id)
        assert rule.allowed is True
        assert rule.reason_code == "reduce_only_non_expanding"

    gross_rule = next(item for item in decision.rule_results if item.rule_id == "max_gross_exposure")
    assert gross_rule.allowed is True
    assert gross_rule.reason_code == "gross_exposure_ok"


def test_reduce_only_sequential_approval_blocks_second_when_pending_exists():
    engine = HardRiskEngine(limits(reduce_only_mode=True))
    long_state = state(position_quantity={"ETHUSDT": Decimal("1")})

    first = engine.evaluate(
        request(side=Side.SELL, quantity="0.6", price="1000", strategy="s2"),
        long_state,
    )
    second = engine.evaluate(
        request(side=Side.SELL, quantity="0.6", price="1000", strategy="s2"),
        long_state,
    )

    assert first.allowed is True
    assert second.allowed is False
    assert "reduce_only_pending_intents_present" in second.reason_codes


def test_reduce_only_short_side_boundaries_are_symmetric():
    short_state = state(position_quantity={"ETHUSDT": Decimal("-1")})

    exact = HardRiskEngine(limits(reduce_only_mode=True)).evaluate(
        request(side=Side.BUY, quantity="1", price="1000", strategy="s2"),
        short_state,
    )
    over = HardRiskEngine(limits(reduce_only_mode=True)).evaluate(
        request(side=Side.BUY, quantity="1.00000001", price="1000", strategy="s2"),
        short_state,
    )
    same_side = HardRiskEngine(limits(reduce_only_mode=True)).evaluate(
        request(side=Side.SELL, quantity="0.25", price="1000", strategy="s2"),
        short_state,
    )

    assert exact.allowed is True
    assert over.allowed is False
    assert "reduce_only_quantity_exceeded" in over.reason_codes
    assert same_side.allowed is False
    assert "reduce_only_violation" in same_side.reason_codes


def test_reduce_only_rejects_zero_or_missing_position():
    engine = HardRiskEngine(limits(reduce_only_mode=True))

    buy = engine.evaluate(
        request(side=Side.BUY, quantity="0.25", price="1000", strategy="s2"),
        state(position_quantity={"ETHUSDT": Decimal("0")}),
    )
    sell = engine.evaluate(
        request(side=Side.SELL, quantity="0.25", price="1000", strategy="s3"),
        state(),
    )

    assert buy.allowed is False
    assert "reduce_only_violation" in buy.reason_codes
    assert sell.allowed is False
    assert "reduce_only_violation" in sell.reason_codes


def test_reduce_only_attributed_buy_cannot_cover_another_strategys_short():
    engine = HardRiskEngine(
        limits(reduce_only_mode=True),
        enforce_attributed_inventory=True,
    )
    attributed_elsewhere = state(
        position_quantity={"ETHUSDT": Decimal("-1")},
        strategy_position_quantity={"s1": Decimal("-1"), "s2": Decimal("0")},
    )

    decision = engine.evaluate(
        request(side=Side.BUY, quantity="0.25", price="1000", strategy="s2"),
        attributed_elsewhere,
    )

    assert decision.allowed is False
    assert "insufficient_attributed_inventory" in decision.reason_codes


def test_reduce_only_does_not_exempt_expanding_net_exposure():
    engine = HardRiskEngine(limits(reduce_only_mode=True))
    exposed = state(
        gross_exposure_quote=Decimal("1000"),
        net_exposure_quote=Decimal("-29900"),
        symbol_exposure_quote={"ETHUSDT": Decimal("1000")},
        strategy_exposure_quote={"s2": Decimal("1000")},
        position_quantity={"ETHUSDT": Decimal("1")},
    )

    decision = engine.evaluate(
        request(side=Side.SELL, quantity="0.25", price="1000", strategy="s2"),
        exposed,
    )

    assert decision.allowed is False
    assert "max_net_exposure_exceeded" in decision.reason_codes
    net_rule = next(item for item in decision.rule_results if item.rule_id == "max_net_exposure")
    assert net_rule.reason_code == "max_net_exposure_exceeded"


def test_reduce_only_does_not_exempt_expanding_strategy_exposure():
    engine = HardRiskEngine(limits(reduce_only_mode=True))
    exposed = state(
        gross_exposure_quote=Decimal("1000"),
        net_exposure_quote=Decimal("1000"),
        symbol_exposure_quote={"ETHUSDT": Decimal("1000")},
        strategy_exposure_quote={"s2": Decimal("-39900")},
        position_quantity={"ETHUSDT": Decimal("1")},
    )

    decision = engine.evaluate(
        request(side=Side.SELL, quantity="0.25", price="1000", strategy="s2"),
        exposed,
    )

    assert decision.allowed is False
    assert "max_strategy_exposure_exceeded" in decision.reason_codes
    strategy_rule = next(item for item in decision.rule_results if item.rule_id == "max_strategy_exposure")
    assert strategy_rule.reason_code == "max_strategy_exposure_exceeded"


def test_reduce_only_does_not_exempt_gross_exposure():
    engine = HardRiskEngine(limits(reduce_only_mode=True))
    exposed = state(
        gross_exposure_quote=Decimal("50000"),
        net_exposure_quote=Decimal("1000"),
        symbol_exposure_quote={"ETHUSDT": Decimal("1000")},
        strategy_exposure_quote={"s2": Decimal("1000")},
        position_quantity={"ETHUSDT": Decimal("1")},
    )

    decision = engine.evaluate(
        request(side=Side.SELL, quantity="0.25", price="1000", strategy="s2"),
        exposed,
    )

    assert decision.allowed is False
    assert "max_gross_exposure_exceeded" in decision.reason_codes
    gross_rule = next(item for item in decision.rule_results if item.rule_id == "max_gross_exposure")
    assert gross_rule.reason_code == "max_gross_exposure_exceeded"


def test_reduce_only_symbol_exemption_requires_consistent_signed_exposure():
    engine = HardRiskEngine(limits(reduce_only_mode=True))
    inconsistent = state(
        symbol_exposure_quote={"ETHUSDT": Decimal("-25000")},
        position_quantity={"ETHUSDT": Decimal("1")},
    )

    decision = engine.evaluate(
        request(side=Side.SELL, quantity="0.25", price="1000", strategy="s2"),
        inconsistent,
    )

    assert decision.allowed is False
    assert "max_symbol_exposure_exceeded" in decision.reason_codes
    symbol_rule = next(item for item in decision.rule_results if item.rule_id == "max_symbol_exposure")
    assert symbol_rule.reason_code == "max_symbol_exposure_exceeded"


def test_reduce_only_does_not_bypass_trading_halt_or_stale_state():
    halted = HardRiskEngine(limits(reduce_only_mode=True, trading_halted=True)).evaluate(
        request(side=Side.SELL, quantity="0.25", price="1000", strategy="s2"),
        state(position_quantity={"ETHUSDT": Decimal("1")}),
    )
    stale_engine = HardRiskEngine(
        limits(reduce_only_mode=True),
        max_state_age_seconds=1.0,
    )
    stale = stale_engine.evaluate(
        request(side=Side.SELL, quantity="0.25", price="1000", strategy="s2"),
        state(position_quantity={"ETHUSDT": Decimal("1")}),
        now=BASE + timedelta(seconds=2),
    )

    assert halted.allowed is False
    assert "trading_halted" in halted.reason_codes
    assert stale.allowed is False
    assert "stale_risk_state" in stale.reason_codes


def test_duplicate_and_conflicting_intents_are_rejected():
    pending = PendingIntentRegistry()
    engine = HardRiskEngine(limits(), pending=pending)
    first = request()

    assert engine.evaluate(first, state()).allowed is True
    duplicate_decision = engine.evaluate(first, state())
    conflicting_decision = engine.evaluate(request(side=Side.SELL), state())

    assert duplicate_decision.allowed is False
    assert "duplicate_intent" in duplicate_decision.reason_codes
    assert conflicting_decision.allowed is False
    assert "conflicting_intent" in conflicting_decision.reason_codes


def test_pending_registry_registers_only_allowed_intents():
    engine = HardRiskEngine(limits())
    rejected = request(quantity="2", price="2501")

    engine.evaluate(rejected, state())

    assert len(engine.pending) == 0


def test_registry_matching_is_symmetric_for_conflicts():
    registry = PendingIntentRegistry()
    item = PendingIntent(intent_id=uuid4(), symbol="ETHUSDT", strategy_id="s1", side=Side.BUY)
    registry.register(item)

    assert registry.conflicting(symbol="ETHUSDT", strategy_id="s1", side=Side.SELL) is item
    assert registry.conflicting(symbol="ETHUSDT", strategy_id="s1", side=Side.BUY) is None
    assert registry.conflicting(symbol="BTCUSDT", strategy_id="s1", side=Side.SELL) is None


# --- stale state input ---

def test_stale_risk_state_is_rejected_when_freshness_is_configured():
    engine = HardRiskEngine(limits(), max_state_age_seconds=5.0)
    stale_state = state(snapshot_ts=BASE - timedelta(seconds=30))

    decision = engine.evaluate(request(), stale_state, now=BASE)

    assert decision.allowed is False
    assert "stale_risk_state" in decision.reason_codes


def test_missing_state_timestamp_fails_closed_when_freshness_is_configured():
    engine = HardRiskEngine(limits(), max_state_age_seconds=5.0)

    decision = engine.evaluate(request(), state(snapshot_ts=None), now=BASE)

    assert decision.allowed is False
    assert "risk_state_timestamp_unavailable" in decision.reason_codes


def test_fresh_risk_state_passes_freshness_check():
    engine = HardRiskEngine(limits(), max_state_age_seconds=5.0)

    decision = engine.evaluate(request(), state(snapshot_ts=BASE), now=BASE + timedelta(seconds=1))

    assert decision.allowed is True


# --- determinism / no LLM / no network ---

def test_decision_is_deterministic_for_identical_inputs():
    engine_a, engine_b = HardRiskEngine(limits()), HardRiskEngine(limits())
    intent_id = uuid4()
    req = request(intent_id=intent_id)
    snapshot = state()

    first = engine_a.evaluate(req, snapshot)
    second = engine_b.evaluate(req, snapshot)

    assert first.allowed == second.allowed
    assert first.reason_codes == second.reason_codes
    assert [r.reason_code for r in first.rule_results] == [r.reason_code for r in second.rule_results]
    assert [str(r.actual) for r in first.rule_results] == [str(r.actual) for r in second.rule_results]


def test_rule_results_always_declare_their_unit():
    engine = HardRiskEngine(limits())

    decision = engine.evaluate(request(), state())

    assert {r.unit for r in decision.rule_results} <= {"quote_currency", "count", "percent"}
    for result in decision.rule_results:
        assert isinstance(result.actual, Decimal) and isinstance(result.limit, Decimal)
        assert result.reason_code


def test_risk_layer_has_no_llm_or_network_dependency():
    import crypto_trader.risk.engine as engine_module

    source = open(engine_module.__file__, encoding="utf-8").read().lower()

    for token in ("crewai", "deepseek", "openai", "requests", "http", "socket"):
        assert token not in source, f"risk engine must not reference {token}"

def test_future_risk_state_timestamp_is_rejected_when_freshness_is_configured():
    engine = HardRiskEngine(limits(), max_state_age_seconds=5.0)
    future_state = state(snapshot_ts=BASE + timedelta(seconds=10))

    decision = engine.evaluate(request(), future_state, now=BASE)

    assert decision.allowed is False
    assert "risk_state_timestamp_in_future" in decision.reason_codes

