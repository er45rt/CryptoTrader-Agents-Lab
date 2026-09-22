"""Authenticated MAINNET READ-ONLY REST client (Phase 15).

The client can only issue HTTP GET requests to an explicit read-only path allowlist; the request
helper does not even accept an HTTP method, so no mutating verb can be expressed. Signed requests
use the dedicated mainnet read-only credential pair; the shared HTTP transport, clock and signing
primitives come from the Phase 11 adapter because they are venue-generic infrastructure, while the
credentials, endpoints and permission profile stay strictly separate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from crypto_trader.execution.binance_testnet import (
    API_KEY_HEADER,
    HttpRequest,
    RetryPolicy,
    TestnetClock,
    Transport,
    TransportError,
    TransportTimeout,
)
from crypto_trader.execution.binance_testnet.signing import (
    build_query_string,
    signed_query_string,
)

from .credentials import MainnetReadOnlyCredentials
from .endpoints import SPOT_MAINNET_REST_BASE, require_mainnet_base_url, require_read_only_path
from .errors import MainnetEndpointError, MainnetError
from .filters import SymbolFilters, parse_symbol_filters
from .permissions import ApiRestrictions, parse_api_restrictions

__all__ = ["MainnetReadOnlyClient"]

PATH_ACCOUNT_RESTRICTIONS = "/sapi/v1/account/apiRestrictions"
PATH_ACCOUNT = "/api/v3/account"
PATH_OPEN_ORDERS = "/api/v3/openOrders"
PATH_MY_TRADES = "/api/v3/myTrades"
PATH_EXCHANGE_INFO = "/api/v3/exchangeInfo"
PATH_PRICE = "/api/v3/ticker/price"
PATH_BOOK_TICKER = "/api/v3/ticker/bookTicker"
PATH_DEPTH = "/api/v3/depth"
PATH_TIME = "/api/v3/time"


@dataclass
class MainnetReadOnlyClient:
    """A mainnet client whose only capability is reading."""

    credentials: MainnetReadOnlyCredentials
    transport: Transport
    clock: TestnetClock
    base_url: str = SPOT_MAINNET_REST_BASE
    symbol: str = "BTCUSDT"
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    _request_log: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.base_url = require_mainnet_base_url(self.base_url)
        if not isinstance(self.credentials, MainnetReadOnlyCredentials):
            raise MainnetError("the mainnet read-only client requires read-only credentials.")

    # --- observability -------------------------------------------------------

    @property
    def venue_label(self) -> str:
        return self.credentials.venue_label

    @property
    def permission_scope(self) -> str:
        return self.credentials.permission_scope

    def request_log(self) -> tuple[str, ...]:
        """Every request in log-safe form (no API key, no signature)."""
        return tuple(self._request_log)

    # --- authenticated reads -------------------------------------------------

    def api_restrictions(self) -> ApiRestrictions:
        """The strict signed read-only permission contract."""
        body = self._get(PATH_ACCOUNT_RESTRICTIONS, {}, signed=True)
        return parse_api_restrictions(body)

    def verify_read_only_permissions(self) -> ApiRestrictions:
        """Re-validate the permission profile (called at every would-be execution boundary)."""
        return self.api_restrictions()

    def server_time_ms(self) -> int:
        body = self._get(PATH_TIME, {}, signed=False)
        server_time = body.get("serverTime") if isinstance(body, Mapping) else None
        if not isinstance(server_time, int):
            raise MainnetError("the server time response was not understood.")
        return server_time

    def synchronize_clock(self) -> int:
        return self.clock.synchronize(self.server_time_ms())

    def account_snapshot(self) -> Mapping[str, Any]:
        """Real mainnet account/balance state (read-only)."""
        body = self._get(PATH_ACCOUNT, {}, signed=True)
        if not isinstance(body, Mapping):
            raise MainnetError("the account response was not understood.")
        return body

    def open_orders(self, symbol: str | None = None) -> tuple[Mapping[str, Any], ...]:
        """Real mainnet open-order state (read-only)."""
        params = {"symbol": symbol} if symbol else {}
        body = self._get(PATH_OPEN_ORDERS, params, signed=True)
        if not isinstance(body, list):
            raise MainnetError("the open-orders response was not understood.")
        return tuple(item for item in body if isinstance(item, Mapping))

    def my_trades(self, symbol: str | None = None, *, limit: int = 50) -> tuple[Mapping[str, Any], ...]:
        """Account trade history (read-only)."""
        if not isinstance(limit, int) or limit <= 0 or limit > 1000:
            raise MainnetError("my_trades limit must be between 1 and 1000.")
        params: dict[str, Any] = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        body = self._get(PATH_MY_TRADES, params, signed=True)
        if not isinstance(body, list):
            raise MainnetError("the trade-history response was not understood.")
        return tuple(item for item in body if isinstance(item, Mapping))

    # --- public reads --------------------------------------------------------

    def exchange_info(self, symbol: str | None = None) -> SymbolFilters:
        """Dynamic symbol filters (never hard-coded)."""
        target = symbol or self.symbol
        body = self._get(PATH_EXCHANGE_INFO, {"symbol": target}, signed=False, weight=20)
        return parse_symbol_filters(body, target)

    def price(self, symbol: str | None = None) -> Decimal:
        body = self._get(PATH_PRICE, {"symbol": symbol or self.symbol}, signed=False)
        value = body.get("price") if isinstance(body, Mapping) else None
        if value is None:
            raise MainnetError("the price response was not understood.")
        return Decimal(str(value))

    def book_ticker(self, symbol: str | None = None) -> tuple[Decimal, Decimal]:
        """Top of book as (best_bid, best_ask)."""
        body = self._get(PATH_BOOK_TICKER, {"symbol": symbol or self.symbol}, signed=False)
        if not isinstance(body, Mapping):
            raise MainnetError("the book-ticker response was not understood.")
        try:
            return Decimal(str(body["bidPrice"])), Decimal(str(body["askPrice"]))
        except (KeyError, ArithmeticError, ValueError) as exc:
            raise MainnetError("the book-ticker response was malformed.") from exc

    def depth(self, symbol: str | None = None, *, limit: int = 100) -> Mapping[str, Any]:
        if not isinstance(limit, int) or limit <= 0 or limit > 5000:
            raise MainnetError("depth limit must be between 1 and 5000.")
        body = self._get(PATH_DEPTH, {"symbol": symbol or self.symbol, "limit": limit}, signed=False)
        if not isinstance(body, Mapping):
            raise MainnetError("the depth response was not understood.")
        return body

    # --- transport plumbing --------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {API_KEY_HEADER: self.credentials.api_key}

    def _get(
        self,
        path: str,
        params: Mapping[str, Any],
        *,
        signed: bool,
        weight: int = 1,
    ) -> Any:
        """The ONLY request helper: it always sends GET to an allowlisted read-only path."""
        require_read_only_path(path)
        attempt = 0
        while True:
            attempt += 1
            payload: dict[str, Any] = dict(params)
            if signed:
                payload = self.clock.signed_timestamp_params(payload)
                query = signed_query_string(payload, self.credentials.with_secret())
            else:
                query = build_query_string(payload)
            request = HttpRequest(
                method="GET",
                url=f"{self.base_url}{path}",
                query=query,
                headers=self._headers(),
                weight=weight,
                is_write=False,
            )
            self._request_log.append(request.safe_repr())
            try:
                response = self.transport.send(request)
            except (TransportTimeout, TransportError):
                if attempt >= self.retry.max_attempts:
                    raise
                continue
            if response.status_code == 200:
                return response.body
            code, message = _error_of(response.body)
            raise MainnetEndpointError(
                f"the mainnet read-only request was rejected (HTTP {response.status_code}, code "
                f"{code}): {self._safe_message(message)}"
            )

    def _safe_message(self, message: str) -> str:
        for secret in (self.credentials.api_key, self.credentials.with_secret()):
            if secret and secret in message:
                return "<redacted venue message>"
        return message


def _error_of(body: Any) -> tuple[int | None, str]:
    if isinstance(body, Mapping):
        code = body.get("code")
        return (int(code) if isinstance(code, int) else None), str(body.get("msg", ""))
    return None, str(body or "")
