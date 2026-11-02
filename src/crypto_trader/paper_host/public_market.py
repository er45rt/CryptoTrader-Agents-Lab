"""Read-only public Binance market reader. GET only, no credentials, no redirects, no proxy."""
from __future__ import annotations

import ssl
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter

from .tape import PublicBook, PublicTrade

__all__ = ["BinancePublicReader", "FeedUnavailable", "MarketBatch"]

BASE_URL = "https://api.binance.com"
BOOK_PATH = "/api/v3/ticker/bookTicker"
TRADES_PATH = "/api/v3/aggTrades"
_ALLOWED_PATHS = frozenset({BOOK_PATH, TRADES_PATH})
_PAGE = 1000


class FeedUnavailable(RuntimeError):
    """The public feed cannot serve now; the host keeps state and retries with backoff."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class MarketBatch:
    book: PublicBook | None
    trades: tuple[PublicTrade, ...]


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise FeedUnavailable("public feed returned a malformed number.") from exc


class _SystemTrustAdapter(HTTPAdapter):
    """Verify TLS against the Windows trust store; verification itself is never relaxed."""

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = ssl.create_default_context()
        super().init_poolmanager(*args, **kwargs)


@dataclass
class BinancePublicReader:
    symbol: str
    timeout_seconds: float = 10.0
    max_pages: int = 5
    clock_ns: Callable[[], int] = time.time_ns
    session: Any = None

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
            self.session.trust_env = False  # No proxy, netrc or ambient auth.
            self.session.mount("https://", _SystemTrustAdapter())

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        if path not in _ALLOWED_PATHS:
            raise FeedUnavailable("refusing an unapproved public market path.")
        try:
            response = self.session.get(f"{BASE_URL}{path}", params=params,
                                        timeout=self.timeout_seconds, allow_redirects=False)
        except requests.RequestException as exc:
            raise FeedUnavailable(f"public feed unreachable: {type(exc).__name__}") from exc
        status = response.status_code
        if status != 200:
            retry = response.headers.get("Retry-After") if hasattr(response, "headers") else None
            raise FeedUnavailable(f"public feed status {status}",
                                  float(retry) if retry and str(retry).isdigit() else None)
        try:
            return response.json()
        except ValueError as exc:
            raise FeedUnavailable("public feed returned invalid JSON.") from exc

    def poll(self, *, after_trade_id: int | None) -> MarketBatch:
        trades: list[PublicTrade] = []
        cursor = after_trade_id
        for _ in range(self.max_pages):
            params: dict[str, Any] = {"symbol": self.symbol, "limit": _PAGE}
            if cursor is not None:
                params["fromId"] = cursor + 1
            rows = self._get(TRADES_PATH, params)
            if not isinstance(rows, list):
                raise FeedUnavailable("public trades payload is not a list.")
            for row in rows:
                trades.append(PublicTrade(
                    trade_id=int(row["a"]), price=_decimal(row["p"]), size=_decimal(row["q"]),
                    ts_ns=int(row["T"]) * 1_000_000, seller_aggressor=bool(row["m"]),
                ))
            if len(rows) < _PAGE or cursor is None:
                break
            cursor = trades[-1].trade_id
        ticker = self._get(BOOK_PATH, {"symbol": self.symbol})
        if not isinstance(ticker, dict) or ticker.get("symbol") != self.symbol:
            raise FeedUnavailable("public book payload does not match the symbol.")
        book = PublicBook(
            bid=_decimal(ticker["bidPrice"]), bid_size=_decimal(ticker["bidQty"]),
            ask=_decimal(ticker["askPrice"]), ask_size=_decimal(ticker["askQty"]),
            received_ns=self.clock_ns(),
        )
        return MarketBatch(book=book, trades=tuple(trades))
