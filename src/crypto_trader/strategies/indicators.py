"""Built-in causal indicators. Every value at index i uses only bars 0..i (no look-ahead).

Values before an indicator has enough data are float('nan'). All functions take a BarHistory
(or anything with open/high/low/close/volume float tuples) and return a tuple aligned to the bars.
"""
from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .api import StrategyError

NaN = float("nan")


def _require_period(period: Any) -> int:
    if not isinstance(period, int) or period < 1:
        raise StrategyError("indicator period must be a positive int.")
    return period


def _source(bars: Any, source: str) -> tuple[float, ...]:
    return tuple(getattr(bars, source))


def sma(bars: Any, period: int, source: str = "close") -> tuple[float, ...]:
    period = _require_period(period)
    values = _source(bars, source)
    out: list[float] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        out.append(total / period if i >= period - 1 else NaN)
    return tuple(out)


def ema(bars: Any, period: int, source: str = "close") -> tuple[float, ...]:
    period = _require_period(period)
    values = _source(bars, source)
    alpha = 2.0 / (period + 1)
    out: list[float] = []
    current = NaN
    for i, v in enumerate(values):
        if i == period - 1:
            current = sum(values[:period]) / period
        elif i >= period:
            current = alpha * v + (1 - alpha) * current
        out.append(current if i >= period - 1 else NaN)
    return tuple(out)


def rsi(bars: Any, period: int = 14, source: str = "close") -> tuple[float, ...]:
    """Wilder's RSI."""
    period = _require_period(period)
    values = _source(bars, source)
    out: list[float] = [NaN] * len(values)
    if len(values) <= period:
        return tuple(out)
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = value(avg_gain, avg_loss)
    return tuple(out)


def true_range(bars: Any) -> tuple[float, ...]:
    high, low, close = bars.high, bars.low, bars.close
    out: list[float] = []
    for i in range(len(close)):
        if i == 0:
            out.append(high[0] - low[0])
        else:
            out.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    return tuple(out)


def atr(bars: Any, period: int = 14) -> tuple[float, ...]:
    """Wilder's ATR."""
    period = _require_period(period)
    tr = true_range(bars)
    out: list[float] = [NaN] * len(tr)
    if len(tr) < period:
        return tuple(out)
    current = sum(tr[:period]) / period
    out[period - 1] = current
    for i in range(period, len(tr)):
        current = (current * (period - 1) + tr[i]) / period
        out[i] = current
    return tuple(out)


def donchian_high(bars: Any, period: int) -> tuple[float, ...]:
    period = _require_period(period)
    high = bars.high
    return tuple(max(high[i - period + 1:i + 1]) if i >= period - 1 else NaN for i in range(len(high)))


def donchian_low(bars: Any, period: int) -> tuple[float, ...]:
    period = _require_period(period)
    low = bars.low
    return tuple(min(low[i - period + 1:i + 1]) if i >= period - 1 else NaN for i in range(len(low)))


def stdev(bars: Any, period: int, source: str = "close") -> tuple[float, ...]:
    period = _require_period(period)
    values = _source(bars, source)
    out: list[float] = []
    for i in range(len(values)):
        if i < period - 1:
            out.append(NaN)
            continue
        window = values[i - period + 1:i + 1]
        mean = sum(window) / period
        out.append(math.sqrt(sum((x - mean) ** 2 for x in window) / period))
    return tuple(out)


def bollinger_upper(bars: Any, period: int = 20, k: float = 2.0) -> tuple[float, ...]:
    m, s = sma(bars, period), stdev(bars, period)
    return tuple(a + k * b for a, b in zip(m, s))


def bollinger_lower(bars: Any, period: int = 20, k: float = 2.0) -> tuple[float, ...]:
    m, s = sma(bars, period), stdev(bars, period)
    return tuple(a - k * b for a, b in zip(m, s))


def macd(bars: Any, fast: int = 12, slow: int = 26) -> tuple[float, ...]:
    f, s = ema(bars, fast), ema(bars, slow)
    return tuple(a - b for a, b in zip(f, s))


def macd_signal(bars: Any, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, ...]:
    line = macd(bars, fast, slow)
    first = next((i for i, v in enumerate(line) if not math.isnan(v)), len(line))

    class _Line:
        close = line[first:]

    tail = ema(_Line, signal)
    return tuple([NaN] * first) + tail


def returns(bars: Any, period: int = 1) -> tuple[float, ...]:
    period = _require_period(period)
    close = bars.close
    return tuple(close[i] / close[i - period] - 1 if i >= period else NaN for i in range(len(close)))


REGISTRY: dict[str, Callable[..., tuple[float, ...]]] = {
    "sma": sma, "ema": ema, "rsi": rsi, "atr": atr, "true_range": true_range,
    "donchian_high": donchian_high, "donchian_low": donchian_low, "stdev": stdev,
    "bollinger_upper": bollinger_upper, "bollinger_lower": bollinger_lower,
    "macd": macd, "macd_signal": macd_signal, "returns": returns,
}
# The built-ins proven causal by the test suite. Only these may be computed once over a whole run and
# sliced (api._SharedSeries); anything added to REGISTRY later is recomputed on the visible bars only.
CAUSAL_BUILTINS: Mapping[str, Callable[..., tuple[float, ...]]] = MappingProxyType(dict(REGISTRY))


def compute(name: str, bars: Any, *args: Any, **kwargs: Any) -> tuple[float, ...]:
    try:
        fn = REGISTRY[name]
    except KeyError:
        raise StrategyError(f"unknown indicator {name!r}; available: {sorted(REGISTRY)}") from None
    return fn(bars, *args, **kwargs)
