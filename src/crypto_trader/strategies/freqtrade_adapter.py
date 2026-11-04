"""Freqtrade IStrategy adapter (D29): run an unmodified Freqtrade strategy file through our engine.

How it works: a helper process (ft_helper.py) runs under a Python that has Freqtrade + its indicator
libraries installed (default: C:\\Users\\Jamal\\ct_trading_pilot\\venv, override with CT_FREQTRADE_PYTHON).
For every closed bar the helper runs the strategy's populate_* methods on the history up to that bar
(bounded window, default 1000 bars, like Freqtrade live) and returns enter_long / exit_long of the LAST row.

SUPPORTED (spot, long only): entry/exit signals, `stoploss`, `minimal_roi`, basic trailing stop
(trailing_stop, trailing_stop_positive, trailing_stop_positive_offset, trailing_only_offset_is_reached).
Stop-loss / ROI / trailing are evaluated on bar CLOSES (Freqtrade's own backtester uses intrabar
high/low), so results differ from `freqtrade backtesting`.

REFUSED (load fails, nothing is guessed): can_short / futures, informative pairs, custom callbacks
(custom_stoploss, custom_exit, custom_stake_amount, adjust_trade_position, confirm_trade_entry,
custom_entry_price, custom_exit_price), and a strategy timeframe we do not support.
Not used: hyperopt spaces (the strategy's current default parameters are used), protections,
order types / time in force (the engine always sends market orders).

Licence: Freqtrade and most community strategies are GPL-3.0; private use is fine, redistribution of a
combined work carries GPL obligations.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .api import TIMEFRAMES, Signal, Strategy, StrategyError
from .loader import LoadedStrategy, _sha256

__all__ = ["load_freqtrade_strategy", "DEFAULT_FT_PYTHON"]

DEFAULT_FT_PYTHON = r"C:\Users\Jamal\ct_trading_pilot\venv\Scripts\python.exe"
HELPER_TIMEOUT_S = float(os.environ.get("CT_FREQTRADE_TIMEOUT", "600"))
_HELPER = Path(__file__).with_name("ft_helper.py")
_UNSUPPORTED_CALLBACKS = {"custom_stoploss", "custom_exit", "custom_stake_amount", "adjust_trade_position",
                          "confirm_trade_entry", "custom_entry_price", "custom_exit_price"}


class _Helper:
    def __init__(self, python: str, file: str, class_name: str, window: int) -> None:
        if not Path(python).is_file():
            raise StrategyError(f"Freqtrade Python not found: {python} (set CT_FREQTRADE_PYTHON).")
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH"}}
        self.proc = subprocess.Popen([python, "-I", "-B", str(_HELPER)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
                                     bufsize=1)
        self.meta = self.call({"op": "load", "file": file, "class": class_name, "window": window})["meta"]

    def call(self, msg: dict[str, Any], timeout: float = HELPER_TIMEOUT_S) -> dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        box: list[str] = []
        reader = threading.Thread(target=lambda: box.append(self.proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            self.proc.kill()
            raise StrategyError(f"Freqtrade helper did not answer within {timeout:.0f}s; killed (fail closed).")
        line = box[0] if box else ""
        if not line:
            raise StrategyError("Freqtrade helper exited unexpectedly.")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise StrategyError(f"Freqtrade strategy error: {reply.get('error')}")
        return reply

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)


def _check_meta(meta: dict[str, Any], class_name: str) -> None:
    problems = []
    if meta["timeframe"] not in TIMEFRAMES:
        problems.append(f"timeframe {meta['timeframe']!r} is not supported")
    if meta["can_short"]:
        problems.append("can_short (shorting/futures) is not supported")
    if meta["informative_pairs"]:
        problems.append("informative pairs (other symbols/timeframes) are not supported")
    bad = sorted(set(meta["custom_callbacks"]) & _UNSUPPORTED_CALLBACKS)
    if bad:
        problems.append(f"custom callbacks not supported: {', '.join(bad)}")
    if problems:
        raise StrategyError(f"Freqtrade strategy {class_name} cannot run in this engine: " + "; ".join(problems))


def load_freqtrade_strategy(spec: str) -> LoadedStrategy:
    """spec = 'path/to/File.py:ClassName' (Freqtrade IStrategy)."""
    file, sep, class_name = spec.rpartition(":")
    if not sep or not file.endswith(".py") or not class_name:
        raise StrategyError("Freqtrade spec must be 'freqtrade:path/to/File.py:ClassName'.")
    path = Path(file)
    if not path.is_file():
        raise StrategyError(f"strategy file not found: {path}")
    python = os.environ.get("CT_FREQTRADE_PYTHON", DEFAULT_FT_PYTHON)
    probe = _Helper(python, str(path.resolve()), class_name, window=1000)
    meta = probe.meta
    probe.close()
    _check_meta(meta, class_name)
    roi = sorted(((int(float(k)), Decimal(str(v))) for k, v in meta["minimal_roi"].items()), key=lambda x: x[0])
    tf = meta["timeframe"]
    tf_minutes = int(TIMEFRAMES[tf].total_seconds() // 60)
    stoploss = Decimal(str(meta["stoploss"]))
    trailing = meta["trailing_stop"]
    t_pos = None if meta["trailing_stop_positive"] is None else Decimal(str(meta["trailing_stop_positive"]))
    t_off = Decimal(str(meta["trailing_stop_positive_offset"]))
    t_only = meta["trailing_only_offset_is_reached"]
    resolved = str(path.resolve())

    class FreqtradeAdapted(Strategy):
        name = f"freqtrade-{class_name}"
        version = _sha256(path)[:12]
        timeframe = tf
        warmup_bars = max(1, int(meta["startup_candle_count"]))
        description = f"Freqtrade {class_name} via adapter (signals per closed bar, exits on closes)"

        def __init__(self, **params: Any) -> None:
            super().__init__(**params)
            self._helper = _Helper(python, resolved, class_name, window=int(params.get("window", 1000)))
            self._fed = 0
            self._long_since: int | None = None    # index of the bar whose close issued the entry
            self._peak: Decimal | None = None
            self.exits: dict[str, int] = {"signal": 0, "stoploss": 0, "roi": 0, "trailing": 0}

        def on_bar(self, bars):
            # feed every bar the helper has not seen yet (warm-up bars included), oldest first
            if self._fed >= len(bars):
                raise StrategyError("adapter called twice for the same bar.")
            reply: dict[str, Any] = {}
            for b in bars.bars[self._fed:]:
                reply = self._helper.call({"op": "bar", "bar": [int(b.open_time.timestamp() * 1000), float(b.open),
                                                                 float(b.high), float(b.low), float(b.close),
                                                                 float(b.volume)]})
                self._fed += 1
            i = len(bars) - 1
            close = bars[i].close
            if self._long_since is None:
                if reply["enter"]:
                    self._long_since = i
                    self._peak = None        # trailing state starts at the ENTRY price, not the signal close
                    return Signal.long(1, "ft:enter_long")
                return Signal.flat("ft:flat")
            entry_i = self._long_since + 1
            if entry_i > i:                           # entry fills at the next bar's open
                return Signal.long(1, "ft:entering")
            entry = bars[entry_i].open
            profit = close / entry - 1
            # peak since entry only (Sol D29 round 1): entry price, then post-entry closes
            self._peak = max(entry if self._peak is None else self._peak, close)
            minutes = (i - entry_i + 1) * tf_minutes
            reason = None
            # Freqtrade stop semantics, evaluated on closes:
            #   no trailing                -> stop = entry * (1 + stoploss)
            #   trailing, offset reached    -> stop = peak * (1 - trailing_stop_positive)   (if set)
            #   trailing, not offset-only   -> stop = peak * (1 + stoploss)
            stop = entry * (1 + stoploss)
            kind = "stoploss"
            peak_profit = self._peak / entry - 1
            if trailing:
                if t_pos is not None and peak_profit > t_off:
                    stop, kind = max(stop, self._peak * (1 - t_pos)), "trailing"
                elif not t_only:
                    stop, kind = max(stop, self._peak * (1 + stoploss)), "trailing"
            if close <= stop:
                reason = kind
            if reason is None:
                for after, need in reversed(roi):
                    if minutes >= after:
                        if profit >= need:
                            reason = "roi"
                        break
            if reason is None and meta["use_exit_signal"] and reply["exit"]:
                reason = "signal"
            if reason:
                self.exits[reason] += 1
                self._long_since = None
                return Signal.flat(f"ft:exit:{reason}")
            return Signal.long(1, "ft:hold_long")

        def __del__(self) -> None:
            try:
                self._helper.close()
            except Exception:
                pass

    FreqtradeAdapted.__name__ = f"Freqtrade_{class_name}"
    FreqtradeAdapted.validate_class()
    return LoadedStrategy(spec=f"freqtrade:{spec}", cls=FreqtradeAdapted, source_path=resolved,
                          source_sha256=_sha256(path))
