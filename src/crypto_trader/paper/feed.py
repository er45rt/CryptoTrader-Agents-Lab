"""Public-market-data feeds for shadow/paper sessions (Phase 10).

Only PUBLIC market data is allowed here. The concrete implementations are:

* :class:`FrozenMarketDataFeed` - a frozen, reproducible sequence used for offline paper runs,
  deterministic replay and regression tests (no network).
* :class:`PublicMarketDataFeed` - the live adapter shape for Phase 1 public data. It is driven by
  an injected ``fetch_batch`` callable (the Phase 2/1 public Binance tool), so this module never
  performs network I/O itself and the tests never need the public API.

Nothing here can produce authenticated data: there is no API key, no signature and no order
endpoint anywhere in this package.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol, runtime_checkable

from .errors import PaperConfigError

__all__ = [
    "FrozenMarketDataFeed",
    "MarketDataFeed",
    "MarketObservation",
    "PublicMarketDataFeed",
]


@dataclass(frozen=True)
class MarketObservation:
    """One public market observation (price tick / closed kline)."""

    symbol: str
    price: Decimal
    observed_at: datetime
    market_reference: str
    volume: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.market_reference:
            raise PaperConfigError("a market observation requires a market reference.")
        if not isinstance(self.price, Decimal) or self.price <= 0:
            raise PaperConfigError("a market observation requires a positive Decimal price.")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise PaperConfigError("a market observation requires a timezone-aware timestamp.")
        if self.volume is not None and (not isinstance(self.volume, Decimal) or self.volume < 0):
            raise PaperConfigError("volume must be a non-negative Decimal when present.")

    def normalized_at(self) -> datetime:
        return self.observed_at.astimezone(timezone.utc)


@runtime_checkable
class MarketDataFeed(Protocol):
    """A deterministic, re-readable source of public market observations."""

    def observations(self) -> Sequence[MarketObservation]: ...

    def from_index(self, index: int) -> Sequence[MarketObservation]: ...


@dataclass(frozen=True)
class FrozenMarketDataFeed:
    """A frozen, reproducible public market-data sequence."""

    entries: tuple[MarketObservation, ...] = ()
    feed_id: str = "frozen-public-v1"

    def __post_init__(self) -> None:
        if not self.feed_id:
            raise PaperConfigError("a frozen feed requires a feed_id.")
        previous: datetime | None = None
        for entry in self.entries:
            current = entry.normalized_at()
            if previous is not None and current < previous:
                raise PaperConfigError("frozen feed observations must be in timestamp order.")
            previous = current

    def observations(self) -> Sequence[MarketObservation]:
        return self.entries

    def from_index(self, index: int) -> Sequence[MarketObservation]:
        if not isinstance(index, int) or index < 0:
            raise PaperConfigError("feed index must be a non-negative integer.")
        return self.entries[index:]

    def __len__(self) -> int:
        return len(self.entries)


@dataclass
class PublicMarketDataFeed:
    """Live public-data adapter: the caller injects the public fetch callable.

    ``fetch_batch(symbol, after)`` must return public observations only; the Phase 1 public
    Binance tool is the intended production wiring. Failures surface as :class:`PaperConfigError`
    rather than being swallowed.
    """

    symbol: str
    fetch_batch: Callable[[str, datetime | None], Sequence[MarketObservation]]
    _buffer: list[MarketObservation] = None  # type: ignore[assignment]
    _cursor: int = 0

    def __post_init__(self) -> None:
        if self._buffer is None:
            self._buffer = []

    def observations(self) -> Sequence[MarketObservation]:
        return tuple(self._buffer)

    def from_index(self, index: int) -> Sequence[MarketObservation]:
        if not isinstance(index, int) or index < 0:
            raise PaperConfigError("feed index must be a non-negative integer.")
        return tuple(self._buffer[index:])

    def pull(self, *, after: datetime | None = None) -> int:
        """Pull the next public batch and return how many observations were appended."""
        batch = self.fetch_batch(self.symbol, after)
        for entry in batch:
            if entry.symbol != self.symbol:
                raise PaperConfigError(
                    f"public feed returned {entry.symbol!r} while tracking {self.symbol!r}."
                )
        self._buffer.extend(batch)
        self._cursor += len(batch)
        return len(batch)

    @property
    def cursor(self) -> int:
        return self._cursor
