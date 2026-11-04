"""Strategy plug-in contract (D29).

A strategy is a plain Python class. It never sees the engine, the journal, the risk layer or the
exchange: it receives a read-only window of CLOSED OHLCV bars and returns a target exposure. The
engine turns that target into orders and sends them through the unchanged pipeline
(decision -> validity -> hard risk -> safety -> simulated or real execution).

Contract (all a strategy author needs):

    from crypto_trader.strategies import Strategy, Signal

    class MyStrategy(Strategy):
        name = "my-strategy"
        version = "1"
        timeframe = "4h"          # bar size the strategy is written for
        warmup_bars = 50          # bars needed before the first decision

        def on_bar(self, bars) -> Signal:
            fast = bars.indicator("sma", 10)
            slow = bars.indicator("sma", 30)
            return Signal.long(1) if fast[-1] > slow[-1] else Signal.flat()

`Signal.target` is the wanted long exposure as a fraction (0 .. 1) of the session's maximum
position size. Spot, long only: no shorting, no leverage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, ClassVar, Mapping, Sequence

__all__ = ["Bar", "BarHistory", "Signal", "Strategy", "StrategyError", "TIMEFRAMES"]

TIMEFRAMES: Mapping[str, timedelta] = {
    "1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30), "1h": timedelta(hours=1), "2h": timedelta(hours=2),
    "4h": timedelta(hours=4), "6h": timedelta(hours=6), "8h": timedelta(hours=8),
    "12h": timedelta(hours=12), "1d": timedelta(days=1),
}


class StrategyError(Exception):
    """A strategy, its data or its output violates the plug-in contract."""


@dataclass(frozen=True)
class Bar:
    """One CLOSED candle. `open_time` is the candle start; it is only visible after `close_time`."""

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    timeframe: str

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAMES:
            raise StrategyError(f"unsupported timeframe {self.timeframe!r}.")
        if self.open_time.tzinfo is None:
            raise StrategyError("bar open_time must be timezone-aware.")
        values = (self.open, self.high, self.low, self.close)
        if any(not isinstance(v, Decimal) or not v.is_finite() or v <= 0 for v in values):
            raise StrategyError("bar prices must be positive finite Decimals.")
        if not isinstance(self.volume, Decimal) or not self.volume.is_finite() or self.volume < 0:
            raise StrategyError("bar volume must be a non-negative finite Decimal.")
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise StrategyError(f"inconsistent OHLC at {self.open_time.isoformat()}.")

    @property
    def close_time(self) -> datetime:
        return self.open_time + TIMEFRAMES[self.timeframe]


class BarHistory:
    """Read-only, look-ahead-free view: only bars closed at or before `as_of`, oldest first."""

    def __init__(self, bars: Sequence[Bar], as_of: datetime) -> None:
        visible = tuple(b for b in bars if b.close_time <= as_of)
        if len(visible) != len(bars):
            raise StrategyError("BarHistory was given a bar that is not closed yet (look-ahead).")
        self._bars = visible
        self.as_of = as_of
        self._cache: dict[tuple[Any, ...], tuple[float, ...]] = {}
        self._shared: _SharedSeries | None = None

    @classmethod
    def _view(cls, shared: "_SharedSeries", n: int, as_of: datetime) -> "BarHistory":
        """Engine-internal fast path: the first n bars of a pre-checked, strictly increasing series.

        Built-in indicators are computed once over the whole series and sliced to n. That is exact only
        because every built-in indicator is causal (value i depends on bars 0..i), which the test suite
        proves indicator by indicator; strategy code still only ever sees bars 0..n-1.
        """
        if not 1 <= n <= len(shared.bars) or shared.bars[n - 1].close_time > as_of:
            raise StrategyError("BarHistory view would expose a bar that is not closed yet (look-ahead).")
        obj = cls.__new__(cls)
        obj._bars = shared.bars[:n]
        obj.as_of = as_of
        obj._cache = {}
        obj._shared = shared
        return obj

    def __len__(self) -> int:
        return len(self._bars)

    def __getitem__(self, index: int) -> Bar:
        return self._bars[index]

    @property
    def bars(self) -> tuple[Bar, ...]:
        return self._bars

    def series(self, field_name: str) -> tuple[float, ...]:
        if field_name not in {"open", "high", "low", "close", "volume"}:
            raise StrategyError(f"unknown OHLCV field {field_name!r}.")
        key = ("field", field_name)
        if key not in self._cache:
            if self._shared is not None:
                self._cache[key] = self._shared.array(field_name)[: len(self._bars)]
            else:
                self._cache[key] = tuple(float(getattr(b, field_name)) for b in self._bars)
        return self._cache[key]

    @property
    def open(self) -> tuple[float, ...]:
        return self.series("open")

    @property
    def high(self) -> tuple[float, ...]:
        return self.series("high")

    @property
    def low(self) -> tuple[float, ...]:
        return self.series("low")

    @property
    def close(self) -> tuple[float, ...]:
        return self.series("close")

    @property
    def volume(self) -> tuple[float, ...]:
        return self.series("volume")

    def frame(self) -> Any:
        """A pandas DataFrame (date, open, high, low, close, volume) of the visible bars."""
        import pandas as pd

        return pd.DataFrame({
            "date": [b.open_time for b in self._bars],
            "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "volume": self.volume,
        })

    def indicator(self, name: str, *args: Any, **kwargs: Any) -> tuple[float, ...]:
        """Compute a built-in causal indicator over the visible bars (see strategies.indicators)."""
        from . import indicators

        key = (name, args, tuple(sorted(kwargs.items())))
        if key not in self._cache:
            if self._shared is not None and self._shared.shareable(name):
                self._cache[key] = self._shared.indicator(key, name, args, kwargs)[: len(self._bars)]
            else:
                self._cache[key] = indicators.compute(name, self, *args, **kwargs)
        return self._cache[key]


class _SharedSeries:
    """One float array per OHLCV field and one result per built-in indicator, for a whole run."""

    def __init__(self, bars: Sequence[Bar]) -> None:
        self.bars = tuple(bars)
        for prev, cur in zip(self.bars, self.bars[1:]):
            if cur.open_time <= prev.open_time:
                raise StrategyError("bars must be strictly increasing.")
        self._arrays: dict[str, tuple[float, ...]] = {}
        self._ind: dict[tuple[Any, ...], tuple[float, ...]] = {}

    def array(self, field_name: str) -> tuple[float, ...]:
        if field_name not in self._arrays:
            self._arrays[field_name] = tuple(float(getattr(b, field_name)) for b in self.bars)
        return self._arrays[field_name]

    def indicator(self, key: tuple[Any, ...], name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[float, ...]:
        if key not in self._ind:
            from . import indicators

            full = BarHistory._view(self, len(self.bars), self.bars[-1].close_time)
            self._ind[key] = indicators.compute(name, full, *args, **kwargs)
        return self._ind[key]

    @staticmethod
    def shareable(name: str) -> bool:
        from . import indicators

        fn = indicators.REGISTRY.get(name)
        return fn is not None and fn is indicators.CAUSAL_BUILTINS.get(name)


@dataclass(frozen=True)
class Signal:
    """The strategy's wish for this bar: a target long exposure in [0, 1]."""

    target: Decimal
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.target, Decimal) or not self.target.is_finite():
            raise StrategyError("signal target must be a finite Decimal.")
        if self.target < 0 or self.target > 1:
            raise StrategyError("signal target must be within [0, 1] (spot, long only, no leverage).")

    @classmethod
    def long(cls, fraction: float | str | Decimal = 1, reason: str = "long") -> "Signal":
        return cls(Decimal(str(fraction)), reason)

    @classmethod
    def flat(cls, reason: str = "flat") -> "Signal":
        return cls(Decimal("0"), reason)


class Strategy:
    """Base class for plug-in strategies. Subclass it and implement `on_bar`."""

    name: ClassVar[str] = ""
    version: ClassVar[str] = "1"
    timeframe: ClassVar[str] = "1h"
    warmup_bars: ClassVar[int] = 1
    description: ClassVar[str] = ""

    def __init__(self, **params: Any) -> None:
        self.params: dict[str, Any] = dict(params)

    def on_bar(self, bars: BarHistory) -> Signal | None:  # pragma: no cover - abstract
        """Return a Signal for the newest closed bar, or None to keep the current position."""
        raise NotImplementedError

    @classmethod
    def validate_class(cls) -> None:
        if not cls.name or not isinstance(cls.name, str):
            raise StrategyError(f"{cls.__name__}: strategy `name` is required.")
        if cls.timeframe not in TIMEFRAMES:
            raise StrategyError(f"{cls.__name__}: unsupported timeframe {cls.timeframe!r}.")
        if not isinstance(cls.warmup_bars, int) or cls.warmup_bars < 1:
            raise StrategyError(f"{cls.__name__}: warmup_bars must be a positive int.")
        if cls.on_bar is Strategy.on_bar:
            raise StrategyError(f"{cls.__name__}: on_bar is not implemented.")


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise StrategyError("timestamps must be timezone-aware.")
    return dt.astimezone(timezone.utc)
