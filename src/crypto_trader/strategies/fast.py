"""Fast preview backtest: same Strategy, same sizing code, same fill model, no engine pipeline.

Use it to screen strategies in seconds; confirm the shortlist with runtime.run_backtest, which runs the
full engine (validity, hard risk, safety, Nautilus simulation, journal). The preview does NOT apply the
engine's risk, validity or safety rules, so when a strategy hits those rules the two results differ;
`compare_with_engine` reports the difference.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Sequence

from crypto_trader.paper import MarketObservation

from .api import Bar, Strategy, StrategyError
from .runtime import (BacktestResult, MarketSpec, RunSettings, StrategyDecisionSource, _perf, _ref,
                      build_tape, buy_and_hold)

__all__ = ["run_fast_backtest"]


@dataclass
class _Snap:
    position_quantity: Decimal
    average_cost: Decimal
    realized_pnl_quote: Decimal
    equity_quote: Decimal
    open_sell_quantity: Decimal
    quote_free: Decimal
    stop_exits: int = 0
    target_exits: int = 0


def run_fast_backtest(strategy: Strategy, bars: Sequence[Bar], *, market: MarketSpec | None = None,
                      settings: RunSettings | None = None) -> BacktestResult:
    market = market or MarketSpec()
    settings = settings or RunSettings()
    if bars[0].timeframe != strategy.timeframe:
        raise StrategyError(f"strategy timeframe {strategy.timeframe} != data timeframe {bars[0].timeframe}.")
    observations, books = build_tape(bars, market, settings.slippage_bps)
    # book i (index in `books` skipping DELETE rows) = bar i's open
    asks = []
    bids = []
    for b in books:
        if b.action == "ADD":
            bids.append(b.levels[0].price)
            asks.append(b.levels[1].price)
    cash = settings.capital_quote
    qty = Decimal(0)
    fee_rate = settings.taker_fee
    pending: tuple[str, Decimal] | None = None
    fills: list[dict[str, Any]] = []
    state = {"cash": cash, "qty": qty}

    def position(observation: MarketObservation) -> _Snap:
        q, c = state["qty"], state["cash"]
        return _Snap(position_quantity=q, average_cost=Decimal(0), realized_pnl_quote=Decimal(0),
                     equity_quote=c + q * observation.price, open_sell_quantity=Decimal(0), quote_free=c)

    source = StrategyDecisionSource(strategy, bars, market=market, settings=settings, position=position)
    equity: list[tuple[str, float]] = []
    trades: list[dict[str, Any]] = []
    open_trade: dict[str, Any] | None = None
    fees_total = Decimal(0)
    in_mkt = 0
    for i, bar in enumerate(bars):
        obs = observations[i] if i < len(observations) else None   # last bar: no later book to fill it
        if pending is not None:                     # fill at this bar's open book
            side, q = pending
            price = asks[i] if side == "buy" else bids[i]
            notional = q * price
            fee = notional * fee_rate
            fees_total += fee
            if side == "buy":
                if notional + fee > state["cash"]:
                    q = ((state["cash"] / (price * (1 + fee_rate))) / market.quantity_step).to_integral_value(
                        rounding=ROUND_DOWN) * market.quantity_step
                    notional, fee = q * price, q * price * fee_rate
                state["cash"] -= notional + fee
                state["qty"] += q
                if open_trade is None:
                    open_trade = {"entry_time": bar.open_time.isoformat(), "entry_price": str(price),
                                  "quantity": Decimal(0), "cost": Decimal(0)}
                open_trade["quantity"] += q
                open_trade["cost"] += notional + fee
            else:
                q = min(q, state["qty"])
                notional, fee = q * price, q * price * fee_rate
                state["cash"] += notional - fee
                state["qty"] -= q
                if open_trade is not None and state["qty"] <= 0:
                    trades.append({"entry_time": open_trade["entry_time"], "entry_price": open_trade["entry_price"],
                                   "exit_time": bar.open_time.isoformat(), "exit_price": str(price),
                                   "quantity": str(open_trade["quantity"]),
                                   "pnl_quote": str((notional - fee - open_trade["cost"]).quantize(Decimal("0.0001")))})
                    open_trade = None
            fills.append({"time": bar.open_time.isoformat(), "side": side, "quantity": str(q), "price": str(price),
                          "fee": str(fee)})
            pending = None
        if obs is not None:
            decision = source.decide(obs)
            if decision.action in {"buy", "sell"}:
                pending = (decision.action, decision.quantity)
        if state["qty"] > 0:
            in_mkt += 1
        equity.append((bar.close_time.isoformat(), float(state["cash"] + state["qty"] * bar.close)))
    if open_trade is not None:
        trades.append({"entry_time": open_trade["entry_time"], "entry_price": open_trade["entry_price"],
                       "exit_time": None, "exit_price": None, "quantity": str(open_trade["quantity"]),
                       "pnl_quote": None, "open": True})
    s = float(settings.slippage_bps) / 1e4
    closed = [t for t in trades if not t.get("open")]
    metrics = _perf(equity, float(settings.capital_quote), bars) | {
        "fills": len(fills), "round_trips": len(closed), "fees_quote": float(fees_total),
        "slippage_cost_quote_est": round(sum(float(Decimal(f["quantity"]) * Decimal(f["price"])) * s / (1 + s)
                                             for f in fills), 6),
        "time_in_market": in_mkt / len(bars),
        "win_rate": sum(1 for t in closed if Decimal(t["pnl_quote"]) > 0) / max(1, len(closed)),
        "mode": "fast-preview (no validity/risk/safety/Nautilus)",
    }
    from dataclasses import asdict
    return BacktestResult(
        strategy=type(strategy).name, strategy_version=type(strategy).version, symbol=market.symbol,
        timeframe=bars[0].timeframe, start=bars[0].open_time.isoformat(), end=bars[-1].close_time.isoformat(),
        bars=len(bars), settings={k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(settings).items()},
        metrics=metrics, benchmark=buy_and_hold(bars, settings), trades=trades, equity_curve=equity,
        engine={"mode": "fast-preview", "fills": fills}, report={},
    )
