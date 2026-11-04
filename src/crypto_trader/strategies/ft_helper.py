"""Freqtrade signal helper (runs inside a Python that has freqtrade installed; imports nothing from the engine).

Protocol: one JSON object per line on stdin, one JSON reply per line on stdout.
  {"op": "load", "file": "...", "class": "Name"}  -> {"ok": true, "meta": {...}}
  {"op": "bar", "bar": [ts_ms, o, h, l, c, v]}      -> {"ok": true, "enter": 0|1, "exit": 0|1}
On every bar the strategy's advise_* methods run on the history UP TO that bar only, and only the last
row is read, so a strategy that peeks forward cannot leak future data into the signal.
"""
from __future__ import annotations

import importlib.util
import json
import sys


def main() -> int:
    import pandas as pd

    proto = sys.stdout              # protocol channel; strategies that print() must not corrupt it
    sys.stdout = sys.stderr
    strategy = None
    rows: list[list[float]] = []
    startup = 0
    window = 1000
    for line in sys.stdin:
        msg = json.loads(line)
        try:
            if msg["op"] == "load":
                spec = importlib.util.spec_from_file_location("ct_ft_user_strategy", msg["file"])
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                cls = getattr(module, msg["class"])
                config = {"timeframe": None, "stake_currency": "USDT", "dry_run": True, "runmode": "backtest",
                          "trading_mode": "spot", "margin_mode": "", "candle_type_def": "spot"}
                strategy = cls(config)
                startup = int(getattr(strategy, "startup_candle_count", 0) or 0)
                window = int(msg.get("window", 1000))
                meta = {
                    "trailing_stop_positive": getattr(strategy, "trailing_stop_positive", None),
                    "trailing_stop_positive_offset": float(getattr(strategy, "trailing_stop_positive_offset", 0.0) or 0.0),
                    "trailing_only_offset_is_reached": bool(getattr(strategy, "trailing_only_offset_is_reached", False)),
                    "window": window,
                    "timeframe": str(getattr(strategy, "timeframe", "")),
                    "startup_candle_count": startup,
                    "stoploss": float(getattr(strategy, "stoploss", -1.0)),
                    "minimal_roi": {str(k): float(v) for k, v in (getattr(strategy, "minimal_roi", {}) or {}).items()},
                    "trailing_stop": bool(getattr(strategy, "trailing_stop", False)),
                    "can_short": bool(getattr(strategy, "can_short", False)),
                    "use_exit_signal": bool(getattr(strategy, "use_exit_signal", True)),
                    "custom_callbacks": sorted(n for n in ("custom_stoploss", "custom_exit", "custom_stake_amount",
                                                           "adjust_trade_position", "confirm_trade_entry",
                                                           "custom_entry_price", "custom_exit_price")
                                               if getattr(type(strategy), n, None) is not None
                                               and getattr(type(strategy), n).__qualname__.split(".")[0] == cls.__name__),
                    "informative_pairs": bool(strategy.informative_pairs()) if hasattr(strategy, "informative_pairs") else False,
                }
                reply = {"ok": True, "meta": meta}
            elif msg["op"] == "bar":
                rows.append(msg["bar"])
                if len(rows) > window:          # like Freqtrade live: a bounded candle window
                    del rows[: len(rows) - window]
                frame = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
                frame["date"] = pd.to_datetime(frame["date"], unit="ms", utc=True)
                md = {"pair": msg.get("pair", "BTC/USDT")}
                n, last_date = len(frame), frame["date"].iloc[-1]
                frame = strategy.advise_indicators(frame, md)
                frame = strategy.advise_entry(frame, md)
                frame = strategy.advise_exit(frame, md)
                if len(frame) != n or frame["date"].iloc[-1] != last_date:
                    # Freqtrade refuses this too ("mismatching length"): the last row is no longer this bar.
                    raise RuntimeError("strategy changed the dataframe length/order (e.g. dropna); refused")
                last = frame.iloc[-1]
                enter = int(last.get("enter_long", 0) == 1) if "enter_long" in frame else 0
                exit_ = int(last.get("exit_long", 0) == 1) if "exit_long" in frame else 0
                reply = {"ok": True, "enter": enter, "exit": exit_, "rows": len(frame)}
            else:
                reply = {"ok": False, "error": f"unknown op {msg.get('op')!r}"}
        except Exception as exc:  # reported to the engine, which fails closed
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        proto.write(json.dumps(reply) + "\n")
        proto.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
