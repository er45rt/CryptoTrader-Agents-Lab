"""Live PAPER trading for plug-in strategies (D29). No credentials, no order endpoint, nothing is sent.

A session folder holds everything; the process can be killed and restarted at any time:
  session.json   sealed at creation: strategy spec + sha256, market, settings, start time, session id
  bars.jsonl     append-only CLOSED klines from Binance's public endpoint (warm-up history + live bars)
  journal.db     the engine journal (idempotent: every cycle replays the same bars, only new events land)
  status.json    last cycle: equity, position, last signal, counts, timestamps
  report.json    the engine RunReport of the last cycle
  STOP           create this file to stop the loop

Each cycle: fetch newly closed bars -> append -> rebuild the simulator -> replay ALL bars through the
unchanged TradingSession (bars closed before the session start only warm the strategy up and never
trade) -> write status. Fills use the same model as the backtest (next bar open +/- slippage, taker fee).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from .api import TIMEFRAMES, Bar, StrategyError
from .loader import load_strategy
from .ohlcv import bars_from_rows, check_bars, fetch_forming_open, fetch_public_klines
from .runtime import MarketSpec, RunSettings, metrics_from_run, run_session

__all__ = ["create_paper_session", "paper_cycle", "run_paper_loop"]


def _write_atomic(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def _dec(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _dec(v) for k, v in obj.items()}
    return obj


def create_paper_session(folder: str | Path, *, strategy_spec: str, market: MarketSpec, settings: RunSettings,
                         params: dict[str, Any] | None = None, now: datetime | None = None) -> Path:
    folder = Path(folder)
    if (folder / "session.json").exists():
        raise StrategyError(f"{folder} already holds a session; use a new folder.")
    loaded = load_strategy(strategy_spec)
    now = now or datetime.now(timezone.utc)
    folder.mkdir(parents=True, exist_ok=True)
    doc = {
        "session_id": str(uuid4()), "created_at": now.isoformat(), "start": now.isoformat(),
        "mode": "PAPER (simulated; no credentials, no orders sent)",
        "strategy_spec": strategy_spec, "strategy_sha256": loaded.source_sha256,
        "strategy_name": loaded.cls.name, "timeframe": loaded.cls.timeframe, "warmup_bars": loaded.cls.warmup_bars,
        "params": params or {}, "market": _dec(asdict(market)), "settings": _dec(asdict(settings)),
    }
    _write_atomic(folder / "session.json", json.dumps(doc, indent=1))
    (folder / "bars.jsonl").touch()
    return folder


def _load(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "session.json").read_text(encoding="utf-8"))


def _read_bars(folder: Path, timeframe: str) -> tuple[list[Bar], list[datetime | None]]:
    path = folder / "bars.jsonl"
    text = path.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.strip()]
    rows = []
    for n, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except ValueError:
            if n != len(lines) - 1:
                raise StrategyError(f"bars.jsonl is corrupt at line {n + 1} (not the tail); refusing to continue.")
            # a crash during the last append left a partial line: drop exactly that line, durably
            _write_atomic(path, "".join(l + "\n" for l in lines[:-1]))
            break
    seen = [datetime.fromisoformat(r["seen_at"]) if r.get("seen_at") else None for r in rows]
    return bars_from_rows(rows, timeframe), seen


#: Fills are priced at the next bar's OPEN, which is only honest if the bar was seen promptly after it
#: closed. A bar is tradable only if first seen strictly within min(one bar, MAX_SIGNAL_LATENCY) of its
#: close (Sol D29 round 1: the boundary is strict). Later bars (process down, network blocked) are
#: recorded as missed and never traded.
MAX_SIGNAL_LATENCY = timedelta(minutes=5)


def _tradable(bars: list[Bar], seen: list[datetime | None]) -> list[bool]:
    out = []
    for b, s in zip(bars, seen):
        window = min(b.close_time - b.open_time, MAX_SIGNAL_LATENCY)
        out.append(s is not None and b.close_time <= s < b.close_time + window)   # unknown = not tradable
    return out


def _market(doc: dict[str, Any]) -> MarketSpec:
    m = doc["market"]
    return MarketSpec(symbol=m["symbol"], base_currency=m["base_currency"], quote_currency=m["quote_currency"],
                      tick_size=Decimal(m["tick_size"]), quantity_step=Decimal(m["quantity_step"]),
                      price_precision=int(m["price_precision"]), size_precision=int(m["size_precision"]),
                      min_order_quote=Decimal(m["min_order_quote"]))


def _settings(doc: dict[str, Any]) -> RunSettings:
    s = doc["settings"]
    opt = lambda k: None if s.get(k) in (None, "None") else Decimal(s[k])  # noqa: E731
    return RunSettings(capital_quote=Decimal(s["capital_quote"]), max_position_quote=opt("max_position_quote"),
                       taker_fee=Decimal(s["taker_fee"]), maker_fee=Decimal(s["maker_fee"]),
                       slippage_bps=Decimal(s["slippage_bps"]), loss_limit_quote=opt("loss_limit_quote"),
                       max_drawdown_pct=Decimal(s["max_drawdown_pct"]), risk_headroom=Decimal(s["risk_headroom"]),
                       risk_overrides=dict(s.get("risk_overrides") or {}))


def paper_cycle(folder: str | Path, *, fetch: Callable[..., list[Bar]] = fetch_public_klines,
                fetch_open: Callable[..., Any] = fetch_forming_open,
                now: datetime | None = None, cafile: str | None = None) -> dict[str, Any]:
    """One crash-safe cycle. Returns the status written to status.json."""
    folder = Path(folder)
    doc = _load(folder)
    loaded = load_strategy(doc["strategy_spec"])
    if loaded.source_sha256 != doc["strategy_sha256"]:
        raise StrategyError("strategy file changed since the session was created; start a new session.")
    tf = doc["timeframe"]
    step = TIMEFRAMES[tf]
    now = now or datetime.now(timezone.utc)
    start = datetime.fromisoformat(doc["start"])
    market = _market(doc)
    settings = _settings(doc)
    bars, seen = _read_bars(folder, tf)
    since = bars[-1].open_time + step if bars else start - step * (doc["warmup_bars"] + 5)
    new = [b for b in fetch(market.symbol, tf, start=since, now=now, cafile=cafile) if b.close_time <= now]
    if bars:
        new = [b for b in new if b.open_time > bars[-1].open_time]
    if new:
        with (folder / "bars.jsonl").open("a", encoding="utf-8") as fh:
            for b in new:
                fh.write(json.dumps({"date": b.open_time.isoformat(), "open": str(b.open), "high": str(b.high),
                                     "low": str(b.low), "close": str(b.close), "volume": str(b.volume),
                                     "seen_at": now.isoformat()}) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        bars = bars + new
        seen = seen + [now] * len(new)
    status: dict[str, Any] = {"cycle_at": now.isoformat(), "bars": len(bars), "new_bars": len(new),
                              "mode": doc["mode"], "strategy": doc["strategy_name"]}
    if bars:
        check_bars(bars, allow_gaps=True)
        trade_from = next((i for i, b in enumerate(bars) if b.close_time > start), len(bars))
        status["live_bars"] = len(bars) - trade_from
        forming = fetch_open(market.symbol, tf, now=now, cafile=cafile)
        if forming is not None and forming[0] != bars[-1].close_time:
            forming = None          # a gap: the last decision waits until the next cycle
        status["next_open"] = None if forming is None else [forming[0].isoformat(), str(forming[1])]
        if len(bars) - trade_from > 0:
            sid = UUID(doc["session_id"])
            strategy = loaded.instantiate(**doc["params"])
            report, source, journal = run_session(
                strategy, bars, market=market, settings=settings, journal_path=folder / "journal.db",
                session_id=sid, strategy_id=f"{doc['strategy_name']}-paper", trade_from_index=trade_from,
                next_open=forming, tradable=_tradable(bars, seen))
            status["missed_bars"] = sum(1 for i, ok in enumerate(_tradable(bars, seen)) if i >= trade_from and not ok)
            try:
                metrics, trades, equity = metrics_from_run(bars[trade_from:], report, settings, journal, sid, market)
            finally:
                journal.__exit__(None, None, None)
            _write_atomic(folder / "report.json", report.to_json())
            status |= {"equity_quote": metrics["final_equity_quote"], "net_return": metrics["net_return"],
                       "max_drawdown": metrics["max_drawdown"], "fills": metrics["fills"],
                       "position": {k: str(v) for k, v in report.positions.items()},
                       "risk_counts": metrics["risk_counts"], "validity_counts": metrics["validity_counts"],
                       "safety_blocked": metrics["safety_blocked"],
                       "last_signal": source.signals[-1] if source.signals else None,
                       "last_bar_close": bars[-1].close_time.isoformat(), "trades": trades[-10:]}
    _write_atomic(folder / "status.json", json.dumps(status, indent=1, default=str))
    return status


def run_paper_loop(folder: str | Path, *, poll_seconds: float = 60.0, max_cycles: int | None = None,
                   cafile: str | None = None, log: Callable[[str], None] = print) -> None:
    folder = Path(folder)
    cycles = 0
    backoff = poll_seconds
    while not (folder / "STOP").exists():
        try:
            st = paper_cycle(folder, cafile=cafile)
            log(f"[{st['cycle_at']}] bars={st['bars']} new={st['new_bars']} live={st.get('live_bars', 0)} "
                f"equity={st.get('equity_quote')} fills={st.get('fills')} position={st.get('position')}")
            backoff = poll_seconds
        except StrategyError:
            raise
        except Exception as exc:   # network trouble: keep state, retry with backoff (never invent data)
            log(f"cycle failed, will retry: {type(exc).__name__}: {exc}")
            failures = 1
            err_path = folder / "last_error.json"
            if err_path.exists():
                try:
                    failures = int(json.loads(err_path.read_text(encoding="utf-8")).get("consecutive", 0)) + 1
                except (ValueError, OSError):
                    failures = 1
            _write_atomic(err_path, json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                                "error": f"{type(exc).__name__}: {exc}",
                                                "consecutive": failures}, indent=1))
            backoff = min(backoff * 2, 900)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
            time.sleep(backoff)
            continue
        (folder / "last_error.json").unlink(missing_ok=True)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        time.sleep(backoff)
    log("STOP file found; paper loop stopped.")
