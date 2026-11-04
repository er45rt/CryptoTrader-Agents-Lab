"""Run any plug-in Strategy through the UNCHANGED engine pipeline (D29).

decision (strategy) -> validity gate -> hard risk -> safety -> Nautilus simulated execution -> journal

Nothing in paper/, risk/, validity/, safety/, execution/ or persistence/ is modified. This module only
*composes* them: it adapts a Strategy to the paper DecisionSource contract, turns OHLCV bars into the
engine's market tape, and summarises the journal into performance metrics.

Execution model (documented, deterministic):
  * the strategy sees bar i only after it closes; its decision is stamped at bar i's close time;
  * orders are MARKET orders (spot, long only), filled by Nautilus against the book that represents the
    NEXT bar's open: bid = open*(1-s), ask = open*(1+s), s = slippage_bps/1e4 (a spread-style slippage);
  * fees are charged by the simulator's spot cash model (taker fee) and appear on every fill;
  * position sizing: target in [0,1] x max_position_quote, rounded down to the quantity step; a change
    smaller than min_order_quote is skipped (exchange minimum notional).
"""
from __future__ import annotations

import json
import math
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import UUID, uuid4

from crypto_trader.crew_paper_bridge import CrewPaperBridgeConfig, portfolio_snapshot
from crypto_trader.execution import InstrumentSpec
from crypto_trader.execution.nautilus import NautilusSimulationRunner
from crypto_trader.execution.ports import BookLevel, MarketBookUpdate
from crypto_trader.paper import (
    FrozenMarketDataFeed, MarketObservation, StrategyDecision, TradingMode, TradingSession,
    load_session_config,
)
from crypto_trader.paper.config import SpotCashSettings
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.risk import load_limits
from crypto_trader.safety import SafetyController
from crypto_trader.validity import DecisionValidityGate, load_policy

from .api import TIMEFRAMES, Bar, BarHistory, Signal, Strategy, StrategyError

__all__ = ["MarketSpec", "RunSettings", "StrategyDecisionSource", "build_tape", "run_backtest",
           "BacktestResult", "metrics_from_run"]


@dataclass(frozen=True)
class MarketSpec:
    symbol: str = "BTCUSDT"
    base_currency: str = "BTC"
    quote_currency: str = "USDT"
    tick_size: Decimal = Decimal("0.01")
    quantity_step: Decimal = Decimal("0.00001")
    price_precision: int = 2
    size_precision: int = 5
    min_order_quote: Decimal = Decimal("5")


@dataclass(frozen=True)
class RunSettings:
    capital_quote: Decimal = Decimal("1000")
    max_position_quote: Decimal | None = None      # optional hard cap in quote; default: no cap (equity-sized)
    taker_fee: Decimal = Decimal("0.001")           # 0.10%
    maker_fee: Decimal = Decimal("0.001")
    slippage_bps: Decimal = Decimal("5")            # 0.05% per side
    loss_limit_quote: Decimal | None = None         # hard-risk loss limit; default = capital
    max_drawdown_pct: Decimal = Decimal("100")      # hard-risk drawdown limit (engine rule)
    strategy_params: dict[str, Any] = field(default_factory=dict)

    risk_headroom: Decimal = Decimal("100")         # engine exposure limits = capital x this when uncapped
    risk_overrides: dict[str, Any] = field(default_factory=dict)   # any hard-risk limit key, applied last

    @property
    def position_cap(self) -> Decimal:
        return self.max_position_quote if self.max_position_quote is not None else (
            self.capital_quote * self.risk_headroom)


REBALANCE_BAND = Decimal("0.10")
INVEST_FRACTION = Decimal("0.98")   # target 1 = 98% of equity (fee + slippage headroom for market buys)
REFRESH_AFTER_TRADE = 3   # bars: the order fills at the next bar's open; 3 leaves margin for partial fills


def _ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000) * 1_000


def _ref(index: int, bar: Bar) -> str:
    return f"public.kline.{bar.timeframe}#{index}@{bar.open_time.strftime('%Y%m%dT%H%M%SZ')}"


def _q(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def build_tape(bars: Sequence[Bar], market: MarketSpec, slippage_bps: Decimal,
               depth_quote: Decimal = Decimal("100000000"),
               next_open: tuple[datetime, Decimal] | None = None,
               ) -> tuple[tuple[MarketObservation, ...], tuple[MarketBookUpdate, ...]]:
    """Observations at each bar close; one book per bar placed 1 ms after the previous bar closed.

    The engine only accepts a decision that a LATER market event can execute, so the last bar's
    observation is kept only when `next_open` (the open of the bar now forming) is known.
    """
    s = slippage_bps / Decimal(10000)
    observations = tuple(MarketObservation(
        symbol=market.symbol, price=b.close, observed_at=b.close_time, market_reference=_ref(i, b),
        volume=b.volume) for i, b in enumerate(bars))
    if next_open is None:
        observations = observations[:-1]
    elif next_open[0] != bars[-1].close_time:
        raise StrategyError("next_open must be the open of the bar that follows the last closed bar.")
    books: list[MarketBookUpdate] = []
    seq = 1
    prev: tuple[Decimal, Decimal] | None = None
    opens = [(b.open_time, b.open) for b in bars] + ([next_open] if next_open is not None else [])
    for open_time, open_price in opens:
        bid = (open_price * (1 - s)).quantize(market.tick_size, rounding=ROUND_DOWN)
        ask = (open_price * (1 + s)).quantize(market.tick_size, rounding=ROUND_DOWN) + (
            market.tick_size if s > 0 else Decimal(0))
        size = _q(depth_quote / open_price, market.quantity_step)
        ts = _ns(open_time) + 1_000_000  # just after the previous bar's close = this bar's open
        if prev is not None:
            books.append(MarketBookUpdate(levels=[BookLevel("BUY", prev[0], size, 1),
                                                  BookLevel("SELL", prev[1], size, 2)],
                                          ts_ns=ts, sequence=seq, action="DELETE"))
            seq += 2
        books.append(MarketBookUpdate(levels=[BookLevel("BUY", bid, size, 1), BookLevel("SELL", ask, size, 2)],
                                      ts_ns=ts, sequence=seq, action="ADD"))
        seq += 2
        prev = (bid, ask)
    return observations, tuple(books)


class StrategyDecisionSource:
    """paper.DecisionSource adapter: closed bars -> Strategy.on_bar -> StrategyDecision."""

    def __init__(self, strategy: Strategy, bars: Sequence[Bar], *, market: MarketSpec,
                 settings: RunSettings, position: Callable[[MarketObservation], Any],
                 trade_from_index: int = 0) -> None:
        self.trade_from_index = trade_from_index   # bars before this index only warm the strategy up
        self.strategy = strategy
        self.bars = tuple(bars)
        self.market = market
        self.settings = settings
        self.position = position
        self._index = {_ref(i, b): i for i, b in enumerate(self.bars)}
        self.signals: list[dict[str, Any]] = []
        self._last_target: Decimal | None = None
        self._cached: Any = None
        self._cached_mark: Decimal | None = None
        self._fresh_bars = 0

    def _snapshot(self, observation: MarketObservation) -> Any:
        """Journal-derived position. The ledger only changes after this source emits a trade, so it is
        rebuilt for REFRESH_AFTER_TRADE bars after each trade decision and otherwise re-marked."""
        if self._cached is None or self._fresh_bars > 0:
            self._cached = self.position(observation)
            self._cached_mark = observation.price
            if self._fresh_bars > 0:
                self._fresh_bars -= 1
            return self._cached
        snap = self._cached
        equity = snap.equity_quote + snap.position_quantity * (observation.price - self._cached_mark)
        return type(snap)(position_quantity=snap.position_quantity, average_cost=snap.average_cost,
                          realized_pnl_quote=snap.realized_pnl_quote, equity_quote=equity,
                          open_sell_quantity=snap.open_sell_quantity, quote_free=snap.quote_free,
                          stop_exits=snap.stop_exits, target_exits=snap.target_exits)

    def decide(self, observation: MarketObservation) -> StrategyDecision:
        i = self._index.get(observation.market_reference)
        if i is None:
            raise StrategyError(f"observation {observation.market_reference} is not one of the bars.")
        at = observation.observed_at
        if i + 1 < self.strategy.warmup_bars:
            return StrategyDecision(action="hold", confidence="low", rationale="warmup", decided_at=at)
        history = BarHistory(self.bars[: i + 1], as_of=at)
        signal = self.strategy.on_bar(history)
        if signal is not None and not isinstance(signal, Signal):
            raise StrategyError(f"{type(self.strategy).__name__}.on_bar must return Signal or None.")
        if i < self.trade_from_index:
            return StrategyDecision(action="hold", confidence="low", rationale="pre-session-history", decided_at=at)
        snap = self._snapshot(observation)
        held = snap.position_quantity - snap.open_sell_quantity
        self.signals.append({"index": i, "at": at.isoformat(), "target": None if signal is None else str(signal.target),
                             "reason": "" if signal is None else signal.reason, "held": str(held)})
        if signal is None:
            return StrategyDecision(action="hold", confidence="low", rationale="strategy:keep", decided_at=at)
        price = observation.price
        # Size from current equity (capped), but only act when the target CHANGES or the holding has
        # drifted more than REBALANCE_BAND from it (e.g. an earlier order was rejected or partly filled).
        budget = min(signal.target * max(snap.equity_quote, Decimal(0)) * INVEST_FRACTION,
                     signal.target * self.settings.position_cap)
        want_qty = _q(budget / price, self.market.quantity_step)
        delta = want_qty - held
        unchanged = self._last_target is not None and signal.target == self._last_target
        within_band = abs(delta) <= REBALANCE_BAND * max(want_qty, held)
        if abs(delta) * price < self.market.min_order_quote or (unchanged and within_band):
            self._last_target = signal.target
            return StrategyDecision(action="hold", confidence="low", rationale=f"strategy:{signal.reason}:at_target",
                                    decided_at=at)
        self._last_target = signal.target
        self._fresh_bars = REFRESH_AFTER_TRADE
        if delta > 0:
            free_qty = _q(snap.quote_free / (price * (1 + self.settings.taker_fee) *
                                            (1 + self.settings.slippage_bps / 10000)), self.market.quantity_step)
            qty = min(delta, free_qty)
            if qty * price < self.market.min_order_quote:
                return StrategyDecision(action="hold", confidence="low", rationale="strategy:no_cash", decided_at=at)
            return StrategyDecision(action="buy", confidence="high", rationale=f"strategy:{signal.reason}",
                                    quantity=qty, order_type="market", decided_at=at)
        qty = _q(-delta, self.market.quantity_step)
        return StrategyDecision(action="sell", confidence="high", rationale=f"strategy:{signal.reason}",
                                quantity=qty, order_type="market", decided_at=at)


@dataclass
class BacktestResult:
    strategy: str
    strategy_version: str
    symbol: str
    timeframe: str
    start: str
    end: str
    bars: int
    settings: dict[str, Any]
    metrics: dict[str, Any]
    benchmark: dict[str, Any]
    trades: list[dict[str, Any]]
    equity_curve: list[tuple[str, float]]
    engine: dict[str, Any]
    report: dict[str, Any]

    def write(self, folder: str | Path) -> Path:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / "backtest_result.json"
        out.write_text(json.dumps(asdict(self), indent=1, default=str), encoding="utf-8")
        return out


def _limits(settings: RunSettings) -> dict[str, Any]:
    cap = settings.capital_quote
    pos = settings.position_cap
    return {**_default_limits(cap, pos, settings), **{k: str(v) if isinstance(v, Decimal) else v
                                                      for k, v in settings.risk_overrides.items()}}


def _default_limits(cap: Decimal, pos: Decimal, settings: RunSettings) -> dict[str, Any]:
    return {
        "limits_version": "strategies-runtime-v1",
        "max_order_notional_quote": str(pos * Decimal("1.05")),
        "max_symbol_exposure_quote": str(pos * Decimal("1.05")),
        "max_gross_exposure_quote": str(pos * Decimal("2.2")),
        "max_net_exposure_quote": str(pos * Decimal("1.05")),
        "max_strategy_exposure_quote": str(pos * Decimal("1.05")),
        "loss_limit_quote": str(settings.loss_limit_quote if settings.loss_limit_quote is not None else cap),
        "max_drawdown_pct": str(settings.max_drawdown_pct),
        "concentration_limit_pct": "100",
        "max_open_orders": 2,
        # The hard-risk rule counts the open position itself; 2 lets an exit/top-up pass while one
        # position is open (single symbol, long only, so a second position cannot exist).
        "max_open_positions": 2,
    }


VALIDITY = {"policy_id": "strategies-validity-v1", "max_age_seconds": 60, "max_market_move_bps": "1000"}


class _IncrementalReader:
    """Read-only journal view for portfolio_snapshot that fetches only recent events each call.

    Re-reads an overlap window (2 bars) before the newest event already seen and de-duplicates by
    event_id, so an event appended late with an older timestamp inside that window is still seen.
    """

    def __init__(self, journal: Any, correlation_id: UUID, overlap: timedelta) -> None:
        self._journal = journal
        self._cid = correlation_id
        self._overlap = overlap
        self._events: dict[UUID, Any] = {}
        self._latest: datetime | None = None

    def read_by_correlation(self, correlation_id: UUID, *, limit: int | None = None) -> list[Any]:
        if correlation_id != self._cid or limit is not None:
            return self._journal.read_by_correlation(correlation_id, limit=limit)
        start = None if self._latest is None else self._latest - self._overlap
        for event in self._journal.read_range(start=start):
            if event.correlation_id == self._cid and event.event_id not in self._events:
                self._events[event.event_id] = event
                if self._latest is None or event.occurred_at > self._latest:
                    self._latest = event.occurred_at
        return sorted(self._events.values(), key=lambda e: e.occurred_at)


def run_session(strategy: Strategy, bars: Sequence[Bar], *, market: MarketSpec, settings: RunSettings,
                journal_path: Path, session_id: UUID, strategy_id: str,
                trade_from_index: int = 0,
                next_open: tuple[datetime, Decimal] | None = None) -> tuple[Any, StrategyDecisionSource, Any]:
    """One deterministic pass of the engine over `bars` (used by backtest and by paper replay)."""
    if len(bars) < 2 and next_open is None:
        raise StrategyError("need at least 2 bars (or the next bar's open) to run.")
    if bars[0].timeframe != strategy.timeframe:
        raise StrategyError(f"strategy timeframe {strategy.timeframe} != data timeframe {bars[0].timeframe}.")
    observations, books = build_tape(bars, market, settings.slippage_bps, next_open=next_open)
    runner = NautilusSimulationRunner(
        instrument=InstrumentSpec(symbol=market.symbol, base_currency=market.base_currency,
                                  quote_currency=market.quote_currency, price_precision=market.price_precision,
                                  size_precision=market.size_precision,
                                  starting_balance=f"{settings.capital_quote} {market.quote_currency}"),
        book_updates=books, trades=(),
    )
    runner.configure_paper_cash(SpotCashSettings(initial_quote=settings.capital_quote,
                                                 maker_fee=settings.maker_fee, taker_fee=settings.taker_fee))
    bridge_cfg = CrewPaperBridgeConfig(session_id=session_id, symbol=market.symbol,
                                       starting_equity_quote=settings.capital_quote,
                                       max_notional_quote=settings.position_cap, tick_size=market.tick_size,
                                       quantity_step=market.quantity_step)
    journal = SQLiteEventJournal(journal_path)
    journal.__enter__()
    reader = _IncrementalReader(journal, session_id, overlap=TIMEFRAMES[bars[0].timeframe] * 2)

    def position(observation: MarketObservation) -> Any:
        return portfolio_snapshot(reader, config=bridge_cfg, mark=observation.price, as_of=observation.observed_at)

    source = StrategyDecisionSource(strategy, bars, market=market, settings=settings, position=position,
                                    trade_from_index=trade_from_index)
    session = TradingSession(
        config=load_session_config({"mode": TradingMode.PAPER.value, "symbol": market.symbol,
                                    "strategy_id": strategy_id, "session_id": session_id,
                                    "starting_equity_quote": str(settings.capital_quote),
                                    "producer": "application.strategies"}),
        journal=journal, feed=FrozenMarketDataFeed(entries=observations, feed_id=f"strategies-{strategy_id}"),
        decisions=source, validity_gate=DecisionValidityGate(load_policy(VALIDITY)),
        limits=load_limits(_limits(settings)), safety=SafetyController(correlation_id=session_id),
        simulator=runner,
    )
    try:
        report = session.run()
    except BaseException:
        journal.__exit__(None, None, None)
        raise
    return report, source, journal


def metrics_from_run(bars: Sequence[Bar], report: Any, settings: RunSettings, journal: Any,
                     session_id: UUID, market: MarketSpec) -> tuple[dict[str, Any], list[dict[str, Any]], list[tuple[str, float]]]:
    """Mark-to-market equity at every bar close, rebuilt from the journal's fill events."""
    events = sorted((e for e in journal.read_by_correlation(session_id)), key=lambda e: e.occurred_at)
    sides = {e.event_id: e.payload.side for e in events if e.event_type == "trade.intent.created"}
    fills = [e for e in events if e.event_type == "execution.fill.recorded"]
    cash = settings.capital_quote
    qty = Decimal(0)
    fees = Decimal(0)
    slip = Decimal(0)
    fi = 0
    equity: list[tuple[str, float]] = []
    trades: list[dict[str, Any]] = []
    in_mkt = 0
    open_ref: dict[str, Any] | None = None
    for b in bars:
        while fi < len(fills) and fills[fi].occurred_at <= b.close_time:
            f = fills[fi]
            side = sides.get(f.causation_id)
            fee = f.payload.fee or Decimal(0)
            notional = f.payload.quantity * f.payload.price
            fees += fee
            if side == "buy":
                cash -= notional + fee
                qty += f.payload.quantity
                if open_ref is None:
                    open_ref = {"entry_time": f.occurred_at.isoformat(), "entry_price": str(f.payload.price),
                                "quantity": Decimal(0), "cost": Decimal(0)}
                open_ref["quantity"] += f.payload.quantity
                open_ref["cost"] += notional + fee
            else:
                cash += notional - fee
                qty -= f.payload.quantity
                if open_ref is not None and qty <= 0:
                    proceeds = notional - fee
                    trades.append({"entry_time": open_ref["entry_time"], "entry_price": open_ref["entry_price"],
                                   "exit_time": f.occurred_at.isoformat(), "exit_price": str(f.payload.price),
                                   "quantity": str(open_ref["quantity"]),
                                   "pnl_quote": str((proceeds - open_ref["cost"]).quantize(Decimal("0.0001")))})
                    open_ref = None
            fi += 1
        if qty > 0:
            in_mkt += 1
        equity.append((b.close_time.isoformat(), float(cash + qty * b.close)))
    if open_ref is not None:
        trades.append({"entry_time": open_ref["entry_time"], "entry_price": open_ref["entry_price"],
                       "exit_time": None, "exit_price": None, "quantity": str(open_ref["quantity"]),
                       "pnl_quote": None, "open": True})
    s = float(settings.slippage_bps) / 1e4
    slip_cost = sum(float(f.payload.quantity * f.payload.price) * s / (1 + s) for f in fills)
    return (_perf(equity, float(settings.capital_quote), bars) | {
        "fills": len(fills), "round_trips": sum(1 for t in trades if not t.get("open")),
        "fees_quote": float(fees), "slippage_cost_quote_est": round(slip_cost, 6),
        "time_in_market": in_mkt / len(bars),
        "win_rate": (sum(1 for t in trades if t.get("pnl_quote") and Decimal(t["pnl_quote"]) > 0) /
                     max(1, sum(1 for t in trades if not t.get("open")))),
        "engine_equity_quote": str(report.equity_quote),
        "risk_counts": dict(report.risk_counts), "validity_counts": dict(report.validity_counts),
        "safety_blocked": report.safety_blocked,
    }), trades, equity


def _perf(equity: list[tuple[str, float]], capital: float, bars: Sequence[Bar]) -> dict[str, Any]:
    values = [v for _, v in equity]
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
    per_year = timedelta(days=365) / TIMEFRAMES[bars[0].timeframe]
    mean = sum(rets) / len(rets) if rets else 0.0
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0.0
    peak, mdd = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    years = (bars[-1].close_time - bars[0].open_time) / timedelta(days=365)
    final = values[-1] if values else capital
    return {
        "final_equity_quote": round(final, 6), "net_return": final / capital - 1,
        "cagr": (final / capital) ** (1 / years) - 1 if years > 0 and final > 0 else None,
        "sharpe": mean / sd * math.sqrt(per_year) if sd > 0 else None,
        "max_drawdown": mdd,
    }


def buy_and_hold(bars: Sequence[Bar], settings: RunSettings) -> dict[str, Any]:
    s = float(settings.slippage_bps) / 1e4
    fee = float(settings.taker_fee)
    cap = float(settings.capital_quote)
    if len(bars) < 2:
        raise StrategyError("benchmark needs at least 2 bars.")
    entry = float(bars[1].open) * (1 + s)          # same timing as a strategy that buys on bar 0 close
    qty = cap * float(INVEST_FRACTION) / (entry * (1 + fee))
    cash = cap - qty * entry * (1 + fee)
    equity = [(b.close_time.isoformat(), cash + qty * float(b.close)) for b in bars[1:]]
    last = equity[-1][1] - qty * float(bars[-1].close) * (s + fee)
    equity[-1] = (equity[-1][0], last)
    return _perf(equity, cap, bars[1:]) | {"note": f"{INVEST_FRACTION:.0%} of capital bought at bar 1 open with the same fee and slippage, sold at the last close"}


def run_backtest(strategy: Strategy, bars: Sequence[Bar], *, market: MarketSpec | None = None,
                 settings: RunSettings | None = None, out_dir: str | Path | None = None) -> BacktestResult:
    market = market or MarketSpec()
    settings = settings or RunSettings()
    session_id = uuid4()
    work = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="ct_bt_"))
    work.mkdir(parents=True, exist_ok=True)
    journal_path = work / "journal.db"
    if journal_path.exists():
        raise StrategyError(f"{journal_path} already exists; use a new output folder per run.")
    sid = f"{type(strategy).name}-{type(strategy).version}"
    report, source, journal = run_session(strategy, bars, market=market, settings=settings,
                                          journal_path=journal_path, session_id=session_id, strategy_id=sid)
    try:
        metrics, trades, equity = metrics_from_run(bars, report, settings, journal, session_id, market)
    finally:
        journal.__exit__(None, None, None)
    result = BacktestResult(
        strategy=type(strategy).name, strategy_version=type(strategy).version, symbol=market.symbol,
        timeframe=bars[0].timeframe, start=bars[0].open_time.isoformat(), end=bars[-1].close_time.isoformat(),
        bars=len(bars), settings={k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(settings).items()},
        metrics=metrics, benchmark=buy_and_hold(bars, settings), trades=trades, equity_curve=equity,
        engine={"session_id": str(session_id), "journal": str(journal_path), "engine_version": report.engine_version,
                "book_type": report.book_type, "queue_aware": report.queue_aware,
                "model": "market orders at next bar open +/- slippage, taker fee, Nautilus spot cash"},
        report={k: v for k, v in report.to_dict().items() if k not in {"decisions", "orders", "fills"}},
    )
    if out_dir:
        result.write(work)
    return result
