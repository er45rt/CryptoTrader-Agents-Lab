"""Binance SPOT TESTNET REST client (Phase 11).

Guarantees:

* the base URL is validated as a documented Spot Testnet host at construction time - a mainnet URL
  raises :class:`MainnetForbiddenError` and can never be used;
* credentials come only from the environment and are never logged, journaled or returned;
* reads are retried with deterministic backoff; WRITES are never blindly retried - a timeout,
  connection failure or 5xx on a write raises :class:`AmbiguousOrderError`
  (``UNKNOWN_PENDING_RECONCILIATION``);
* ``newClientOrderId`` is the idempotency key: a duplicate submission with the same id never
  produces a second venue order - the existing order state is fetched instead;
* HTTP 429 backs off (retry-after honoured) and HTTP 418 stops the run;
* ``-1021`` (timestamp outside recvWindow) re-synchronizes the clock once for reads and fails
  closed for writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from uuid import uuid4

from ..ports import OrderLifecycleState
from .clock import TestnetClock
from .credentials import TestnetCredentials
from .endpoints import SPOT_TESTNET_REST_BASE, require_testnet_base_url
from .errors import (
    AmbiguousOrderError,
    BinanceTestnetError,
    ClockDriftError,
    RateLimitError,
    TestnetRejectedError,
    TestnetSymbolError,
    UnsafeTestnetAccountError,
)
from .models import (
    STATUS_TO_STATE,
    TestnetAccountState,
    TestnetEnvironment,
    TestnetFill,
    TestnetOrder,
    TestnetOrderRequest,
    TestnetSymbolRules,
    format_decimal,
    utc_from_ms,
)
from .rate_limit import RateLimitBudget, RetryPolicy, TokenBucket
from .signing import build_query_string, signed_query_string
from .transport import (
    API_KEY_HEADER,
    HttpRequest,
    HttpResponse,
    Transport,
    TransportError,
    TransportTimeout,
)

__all__ = ["BinanceSpotTestnetClient"]


class WithdrawalPolicy(str, Enum):
    """How strictly an account's withdrawal permission is treated.

    * ``REQUIRE_DISABLED`` (default): fail closed when the venue reports withdrawals enabled. This
      is the correct policy for any real (live-capable) key.
    * ``RECORD_ONLY``: record the flag and continue. Used for the Binance **testnet** default,
      where the permission bit can be granted while no withdrawal capability exists in this
      adapter at all (no withdrawal endpoint is implemented; proven by the scope tests).
    """

    REQUIRE_DISABLED = "require_disabled"
    RECORD_ONLY = "record_only"

# Endpoint paths (Spot REST, documented).
PATH_TIME = "/api/v3/time"
PATH_EXCHANGE_INFO = "/api/v3/exchangeInfo"
PATH_ACCOUNT = "/api/v3/account"
PATH_ORDER = "/api/v3/order"
PATH_OPEN_ORDERS = "/api/v3/openOrders"
PATH_PRICE = "/api/v3/ticker/price"

_WEIGHT_READ_ORDER = 2
_WEIGHT_READ_OPEN_ORDERS = 3
_WEIGHT_EXCHANGE_INFO = 20
_WEIGHT_WRITE = 1

#: Documented error code for a timestamp outside the receive window.
CODE_TIMESTAMP_OUTSIDE_RECV_WINDOW = -1021


@dataclass
class BinanceSpotTestnetClient:
    """A testnet-only Spot client with explicit ambiguity and rate-limit semantics."""

    credentials: TestnetCredentials
    transport: Transport
    clock: TestnetClock
    base_url: str = SPOT_TESTNET_REST_BASE
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    symbol: str = "BTCUSDT"
    withdrawal_policy: WithdrawalPolicy = WithdrawalPolicy.REQUIRE_DISABLED
    budget: RateLimitBudget | None = None
    _orders: dict[str, TestnetOrder] = field(default_factory=dict, init=False)
    _request_log: list[str] = field(default_factory=list, init=False)
    _rules: TestnetSymbolRules | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.base_url = require_testnet_base_url(self.base_url)
        if self.budget is None:
            self.budget = RateLimitBudget(bucket=TokenBucket(capacity=1200, refill_per_second=20.0))

    # --- safe observability -------------------------------------------------

    @property
    def venue_label(self) -> str:
        return self.credentials.venue_label

    def request_log(self) -> tuple[str, ...]:
        """Every request in log-safe form (no API key, no signature)."""
        return tuple(self._request_log)

    def known_orders(self) -> tuple[TestnetOrder, ...]:
        return tuple(self._orders.values())

    # --- public / signed requests ------------------------------------------

    def server_time_ms(self) -> int:
        body = self._request(PATH_TIME, {}, weight=1, is_write=False)
        server_time = body.get("serverTime") if isinstance(body, Mapping) else None
        if not isinstance(server_time, int):
            raise BinanceTestnetError("the testnet server time response was not understood.")
        return server_time

    def synchronize_clock(self) -> int:
        """Synchronize the local clock with the testnet server; returns the offset in ms."""
        return self.clock.synchronize(self.server_time_ms())

    def price(self, symbol: str | None = None) -> Decimal:
        body = self._request(PATH_PRICE, {"symbol": symbol or self.symbol}, weight=1, is_write=False)
        price = body.get("price") if isinstance(body, Mapping) else None
        if price is None:
            raise BinanceTestnetError("the testnet price response was not understood.")
        return Decimal(str(price))

    def account_snapshot(self, *, asset: str | None = None) -> TestnetAccountState:
        body = self._request(PATH_ACCOUNT, {}, weight=10, is_write=False, signed=True)
        if not isinstance(body, Mapping):
            raise BinanceTestnetError("the testnet account response was not understood.")
        balances = body.get("balances") or []
        free = locked = Decimal("0")
        if asset:
            for entry in balances:
                if str(entry.get("asset", "")).upper() == asset.upper():
                    free = Decimal(str(entry.get("free", "0")))
                    locked = Decimal(str(entry.get("locked", "0")))
                    break
        return TestnetAccountState(
            can_trade=bool(body.get("canTrade")),
            can_withdraw=bool(body.get("canWithdraw")),
            account_type=str(body.get("accountType", "UNKNOWN")),
            permissions=tuple(str(item) for item in (body.get("permissions") or ())),
            maker_commission=int(body.get("makerCommission", 0) or 0),
            taker_commission=int(body.get("takerCommission", 0) or 0),
            free_balance=free,
            locked_balance=locked,
            asset=asset,
            venue_label=self.venue_label,
        )

    def verify_environment(self, *, symbol: str | None = None) -> TestnetEnvironment:
        """Verify endpoint, clock, account privileges and instrument rules.

        Fails closed when trading is disabled, and - unless the caller explicitly selected
        ``WithdrawalPolicy.RECORD_ONLY`` for the Binance **testnet** default - when the venue
        reports withdrawals enabled. The observed flag is always recorded.
        """
        target = symbol or self.symbol
        offset = self.synchronize_clock()
        rules = self.symbol_rules(target)
        account = self.account_snapshot(asset=rules.quote_asset)
        if account.withdrawals_available and self.withdrawal_policy is WithdrawalPolicy.REQUIRE_DISABLED:
            raise UnsafeTestnetAccountError(
                "the testnet API key has withdrawals enabled; Phase 11 requires a least-privilege "
                "key with withdrawals unavailable. Refusing to continue (pass "
                "WithdrawalPolicy.RECORD_ONLY only for a venue that grants the flag by default and "
                "where this adapter provably implements no withdrawal endpoint)."
            )
        if not account.can_trade:
            raise UnsafeTestnetAccountError(
                "the testnet API key cannot trade; Phase 11 requires spot trading enabled."
            )
        notes: tuple[str, ...] = (
            "endpoint verified as Binance Spot Testnet",
            "no mainnet endpoint is reachable from this adapter",
            "no withdrawal endpoint exists anywhere in this adapter",
        )
        if account.withdrawals_available:
            notes += (
                "VENUE REPORTS WITHDRAWALS ENABLED on this testnet key "
                f"(policy={self.withdrawal_policy.value}); no withdrawal capability is implemented "
                "or used. A future live/mainnet key MUST be withdrawal-disabled.",
            )
        else:
            notes += ("withdrawals unavailable on this key (verified)",)
        return TestnetEnvironment(
            base_url=self.base_url,
            host=self.base_url.split("//", 1)[-1],
            venue_label=self.venue_label,
            server_time_ms=self.clock.timestamp_ms(),
            clock_offset_ms=offset,
            account=account,
            symbol=target,
            notes=notes,
        )

    def symbol_rules(self, symbol: str | None = None) -> TestnetSymbolRules:
        """Fetch (and cache) the trading rules for one symbol."""
        target = symbol or self.symbol
        if self._rules is not None and self._rules.symbol == target:
            return self._rules
        body = self._request(
            PATH_EXCHANGE_INFO, {"symbol": target}, weight=_WEIGHT_EXCHANGE_INFO, is_write=False
        )
        symbols = body.get("symbols") if isinstance(body, Mapping) else None
        if not symbols:
            raise TestnetSymbolError(f"symbol {target!r} was not found on the testnet exchange.")
        entry = symbols[0]
        filters = {item["filterType"]: item for item in entry.get("filters", [])}
        price_filter = filters.get("PRICE_FILTER", {})
        lot_filter = filters.get("LOT_SIZE", {})
        notional_filter = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
        self._rules = TestnetSymbolRules(
            symbol=str(entry.get("symbol", target)),
            base_asset=str(entry.get("baseAsset", "")),
            quote_asset=str(entry.get("quoteAsset", "")),
            price_tick=Decimal(str(price_filter.get("tickSize", "0.00000001"))),
            quantity_step=Decimal(str(lot_filter.get("stepSize", "0.00000001"))),
            min_quantity=Decimal(str(lot_filter.get("minQty", "0"))),
            min_notional=Decimal(str(notional_filter.get("minNotional", "0"))),
            status=str(entry.get("status", "UNKNOWN")),
        )
        return self._rules

    # --- orders -------------------------------------------------------------

    def place_order(self, request: TestnetOrderRequest) -> TestnetOrder:
        """Place one order. A duplicate ``client_order_id`` is never sent twice."""
        existing = self._orders.get(request.client_order_id)
        if existing is not None:
            current = self.order_status(request.symbol, request.client_order_id)
            return replace(current, deduplicated=True)

        rules = self.symbol_rules(request.symbol)
        if not rules.quantity_is_valid(request.quantity):
            raise TestnetSymbolError(
                f"quantity {format_decimal(request.quantity)} violates the {request.symbol} step "
                f"size {format_decimal(rules.quantity_step)}; the adapter never rounds quantities."
            )
        if request.order_type == "LIMIT":
            assert request.price is not None
            if not rules.price_is_valid(request.price):
                raise TestnetSymbolError(
                    f"price {format_decimal(request.price)} violates the {request.symbol} tick size "
                    f"{format_decimal(rules.price_tick)}; the adapter never rounds prices."
                )
            if not rules.notional_is_valid(request.quantity, request.price):
                raise TestnetSymbolError(
                    f"order notional is below the {request.symbol} minimum "
                    f"{format_decimal(rules.min_notional)}."
                )

        params: dict[str, object] = {
            "symbol": request.symbol,
            "side": request.side,
            "type": request.order_type,
            "quantity": format_decimal(request.quantity),
            "newClientOrderId": request.client_order_id,
            "newOrderRespType": "FULL",
        }
        if request.order_type == "LIMIT":
            params["price"] = format_decimal(request.price)  # type: ignore[arg-type]
            params["timeInForce"] = request.time_in_force

        try:
            body = self._request(
                PATH_ORDER, params, weight=_WEIGHT_WRITE, is_write=True, signed=True
            )
        except TestnetRejectedError as exc:
            if "duplicate" not in str(exc).lower():
                raise
            # The venue deduplicated on newClientOrderId (for example after a restart where the
            # local record was lost). Adopt the existing order instead of treating this as a
            # failure - and still never send a second order.
            order = self.order_status(request.symbol, request.client_order_id)
            self._orders[order.client_order_id] = order
            return replace(order, deduplicated=True)
        order = self._parse_order(body)
        self._orders[order.client_order_id] = order
        return order

    def cancel_order(self, symbol: str, client_order_id: str) -> TestnetOrder:
        """Cancel one order by client order id (idempotent: canceling a terminal order is a no-op)."""
        existing = self._orders.get(client_order_id)
        if existing is not None and existing.is_terminal:
            return existing
        body = self._request(
            PATH_ORDER,
            {"symbol": symbol, "origClientOrderId": client_order_id},
            weight=_WEIGHT_WRITE,
            is_write=True,
            signed=True,
            method="DELETE",
        )
        order = self._parse_order(body)
        self._orders[order.client_order_id] = order
        return order

    def order_status(self, symbol: str, client_order_id: str) -> TestnetOrder:
        body = self._request(
            PATH_ORDER,
            {"symbol": symbol, "origClientOrderId": client_order_id},
            weight=_WEIGHT_READ_ORDER,
            is_write=False,
            signed=True,
        )
        order = self._parse_order(body)
        self._orders[order.client_order_id] = order
        return order

    def open_orders(self, symbol: str | None = None) -> tuple[TestnetOrder, ...]:
        params = {"symbol": symbol} if symbol else {}
        body = self._request(
            PATH_OPEN_ORDERS, params, weight=_WEIGHT_READ_OPEN_ORDERS, is_write=False, signed=True
        )
        orders = tuple(self._parse_order(item) for item in (body or ()))
        for order in orders:
            self._orders[order.client_order_id] = order
        return orders

    def cancel_all_open_orders(self, symbol: str) -> tuple[TestnetOrder, ...]:
        """Cancel every open order for ``symbol`` (the Safety Lane cancel-all action)."""
        results: list[TestnetOrder] = []
        for order in self.open_orders(symbol):
            results.append(self.cancel_order(symbol, order.client_order_id))
        return tuple(results)

    def test_order(self, request: TestnetOrderRequest) -> Mapping[str, Any]:
        """Validate an order without placing it (``/order/test`` equivalent, signed)."""
        params: dict[str, object] = {
            "symbol": request.symbol,
            "side": request.side,
            "type": request.order_type,
            "quantity": format_decimal(request.quantity),
            "newClientOrderId": request.client_order_id,
        }
        if request.order_type == "LIMIT":
            params["price"] = format_decimal(request.price)  # type: ignore[arg-type]
            params["timeInForce"] = request.time_in_force
        body = self._request(
            f"{PATH_ORDER}/test", params, weight=1, is_write=False, signed=True
        )
        return body if isinstance(body, Mapping) else {}

    def reconnect(self) -> TestnetEnvironment:
        """Re-verify connectivity, clock and permissions after an interruption."""
        return self.verify_environment()

    # --- transport plumbing -------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {API_KEY_HEADER: self.credentials.api_key}

    def _request(
        self,
        path: str,
        params: Mapping[str, object],
        *,
        weight: int,
        is_write: bool,
        signed: bool = False,
        method: str | None = None,
    ) -> Any:
        assert self.budget is not None
        attempt = 0
        resync_used = False
        while True:
            attempt += 1
            self.budget.enforce_backoff()
            self.budget.bucket.acquire(weight)

            payload = dict(params)
            if signed:
                payload = self.clock.signed_timestamp_params(payload)
                query = signed_query_string(payload, self.credentials.with_secret())
            else:
                query = build_query_string(payload)

            resolved_method = method or ("POST" if is_write else "GET")
            request = HttpRequest(
                method=resolved_method,
                url=f"{self.base_url}{path}",
                query=query,
                headers=self._headers(),
                weight=weight,
                is_write=is_write,
            )
            # Only the redacted form is ever recorded: the API key and signature never enter
            # the log, the journal, a report or a test failure diff.
            self._request_log.append(request.safe_repr())

            try:
                response = self.transport.send(request)
            except (TransportTimeout, TransportError) as exc:
                if is_write:
                    raise AmbiguousOrderError(
                        "the write outcome is unknown (transport failure before a response); the "
                        "order is NOT retried and must go through reconciliation."
                    ) from exc
                if attempt >= self.retry.max_attempts:
                    raise
                self._sleep(self.retry.delay_for_attempt(attempt))
                continue

            if response.status_code == 200:
                return response.body

            if response.status_code == 418:
                self.budget.record_418(response.retry_after_seconds)

            if response.status_code == 429:
                delay = self.budget.record_429(response.retry_after_seconds)
                if is_write:
                    raise RateLimitError(
                        f"rate limited on a write (429); backing off {delay}s without retrying."
                    )
                if attempt >= self.retry.max_attempts:
                    raise RateLimitError("rate limited (429) and retries are exhausted.")
                continue

            if 500 <= response.status_code < 600:
                if is_write:
                    raise AmbiguousOrderError(
                        f"the venue returned {response.status_code} for a write; the outcome is "
                        "unknown and the order is not retried."
                    )
                if attempt >= self.retry.max_attempts:
                    raise TransportError(f"the venue returned {response.status_code}.")
                self._sleep(self.retry.delay_for_attempt(attempt))
                continue

            code, message = self._error_of(response)
            if code == CODE_TIMESTAMP_OUTSIDE_RECV_WINDOW:
                if is_write:
                    raise ClockDriftError(
                        "the venue rejected the timestamp (-1021) on a write; re-synchronize the "
                        "clock and reconcile before sending anything else."
                    )
                if resync_used:
                    raise ClockDriftError(
                        "the venue rejected the timestamp (-1021) twice; refusing to continue."
                    )
                resync_used = True
                self.synchronize_clock()
                continue

            raise TestnetRejectedError(
                f"the testnet venue rejected the request (HTTP {response.status_code}, code "
                f"{code}): {self._safe_message(message)}"
            )

    def _sleep(self, seconds: float) -> None:
        assert self.budget is not None
        self.budget.sleep(seconds)

    @staticmethod
    def _error_of(response: HttpResponse) -> tuple[int | None, str]:
        body = response.body
        if isinstance(body, Mapping):
            code = body.get("code")
            message = str(body.get("msg", ""))
            return (int(code) if isinstance(code, int) else None), message
        return None, str(body or "")

    def _safe_message(self, message: str) -> str:
        """Mask a venue error message ONLY when it actually echoes a credential value.

        Masking on keywords alone would hide legitimate diagnostics such as "-2014 API-key format
        invalid" while adding no protection, so the check is value-based.
        """
        for secret in (self.credentials.api_key, self.credentials.with_secret()):
            if secret and secret in message:
                return "<redacted venue message>"
        return message

    def _parse_order(self, body: Any) -> TestnetOrder:
        if not isinstance(body, Mapping):
            raise BinanceTestnetError("the testnet order response was not understood.")
        status = str(body.get("status", "NEW"))
        state = STATUS_TO_STATE.get(status)
        if state is None:
            raise BinanceTestnetError(
                f"unrecognised testnet order status {status!r}; refusing to guess a lifecycle state."
            )
        try:
            fills = tuple(
                TestnetFill(
                    price=Decimal(str(item.get("price", "0"))),
                    quantity=Decimal(str(item.get("qty", "0"))),
                    commission=Decimal(str(item.get("commission", "0"))),
                    commission_asset=(
                        None if item.get("commissionAsset") is None
                        else str(item["commissionAsset"])
                    ),
                    trade_id=None if item.get("tradeId") is None else str(item["tradeId"]),
                )
                for item in (body.get("fills") or ())
            )
            executed = Decimal(str(body.get("executedQty", "0")))
            return TestnetOrder(
                symbol=str(body.get("symbol", self.symbol)),
                client_order_id=str(
                    body.get("clientOrderId") or body.get("origClientOrderId") or ""
                ),
                venue_order_id=None if body.get("orderId") is None else str(body["orderId"]),
                state=state,
                status=status,
                original_quantity=Decimal(str(body.get("origQty", executed))),
                executed_quantity=executed,
                cummulative_quote_quantity=Decimal(
                    str(body.get("cummulativeQuoteQty", body.get("cumulativeQuoteQty", "0")))
                ),
                price=Decimal(str(body.get("price", "0"))),
                fills=fills,
                transact_time=utc_from_ms(
                    body.get("transactTime") or body.get("updateTime") or body.get("time")
                ),
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise BinanceTestnetError(f"the testnet order response was malformed: {exc}") from exc
