"""Command line for the strategy layer:  python -m crypto_trader.strategies <command> ...

  list      <folder>                                   list installable strategies in a folder
  check     <spec>                                     load + validate one strategy (no data, no trading)
  backtest  <spec> --data FILE [--engine] ...          fast preview (default) or full engine backtest
  paper-new <folder> <spec> --symbol BTCUSDT ...       create a live PAPER session (nothing is sent)
  paper-run <folder> [--once] [--poll 60]              run / resume it (stop: create <folder>/STOP)
  paper-status <folder>                                print the last status

Specs: 'file.py', 'file.py:ClassName', 'package.module:ClassName', 'freqtrade:File.py:ClassName'.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .api import StrategyError
from .loader import list_strategies, load_strategy
from .runtime import MarketSpec, RunSettings

SEALED_FROM = "2025-09-07T00:00:00+00:00"   # project rule: research data must stop before the sealed period


def _dt(text: str | None) -> datetime | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _params(items: list[str]) -> dict:
    out = {}
    for item in items or []:
        k, _, v = item.partition("=")
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def _settings(a: argparse.Namespace) -> RunSettings:
    return RunSettings(capital_quote=Decimal(a.capital), taker_fee=Decimal(a.fee), maker_fee=Decimal(a.fee),
                       slippage_bps=Decimal(a.slippage_bps),
                       max_position_quote=Decimal(a.max_position) if a.max_position else None,
                       risk_overrides=_params(a.risk))


def _market(a: argparse.Namespace) -> MarketSpec:
    return MarketSpec(symbol=a.symbol, base_currency=a.base, quote_currency=a.quote, tick_size=Decimal(a.tick),
                      quantity_step=Decimal(a.step), price_precision=a.price_precision,
                      size_precision=a.size_precision, min_order_quote=Decimal(a.min_order))


def _add_market(p: argparse.ArgumentParser) -> None:
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--base", default="BTC")
    p.add_argument("--quote", default="USDT")
    p.add_argument("--tick", default="0.01")
    p.add_argument("--step", default="0.00001")
    p.add_argument("--price-precision", type=int, default=2)
    p.add_argument("--size-precision", type=int, default=5)
    p.add_argument("--min-order", default="5")
    p.add_argument("--capital", default="1000")
    p.add_argument("--fee", default="0.001", help="taker fee rate per side (0.001 = 0.10%%)")
    p.add_argument("--slippage-bps", default="5")
    p.add_argument("--max-position", default=None, help="hard cap on position size in quote")
    p.add_argument("--risk", nargs="*", default=[], help="engine risk overrides key=value")
    p.add_argument("--param", nargs="*", default=[], help="strategy parameters key=value")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m crypto_trader.strategies")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("folder")
    p = sub.add_parser("check"); p.add_argument("spec")
    p = sub.add_parser("backtest"); p.add_argument("spec"); p.add_argument("--data", required=True)
    p.add_argument("--start"); p.add_argument("--end"); p.add_argument("--engine", action="store_true")
    p.add_argument("--out", help="folder for backtest_result.json (+ journal.db with --engine)")
    p.add_argument("--allow-sealed", action="store_true", help="allow data at/after the project's sealed date")
    _add_market(p)
    p = sub.add_parser("paper-new"); p.add_argument("folder"); p.add_argument("spec"); _add_market(p)
    p = sub.add_parser("paper-run"); p.add_argument("folder"); p.add_argument("--once", action="store_true")
    p.add_argument("--poll", type=float, default=60.0); p.add_argument("--cafile")
    p = sub.add_parser("paper-status"); p.add_argument("folder")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            print(json.dumps(list_strategies(a.folder), indent=1))
        elif a.cmd == "check":
            L = load_strategy(a.spec)
            print(json.dumps({"ok": True, "name": L.cls.name, "version": L.cls.version, "timeframe": L.cls.timeframe,
                              "warmup_bars": L.cls.warmup_bars, "sha256": L.source_sha256}, indent=1))
        elif a.cmd == "backtest":
            from .ohlcv import load_bars
            L = load_strategy(a.spec)
            not_after = None if a.allow_sealed else _dt(SEALED_FROM)
            bars = load_bars(a.data, L.cls.timeframe, start=_dt(a.start), end=_dt(a.end), not_after=not_after,
                             allow_gaps=True)
            strategy = L.instantiate(**_params(a.param))
            if a.engine:
                from .runtime import run_backtest
                r = run_backtest(strategy, bars, market=_market(a), settings=_settings(a), out_dir=a.out)
            else:
                from .fast import run_fast_backtest
                r = run_fast_backtest(strategy, bars, market=_market(a), settings=_settings(a))
                if a.out:
                    r.write(a.out)
            print(json.dumps({"strategy": r.strategy, "bars": r.bars, "start": r.start, "end": r.end,
                              "metrics": r.metrics, "benchmark_buy_and_hold": r.benchmark,
                              "trades": len(r.trades)}, indent=1, default=str))
        elif a.cmd == "paper-new":
            from .paper_live import create_paper_session
            create_paper_session(a.folder, strategy_spec=a.spec, market=_market(a), settings=_settings(a),
                                 params=_params(a.param))
            print(f"PAPER session created in {a.folder} (no credentials, no orders sent).")
        elif a.cmd == "paper-run":
            from .paper_live import paper_cycle, run_paper_loop
            if a.once:
                print(json.dumps(paper_cycle(a.folder, cafile=a.cafile), indent=1, default=str))
            else:
                run_paper_loop(a.folder, poll_seconds=a.poll, cafile=a.cafile)
        elif a.cmd == "paper-status":
            print((Path(a.folder) / "status.json").read_text(encoding="utf-8"))
    except StrategyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
