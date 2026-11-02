"""Durable, hash-chained market tape: the only market input a Paper session ever replays.

Every line is fsynced before anything can depend on it, and each line chains to the previous one.
A torn last line is what a killed writer leaves, so it alone is cut away; any other damage stops
the session. Time on the tape is strictly increasing in whole microseconds, and an event that
arrives late keeps its order at the tape head rather than rewriting history.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from crypto_trader.execution import BookLevel, MarketBookUpdate, MarketTrade
from crypto_trader.paper import FrozenMarketDataFeed, MarketObservation, PaperStateError

__all__ = ["MarketTape", "PublicBook", "PublicTrade"]

_US_NS = 1_000
_GENESIS = "0" * 64


@dataclass(frozen=True)
class PublicBook:
    bid: Decimal
    bid_size: Decimal
    ask: Decimal
    ask_size: Decimal
    received_ns: int


@dataclass(frozen=True)
class PublicTrade:
    trade_id: int
    price: Decimal
    size: Decimal
    ts_ns: int
    seller_aggressor: bool


def _digest(previous: str, body: dict[str, Any]) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{previous}|{text}".encode("utf-8")).hexdigest()


def _floor_us(ts_ns: int) -> int:
    return ts_ns - ts_ns % _US_NS


@dataclass
class MarketTape:
    path: Path
    symbol: str
    book_max_age_ns: int = 30 * 1_000_000_000
    tick_size: Decimal = Decimal("0.01")
    rows: list[dict[str, Any]] = field(default_factory=list)
    last_ts_ns: int = 0
    last_trade_id: int | None = None
    last_book_ns: int = 0
    tip: str = _GENESIS
    counters: dict[str, int] = field(default_factory=dict)
    torn_tail_bytes: int = 0
    _pending: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.path.exists():
            self._load()
        else:
            with self.path.open("xb") as stream:
                stream.flush()
                os.fsync(stream.fileno())

    def _load(self) -> None:
        data = self.path.read_bytes()
        good = 0
        # Bytes after the last newline are an unfinished write; every full line must verify.
        for raw in data.split(b"\n")[:-1]:
            try:
                row = json.loads(raw)
                digest = row.pop("h")
                if _digest(self.tip, row) != digest:
                    raise ValueError("chain")
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise PaperStateError("market tape is damaged before its tail.") from exc
            self._absorb(row)
            self.tip = digest
            good += len(raw) + 1
        if good != len(data):
            self.torn_tail_bytes = len(data) - good
            with self.path.open("r+b") as stream:
                stream.truncate(good)
                stream.flush()
                os.fsync(stream.fileno())

    def _absorb(self, row: dict[str, Any]) -> None:
        self.rows.append(row)
        self.last_ts_ns = max(self.last_ts_ns, row["ts_ns"])
        if row["kind"] == "trade":
            self.last_trade_id = row["id"]
        elif row["kind"] == "book":
            self.last_book_ns = row["ts_ns"]

    def _count(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1

    def _append(self, kind: str, ts_ns: int, **body: Any) -> dict[str, Any]:
        row = {"n": len(self.rows), "kind": kind, "ts_ns": ts_ns, **body}
        digest = _digest(self.tip, row)
        self._pending.append(json.dumps({**row, "h": digest}, sort_keys=True, separators=(",", ":")))
        self.tip = digest
        self._absorb(row)
        return row

    def commit(self) -> None:
        if not self._pending:
            return
        payload = ("\n".join(self._pending) + "\n").encode("utf-8")
        with self.path.open("ab") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        self._pending.clear()

    def _next_ts(self, wanted_ns: int) -> int:
        return max(_floor_us(wanted_ns), self.last_ts_ns + _US_NS)

    def ingest_trades(self, trades: list[PublicTrade]) -> int:
        added = 0
        for trade in sorted(trades, key=lambda item: item.trade_id):
            if self.last_trade_id is not None and trade.trade_id <= self.last_trade_id:
                self._count("duplicate_trades")
                continue
            if self.last_trade_id is not None and trade.trade_id != self.last_trade_id + 1:
                self._count("trade_id_gaps")
            if not all(value.is_finite() and value > 0 for value in (trade.price, trade.size)):
                self._count("invalid_trades")
                continue
            price = self._price(trade.price)
            ts_ns = self._next_ts(trade.ts_ns)
            if ts_ns != _floor_us(trade.ts_ns):
                self._count("late_trades")
            quote = self._last_quote()
            if (ts_ns - self.last_book_ns > self.book_max_age_ns or quote is None
                    or not quote[0] <= price <= quote[1]):
                # A print outside the quote, or one from an outage, proves the book is stale. Quote
                # around the print so an exit can never fill against a price already gone.
                self._count("synthetic_books")
                ask = price + self.tick_size
                self._append("book", ts_ns, bid=str(price), bid_size=str(trade.size),
                             ask=str(ask), ask_size=str(trade.size), synthetic=True)
                ts_ns = self._next_ts(ts_ns + _US_NS)
            self._append("trade", ts_ns, id=trade.trade_id, price=str(price),
                         size=str(trade.size), seller=trade.seller_aggressor)
            added += 1
        return added

    def _price(self, value: Decimal) -> Decimal:
        """Venue strings pad zeros past the tick; a real sub-tick price stops the session."""
        price = value.quantize(self.tick_size)
        if price != value:
            raise PaperStateError("public price is finer than the session tick size.")
        return price

    def _last_quote(self) -> tuple[Decimal, Decimal] | None:
        for row in reversed(self.rows):
            if row["kind"] == "book":
                return Decimal(row["bid"]), Decimal(row["ask"])
        return None

    def ingest_book(self, book: PublicBook) -> int | None:
        values = (book.bid, book.bid_size, book.ask, book.ask_size)
        if not all(value.is_finite() and value > 0 for value in values) or book.bid >= book.ask:
            self._count("invalid_books")
            return None
        ts_ns = self._next_ts(book.received_ns)
        self._append("book", ts_ns, bid=str(self._price(book.bid)), bid_size=str(book.bid_size),
                     ask=str(self._price(book.ask)), ask_size=str(book.ask_size), synthetic=False)
        return ts_ns

    def add_observation(self, ts_ns: int, price: Decimal) -> MarketObservation:
        """Bind a decision point to an existing tape event; it never adds market time."""
        index = sum(1 for row in self.rows if row["kind"] == "obs")
        reference = f"public.book#{index}"
        self._append("obs", ts_ns, ref=reference, price=str(price))
        return self._observation(self.rows[-1])

    def _observation(self, row: dict[str, Any]) -> MarketObservation:
        return MarketObservation(
            symbol=self.symbol, price=Decimal(row["price"]),
            observed_at=self._when(row["ts_ns"]), market_reference=row["ref"],
        )

    @staticmethod
    def _when(ts_ns: int) -> datetime:
        return datetime.fromtimestamp(ts_ns // 1_000_000_000, tz=timezone.utc).replace(
            microsecond=ts_ns % 1_000_000_000 // 1000)

    def observations(self) -> tuple[MarketObservation, ...]:
        return tuple(self._observation(row) for row in self.rows if row["kind"] == "obs")

    def feed(self, observations: tuple[MarketObservation, ...]) -> FrozenMarketDataFeed:
        return FrozenMarketDataFeed(entries=observations, feed_id=f"tape-{self.tip[:16]}")

    def market_events(self) -> tuple[tuple[MarketBookUpdate, ...], tuple[MarketTrade, ...]]:
        """Translate rows to the engine-independent tape: book changes replace the old level."""
        books: list[MarketBookUpdate] = []
        trades: list[MarketTrade] = []
        previous: tuple[Decimal, Decimal, Decimal, Decimal] | None = None
        sequence = 1
        for row in self.rows:
            if row["kind"] == "trade":
                trades.append(MarketTrade(price=Decimal(row["price"]), size=Decimal(row["size"]),
                                          aggressor="SELLER" if row["seller"] else "BUYER",
                                          ts_ns=row["ts_ns"], trade_id=f"A{row['id']}"))
            elif row["kind"] == "book":
                current = (Decimal(row["bid"]), Decimal(row["bid_size"]),
                           Decimal(row["ask"]), Decimal(row["ask_size"]))
                if previous is not None and current[0] == previous[0] and current[2] == previous[2]:
                    # Same prices: keep queue position, and keep this time on the market timeline.
                    books.append(MarketBookUpdate(levels=[
                        BookLevel(side="BUY", price=current[0], size=current[1], order_id=1),
                        BookLevel(side="SELL", price=current[2], size=current[3], order_id=2),
                    ], ts_ns=row["ts_ns"], sequence=sequence, action="UPDATE"))
                    sequence += 2
                    previous = current
                    continue
                if previous is not None:
                    books.append(MarketBookUpdate(levels=[
                        BookLevel(side="BUY", price=previous[0], size=previous[1], order_id=1),
                        BookLevel(side="SELL", price=previous[2], size=previous[3], order_id=2),
                    ], ts_ns=row["ts_ns"], sequence=sequence, action="DELETE"))
                    sequence += 2
                books.append(MarketBookUpdate(levels=[
                    BookLevel(side="BUY", price=current[0], size=current[1], order_id=1),
                    BookLevel(side="SELL", price=current[2], size=current[3], order_id=2),
                ], ts_ns=row["ts_ns"], sequence=sequence))
                sequence += 2
                previous = current
        return tuple(books), tuple(trades)

    def has_event_after(self, ts_ns: int) -> bool:
        return any(row["kind"] in {"book", "trade"} and row["ts_ns"] > ts_ns for row in reversed(self.rows))
