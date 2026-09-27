"""Engine-independent market state providers (Phase 6).

Freshness originates from the ORIGINAL market observation. Nothing here resets age when an LLM
finishes or when an intent is created: those timestamps are carried, never re-derived.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from .errors import MarketStateError

__all__ = [
    "LiveMarketStateProvider",
    "MarketObservation",
    "MarketStateProvider",
    "ReplayMarketStateProvider",
]


@dataclass(frozen=True)
class MarketObservation:
    """One market observation with its own timestamps and reference."""

    symbol: str
    price: Decimal
    market_event_ts: datetime
    local_ingest_ts: datetime
    market_reference: str
    sequence: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("market_event_ts", "local_ingest_ts"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise MarketStateError(f"{field_name} must be timezone-aware.")
        if not isinstance(self.price, Decimal) or self.price <= 0:
            raise MarketStateError("market observation price must be a positive Decimal.")
        if not self.market_reference:
            raise MarketStateError("market observation requires a reference.")
        if self.local_ingest_ts < self.market_event_ts:
            raise MarketStateError("local_ingest_ts cannot precede market_event_ts.")


@runtime_checkable
class MarketStateProvider(Protocol):
    """The single interface validity/risk code is allowed to depend on."""

    def latest(self, symbol: str) -> MarketObservation | None: ...


class ReplayMarketStateProvider:
    """Deterministic replay of a frozen observation sequence (recorded sessions and tests)."""

    def __init__(self, observations: Sequence[MarketObservation]) -> None:
        self._by_symbol: dict[str, list[MarketObservation]] = {}
        for observation in observations:
            self._by_symbol.setdefault(observation.symbol, []).append(observation)
        self._index: dict[str, int] = {symbol: 0 for symbol in self._by_symbol}

    def latest(self, symbol: str) -> MarketObservation | None:
        series = self._by_symbol.get(symbol)
        if not series:
            return None
        position = self._index.get(symbol, 0)
        if position >= len(series):
            return None
        return series[position]

    def advance(self, symbol: str) -> MarketObservation | None:
        """Advance one replay position; exhaustion is explicit instead of repeating the tail."""
        series = self._by_symbol.get(symbol)
        if not series:
            return None
        position = self._index.get(symbol, 0) + 1
        self._index[symbol] = position
        if position >= len(series):
            return None
        return series[position]


class LiveMarketStateProvider:
    """Thin injectable wrapper around a live feed.

    The feed is supplied by the caller (adapter/websocket/cache). This layer performs no
    networking itself, which keeps it testable and offline by default.
    """

    def __init__(self, feed: Callable[[str], MarketObservation | None]) -> None:
        self._feed = feed

    def latest(self, symbol: str) -> MarketObservation | None:
        return self._feed(symbol)
