"""Bounded read-only Binance Spot Testnet @ticker -> host market quote.

One TLS WebSocket connection, one frame, no retries, and no order-capable client.
Venue event time E comes from the same frame as bid/ask, never from local receipt.
This source does not authorize an intent, admission, or a venue write.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
import re
import ssl
from typing import Any

from crypto_trader.crew_intent_policy import InstrumentFilters, VerifiedMarketQuote
from crypto_trader.execution.binance_testnet.models import MarkEvidence, TestnetSymbolRules
from crypto_trader.validity.market_state import MarketObservation

__all__ = ["TestnetTickerQuoteError", "BinanceTestnetTickerQuoteSource", "filters_from_testnet_rules"]

_VENUE = "binance-spot-testnet"
_PRICE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_SYMBOL = re.compile(r"[A-Z0-9]+\Z")
_MAX_FRAME_BYTES = 16_384


class TestnetTickerQuoteError(ValueError):
    """A testnet ticker frame cannot be trusted for an opt-in host decision."""


def _decimal(raw: object, name: str) -> Decimal:
    if not isinstance(raw, str) or not _PRICE.fullmatch(raw):
        raise TestnetTickerQuoteError(f"{name} must be an exact unsigned decimal string.")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise TestnetTickerQuoteError(f"{name} is malformed.") from exc
    if not value.is_finite() or value <= 0:
        raise TestnetTickerQuoteError(f"{name} must be positive and finite.")
    return value


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise TestnetTickerQuoteError("duplicate field in market frame.")
        result[name] = value
    return result


def _default_connect(url: str, timeout_seconds: float) -> Any:
    import websocket  # Direct dependency, injected connector in offline tests.

    return websocket.create_connection(
        url, timeout=timeout_seconds,
        sslopt={"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True},
        enable_multithread=True,
    )


def filters_from_testnet_rules(rules: TestnetSymbolRules) -> InstrumentFilters:
    """Map strict exchangeInfo filters into the separate host policy boundary."""
    if type(rules) is not TestnetSymbolRules or rules.status != "TRADING":
        raise TestnetTickerQuoteError("instrument rules must be active strict TestnetSymbolRules.")
    for field in ("tick_size", "step_size", "min_quantity", "min_notional"):
        value = getattr(rules, field)
        if type(value) is not Decimal or not value.is_finite() or value <= 0:
            raise TestnetTickerQuoteError("testnet instrument rules contain an unsafe limit.")
    return InstrumentFilters(
        venue=_VENUE, symbol=rules.symbol,
        tick_size=rules.tick_size, quantity_step=rules.step_size,
        minimum_quantity=rules.min_quantity,
        minimum_notional_quote=rules.min_notional,
    )


class BinanceTestnetTickerQuoteSource:
    """Fetch exactly one TLS-pinned public ticker frame; never submit an order.

    The host supplies a wall clock and positive maximum event age / bid-ask spread.
    The connector is injectable for offline tests. An accepted quote remains subject
    to host policy, Validity, Hard Risk and all runtime safety gates.
    """

    def __init__(
        self, *, symbol: str, clock: Callable[[], datetime],
        max_event_age: timedelta, max_spread_bps: Decimal,
        timeout_seconds: float = 5.0,
        connect: Callable[[str, float], Any] = _default_connect,
    ) -> None:
        if type(symbol) is not str or not _SYMBOL.fullmatch(symbol):
            raise TestnetTickerQuoteError("ticker symbol must be uppercase ASCII alphanumeric.")
        if not callable(clock) or not callable(connect):
            raise TestnetTickerQuoteError("ticker source requires a clock and connector.")
        if type(max_event_age) is not timedelta or not timedelta(0) < max_event_age <= timedelta(minutes=1):
            raise TestnetTickerQuoteError("maximum event age must be positive and bounded.")
        if (type(max_spread_bps) is not Decimal or not max_spread_bps.is_finite()
                or not Decimal("0") < max_spread_bps <= Decimal("10000")):
            raise TestnetTickerQuoteError("maximum spread must be finite and bounded.")
        if (type(timeout_seconds) not in (float, int) or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30):
            raise TestnetTickerQuoteError("read timeout must be bounded.")
        self.symbol = symbol
        self.url = f"wss://stream.testnet.binance.vision/ws/{symbol.lower()}@ticker"
        self._clock = clock
        self._max_age = max_event_age
        self._max_spread = max_spread_bps
        self._timeout = float(timeout_seconds)
        self._connect = connect
        self._used = False

    def read_once_bound(self) -> tuple[VerifiedMarketQuote, MarketObservation]:
        """Read one frame for BOTH advisory price and runtime validity provenance.

        A neutral midpoint is only a reference price, never a fill or tradable offer.
        The pair shares the exact venue event time and frame digest; neither is
        independently refreshed, and all downstream age/risk checks still apply.
        """
        quote = self.read_once()
        observation = MarketObservation(
            symbol=quote.symbol,
            price=(quote.best_bid + quote.best_ask) / Decimal("2"),
            market_event_ts=quote.event_ts,
            local_ingest_ts=quote.received_at,
            market_reference=quote.reference_id,
        )
        return quote, observation

    def read_once_risk_mark(self) -> MarkEvidence:
        """One-shot production mark with ORIGINAL Binance venue E and frame identity.

        This is a market *observation*, not an account read or an order authority.
        A consumed or failed source is never retried or replaced with an untimed REST price.
        """
        _quote, observation = self.read_once_bound()
        return MarkEvidence(
            symbol=observation.symbol, price=observation.price,
            observed_at=observation.market_event_ts,
            reference=observation.market_reference,
        )

    def read_once(self) -> VerifiedMarketQuote:
        if self._used:
            raise TestnetTickerQuoteError("ticker source is one-shot; create a fresh source.")
        self._used = True  # A failed or ambiguous read cannot be retried automatically.
        socket = None
        try:
            socket = self._connect(self.url, self._timeout)
            frame = socket.recv()
            received = self._clock()
            if (not isinstance(received, datetime) or received.tzinfo is None
                    or received.utcoffset() is None):
                raise TestnetTickerQuoteError("host receive clock must be timezone aware.")
            if type(frame) is bytes:
                if len(frame) > _MAX_FRAME_BYTES:
                    raise TestnetTickerQuoteError("ticker frame exceeds maximum byte size.")
                frame = frame.decode("utf-8", errors="strict")
            if type(frame) is not str or len(frame.encode("utf-8")) > _MAX_FRAME_BYTES:
                raise TestnetTickerQuoteError("ticker frame must be bounded JSON text.")
            payload = json.loads(frame, object_pairs_hook=_unique_pairs)
            if not isinstance(payload, Mapping) or payload.get("e") != "24hrTicker":
                raise TestnetTickerQuoteError("only raw 24hrTicker frames are accepted.")
            if payload.get("s") != self.symbol:
                raise TestnetTickerQuoteError("ticker frame symbol mismatches pinned symbol.")
            ms = payload.get("E")
            if type(ms) is not int or ms <= 0:
                raise TestnetTickerQuoteError("exchange event timestamp E is required.")
            try:
                event = datetime.fromtimestamp(ms // 1000, tz=timezone.utc) + timedelta(
                    milliseconds=ms % 1000
                )
            except (OverflowError, OSError, ValueError) as exc:
                raise TestnetTickerQuoteError("exchange event timestamp is out of range.") from exc
            received_utc = received.astimezone(timezone.utc)
            if event > received_utc or received_utc - event > self._max_age:
                raise TestnetTickerQuoteError("ticker exchange event is future-dated or stale.")
            bid = _decimal(payload.get("b"), "best bid")
            ask = _decimal(payload.get("a"), "best ask")
            _decimal(payload.get("B"), "bid size")
            _decimal(payload.get("A"), "ask size")
            if bid > ask or (ask - bid) * Decimal("10000") / bid > self._max_spread:
                raise TestnetTickerQuoteError("ticker spread is crossed or exceeds host cap.")
            digest = sha256(frame.encode("utf-8")).hexdigest()
            return VerifiedMarketQuote(
                venue=_VENUE, symbol=self.symbol,
                reference_id=f"{_VENUE}:ticker:{self.symbol}:{ms}:{digest}",
                event_ts=event, received_at=received_utc, best_bid=bid, best_ask=ask,
            )
        except TestnetTickerQuoteError:
            raise
        except (Exception,) as exc:
            raise TestnetTickerQuoteError("testnet quote read failed closed.") from exc
        finally:
            if socket is not None:
                socket.close()
