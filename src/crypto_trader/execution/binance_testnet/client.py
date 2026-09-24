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
from uuid import UUID, uuid4

from ..ports import OrderLifecycleState
from .clock import TestnetClock
from .credentials import TestnetCredentials
from .endpoints import SPOT_TESTNET_REST_BASE, require_testnet_base_url
from .errors import (
    AmbiguousOrderError,
    AttemptJournalRequiredError,
    BinanceTestnetError,
    ClockDriftError,
    RateLimitError,
    TestnetRejectedError,
    TestnetSymbolError,
    UnsafeTestnetAccountError,
    VenueProtocolRejectionError,
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
    PreSendTransportError,
    Transport,
    TransportError,
    TransportTimeout,
)
from .translate import deterministic_order_uuid

__all__ = ["AttemptAuthorization", "BinanceSpotTestnetClient", "PreparedTestnetOrder"]


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


@dataclass(frozen=True)
class AttemptAuthorization:
    """The mechanical binding between a durably committed attempt marker and one prepared write.

    Produced only by :meth:`BinanceSpotTestnetClient._authorize_attempt`, which the production
    runtime invokes immediately after ``order.submission.attempted`` has been durably appended.
    It binds the marker identity to the exact economic anchors of the prepared order, so a
    prepared write cannot be sent without a committed durable attempt anchor.
    """

    marker_event_id: UUID
    client_order_id: str
    order_id: UUID
    intent_id: UUID
    correlation_id: UUID
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal | None
    time_in_force: str


@dataclass(frozen=True)
class PreparedTestnetOrder:
    """The fully prepared, signed write for one order (Stage 2).

    Everything locally rejectable happens BEFORE this object exists: dedup reads, symbol-rule
    reads, order validation, rate-budget admission, the clock precondition, timestamp generation,
    recvWindow construction, signing and serialization. After a durable attempt marker is
    committed, the only remaining write-path action is ``transport.send(prepared.request)``.

    ``request`` is ``None`` when the order is already known (an idempotent re-read needs no write).
    """

    client_order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal | None
    time_in_force: str
    request: HttpRequest | None
    existing: TestnetOrder | None = None
    attempt_authorization: AttemptAuthorization | None = None

    @property
    def requires_write(self) -> bool:
        return self.request is not None

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

    def prepare_order(self, request: TestnetOrderRequest) -> PreparedTestnetOrder:
        """Do every locally rejectable step for a write, without performing the write.

        Read-only venue calls (an idempotency status lookup and symbol-rule retrieval) are allowed
        here: they happen before the durable attempt marker is committed and cannot mutate an order.
        Rate/backoff admission, the clock precondition, timestamp generation, recvWindow
        construction, signing and serialization also happen here, so the caller can commit a durable
        attempt marker knowing that nothing after it may reject the write locally.

        Budget consumption is intentionally conservative: a token spent before a marker write that
        later fails is never refunded.
        """
        existing = self._orders.get(request.client_order_id)
        if existing is not None:
            current = self.order_status(request.symbol, request.client_order_id)
            self._orders[current.client_order_id] = current
            return PreparedTestnetOrder(
                client_order_id=request.client_order_id,
                symbol=request.symbol,
                side=request.side.lower(),
                order_type=request.order_type.lower(),
                quantity=request.quantity,
                limit_price=request.price,
                time_in_force=(request.time_in_force or "").lower(),
                request=None,
                existing=replace(current, deduplicated=True),
            )

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

        # Rate/backoff admission and the clock/signing preconditions are the last local steps that
        # can reject the write, so they must complete before the caller commits the attempt marker.
        assert self.budget is not None
        self.budget.enforce_backoff()
        self.budget.bucket.acquire(_WEIGHT_WRITE)
        signed_payload = self.clock.signed_timestamp_params(params)
        query = signed_query_string(signed_payload, self.credentials.with_secret())
        http_request = HttpRequest(
            method="POST",
            url=f"{self.base_url}{PATH_ORDER}",
            query=query,
            headers=self._headers(),
            weight=_WEIGHT_WRITE,
            is_write=True,
        )
        return PreparedTestnetOrder(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side.lower(),
            order_type=request.order_type.lower(),
            quantity=request.quantity,
            limit_price=request.price,
            time_in_force=(request.time_in_force or "").lower(),
            request=http_request,
        )

    def _authorize_attempt(
        self,
        prepared: PreparedTestnetOrder,
        *,
        marker_event_id: UUID,
        venue_reference: str,
        order_id: UUID,
        intent_id: UUID,
        correlation_id: UUID,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        limit_price: Decimal | None,
        time_in_force: str,
    ) -> PreparedTestnetOrder:
        """Bind a durably appended attempt marker to the exact prepared write.

        Called ONLY by the production runtime (``execution/binance_testnet/runtime.py``) and only
        after ``journal.append(marker)`` has returned successfully. The client never reads the
        journal: it validates that the marker fields it is handed are exactly the prepared order's
        own identity and economic anchors, so an unauthorized prepared write can never be sent.
        """
        if not isinstance(prepared, PreparedTestnetOrder):
            raise AttemptJournalRequiredError("attempt authorization expects a prepared order.")
        if prepared.request is None:
            raise AttemptJournalRequiredError(
                "attempt authorization was requested for an order that requires no write."
            )
        expected_order_id = deterministic_order_uuid(prepared.client_order_id)
        mismatches: list[str] = []
        if not isinstance(marker_event_id, UUID):
            mismatches.append("marker_event_id")
        if venue_reference != prepared.client_order_id:
            mismatches.append("venue_reference")
        if order_id != expected_order_id:
            mismatches.append("order_id")
        if not isinstance(intent_id, UUID):
            mismatches.append("intent_id")
        if not isinstance(correlation_id, UUID):
            mismatches.append("correlation_id")
        if symbol != prepared.symbol:
            mismatches.append("symbol")
        if side != prepared.side:
            mismatches.append("side")
        if order_type != prepared.order_type:
            mismatches.append("order_type")
        if quantity != prepared.quantity:
            mismatches.append("quantity")
        if limit_price != prepared.limit_price:
            mismatches.append("limit_price")
        if prepared.order_type == "limit" and time_in_force != prepared.time_in_force:
            mismatches.append("time_in_force")
        if mismatches:
            raise AttemptJournalRequiredError(
                "the durable attempt marker does not bind to this prepared order (fields: "
                + ", ".join(sorted(mismatches))
                + "); refusing to send."
            )
        return replace(
            prepared,
            attempt_authorization=AttemptAuthorization(
                marker_event_id=marker_event_id,
                client_order_id=prepared.client_order_id,
                order_id=expected_order_id,
                intent_id=intent_id,
                correlation_id=correlation_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                limit_price=limit_price,
                time_in_force=time_in_force,
            ),
        )

    @staticmethod
    def _verify_attempt_authorization(prepared: PreparedTestnetOrder) -> None:
        """Fail closed unless the prepared write carries a matching durable authorization."""
        authorization = prepared.attempt_authorization
        if authorization is None:
            raise AttemptJournalRequiredError(
                "refusing to send a prepared order without a durably committed "
                "order.submission.attempted authorization."
            )
        mismatches: list[str] = []
        if authorization.client_order_id != prepared.client_order_id:
            mismatches.append("client_order_id")
        if authorization.order_id != deterministic_order_uuid(prepared.client_order_id):
            mismatches.append("order_id")
        if authorization.symbol != prepared.symbol:
            mismatches.append("symbol")
        if authorization.side != prepared.side:
            mismatches.append("side")
        if authorization.order_type != prepared.order_type:
            mismatches.append("order_type")
        if authorization.quantity != prepared.quantity:
            mismatches.append("quantity")
        if authorization.limit_price != prepared.limit_price:
            mismatches.append("limit_price")
        if prepared.order_type == "limit" and authorization.time_in_force != prepared.time_in_force:
            mismatches.append("time_in_force")
        if mismatches:
            raise AttemptJournalRequiredError(
                "the attempt authorization does not match the prepared order (fields: "
                + ", ".join(sorted(mismatches))
                + "); refusing to send."
            )

    def submit_prepared(self, prepared: PreparedTestnetOrder) -> TestnetOrder:
        """Send the exact prepared request. This is the ONLY venue-write call site.

        No validation, rate admission, clock work, signing or request reconstruction happens here:
        the caller commits the durable attempt marker immediately before this call, and the next
        write-path action is ``transport.send`` of the already-prepared request.
        """
        if not isinstance(prepared, PreparedTestnetOrder):
            raise BinanceTestnetError("submit_prepared expects a PreparedTestnetOrder.")
        if prepared.existing is not None:
            return replace(prepared.existing, deduplicated=True)
        http_request = prepared.request
        if http_request is None:
            raise BinanceTestnetError("a prepared order without a request cannot be submitted.")
        # The venue-write boundary: a prepared write is only sendable with a durable authorization
        # bound to the attempt marker the runtime committed.
        self._verify_attempt_authorization(prepared)

        # Only the redacted form is ever recorded: the API key and signature never enter the log.
        self._request_log.append(http_request.safe_repr())
        try:
            response = self.transport.send(http_request)
        except PreSendTransportError:
            # Mechanically proven not transmitted: typed pre-send evidence, never retried here.
            raise
        except (TransportTimeout, TransportError) as exc:
            raise AmbiguousOrderError(
                "the write outcome is unknown (transport failure before a response); the "
                "order is NOT retried and must go through reconciliation."
            ) from exc
        return self._resolve_write_response(response, prepared)

    def place_order(self, request: TestnetOrderRequest) -> TestnetOrder:
        """Compatibility path: adopt a known order, or fail closed for a fresh write.

        A fresh venue write requires the durable ``order.submission.attempted`` anchor and the
        runtime-owned authorization, so this method never performs one: it may only resolve an
        already-known order (the read-only dedup path) and otherwise raises
        :class:`AttemptJournalRequiredError` with zero POSTs.
        """
        prepared = self.prepare_order(request)
        if prepared.request is None and prepared.existing is not None:
            return replace(prepared.existing, deduplicated=True)
        raise AttemptJournalRequiredError(
            "place_order cannot perform a fresh venue write: a durable attempt marker must be "
            "committed by TestnetOrderRuntime before submit_prepared may send. No request was sent."
        )

    def _resolve_write_response(
        self, response: HttpResponse, prepared: PreparedTestnetOrder
    ) -> TestnetOrder:
        """Classify one venue response to a write; HTTP 200 alone never implies success."""
        status_code = response.status_code
        if 300 <= status_code < 400:
            # The redirect was NOT followed: the request reached a server, but no order outcome can
            # be established, and the credentials/signature are never replayed to the Location.
            raise AmbiguousOrderError(
                f"the venue returned HTTP {status_code} for a write and the redirect was not "
                "followed; the order outcome is unknown and the order is not retried."
            )
        if status_code == 200:
            return self._parse_write_success(response.body, prepared)
        if status_code == 418:
            assert self.budget is not None
            self.budget.record_418(response.retry_after_seconds)
        if status_code == 429:
            assert self.budget is not None
            delay = self.budget.record_429(response.retry_after_seconds)
            raise RateLimitError(
                f"rate limited on a write (429); backing off {delay}s without retrying."
            )
        if 500 <= status_code < 600:
            raise AmbiguousOrderError(
                f"the venue returned {status_code} for a write; the outcome is unknown and the "
                "order is not retried."
            )
        code, message = self._error_of(response)
        if "duplicate" in message.lower():
            # A duplicate-identity response does NOT prove the order does not exist: the same
            # client order id may already exist at the venue (an earlier uncertain submission, a
            # replicated executor, an operator, or any external state change between our read and
            # this POST). Terminalising it as REJECTED would be a false negative, so the ONLY safe
            # treatment is reconciliation by the SAME identity: re-read the order and adopt the
            # venue view. The write is never retried (this POST count stays at one) and the durable
            # attempt marker stays in place for reconciliation.
            order = self.order_status(prepared.symbol, prepared.client_order_id)
            self._orders[order.client_order_id] = order
            return replace(order, deduplicated=True)
        if code == CODE_TIMESTAMP_OUTSIDE_RECV_WINDOW:
            raise VenueProtocolRejectionError(
                "the venue rejected the write timestamp (-1021): the request was refused before any "
                "order was created. The write is NOT retried; the durable attempt remains and must "
                "be reconciled."
            )
        # HTTP 429/418 (and the generic deterministic-rejection branch below) mean the venue
        # responded, but 429/418 alone do NOT prove the final order-processing outcome, so they are
        # neither PRE_SEND nor a terminal REJECTED: the RateLimitError/RateLimitBanError raised
        # here leaves the durable attempt marker in place, keeps the order fenced pending
        # reconciliation, and never triggers an automatic POST retry.
        raise TestnetRejectedError(
            f"the testnet venue rejected the request (HTTP {status_code}, code "
            f"{code}): {self._safe_message(message)}"
        )

    def _parse_write_success(self, body: Any, prepared: PreparedTestnetOrder) -> TestnetOrder:
        """A write must be mechanically recognizable as successful, or it is ambiguous."""
        if not isinstance(body, Mapping):
            raise AmbiguousOrderError(
                "the write returned HTTP 200 without a usable order payload (empty or non-JSON "
                "body); the order outcome is unknown and the order is not retried."
            )
        try:
            order = self._parse_order(body)
        except BinanceTestnetError as exc:
            raise AmbiguousOrderError(
                "the write returned HTTP 200 with an unparseable order payload "
                f"({type(exc).__name__}); the order outcome is unknown and the order is not retried."
            ) from exc
        if not order.venue_order_id:
            raise AmbiguousOrderError(
                "the write response carried no venue order identifier; the order outcome is "
                "unknown and the order is not retried."
            )
        if order.client_order_id != prepared.client_order_id:
            raise AmbiguousOrderError(
                "the write response carried a different client order identity than the prepared "
                "order; the order outcome is unknown and the order is not retried."
            )
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
