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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
import math
import time
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from ..exchange_filters import (
    ExchangeFilterValidationError,
    parse_exchange_filters,
    validate_limit_order,
)
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
    OrderNotFoundError,
    TestnetRejectedError,
    TestnetEndpointError,
    TestnetSymbolError,
    UnsafeTestnetAccountError,
    UnsupportedOrderTypeError,
    VenueProtocolRejectionError,
)
from .models import (
    STATUS_TO_STATE,
    TestnetAccountState,
    TestnetEnvironment,
    TestnetFill,
    VenueTrade,
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
PATH_MY_TRADES = "/api/v3/myTrades"
PATH_PRICE = "/api/v3/ticker/price"

_WEIGHT_READ_ORDER = 2
_WEIGHT_READ_OPEN_ORDERS = 3
_WEIGHT_READ_MY_TRADES_WITH_ORDER = 5
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
    rules_ttl_seconds: float = 60.0
    rules_monotonic: Callable[[], float] = time.monotonic
    _orders: dict[str, TestnetOrder] = field(default_factory=dict, init=False)
    _request_log: list[str] = field(default_factory=list, init=False)
    _rules: TestnetSymbolRules | None = field(default=None, init=False)
    _rules_fetched_at: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.base_url = require_testnet_base_url(self.base_url)
        if not math.isfinite(self.rules_ttl_seconds) or self.rules_ttl_seconds <= 0:
            raise TestnetEndpointError("rules_ttl_seconds must be a finite positive number.")
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
        rules = self.symbol_rules(target, refresh=True)
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

    def symbol_rules(
        self, symbol: str | None = None, *, refresh: bool = False
    ) -> TestnetSymbolRules:
        """Fetch trading rules, reusing the local cache only while it is provably fresh."""
        target = symbol or self.symbol
        now = float(self.rules_monotonic())
        if not math.isfinite(now):
            raise TestnetEndpointError("rules_monotonic returned a non-finite value.")
        fetched_at = self._rules_fetched_at
        age = None if fetched_at is None else now - fetched_at
        if (
            not refresh
            and self._rules is not None
            and self._rules.symbol == target
            and age is not None
            and 0 <= age < self.rules_ttl_seconds
        ):
            return self._rules

        # A failed refresh must never leave a previously cached rule set eligible for reuse.
        self._rules = None
        self._rules_fetched_at = None
        body = self._request(
            PATH_EXCHANGE_INFO, {"symbol": target}, weight=_WEIGHT_EXCHANGE_INFO, is_write=False
        )
        try:
            parsed = parse_exchange_filters(body, target)
        except ExchangeFilterValidationError as exc:
            raise TestnetSymbolError(str(exc)) from exc
        self._rules = TestnetSymbolRules(
            symbol=parsed.symbol,
            status=parsed.status,
            base_asset=parsed.base_asset,
            quote_asset=parsed.quote_asset,
            tick_size=parsed.tick_size,
            step_size=parsed.step_size,
            min_quantity=parsed.min_quantity,
            max_quantity=parsed.max_quantity,
            min_notional=parsed.min_notional,
        )
        self._rules_fetched_at = now
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

        if request.order_type == "MARKET":
            raise UnsupportedOrderTypeError(
                "MARKET venue writes are disabled until an explicit Binance market-notional "
                "validation contract is implemented."
            )

        rules = self.symbol_rules(request.symbol)
        assert request.price is not None
        try:
            validate_limit_order(rules, price=request.price, quantity=request.quantity)
        except ExchangeFilterValidationError as exc:
            raise TestnetSymbolError(str(exc)) from exc

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
            try:
                order = self.order_status(
                    prepared.symbol,
                    prepared.client_order_id,
                    require_exact_identity=True,
                )
            except BinanceTestnetError as exc:
                raise AmbiguousOrderError(
                    "the venue reported a duplicate client order identity, but the required "
                    "identity re-read failed; the original write outcome remains unknown and "
                    "the order must stay pending reconciliation."
                ) from exc
            # Exact identity was proved by the strict read before the response could enter
            # the in-memory order cache.
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
        try:
            if not isinstance(body, Mapping):
                raise BinanceTestnetError("cancel response must be an object.")
            original_id = body.get("origClientOrderId")
            cancel_id = body.get("clientOrderId")
            venue_id = body.get("orderId")
            if (
                not isinstance(original_id, str) or original_id != client_order_id
                or not original_id.strip()
                or not isinstance(cancel_id, str) or not cancel_id.strip()
                or type(venue_id) is not int or venue_id <= 0
                or body.get("symbol") != symbol
                or body.get("status") != "CANCELED"
                or (existing is not None and existing.symbol != symbol)
                or (
                    existing is not None and existing.venue_order_id is not None
                    and existing.venue_order_id != str(venue_id)
                )
            ):
                raise BinanceTestnetError("cancel response identity, symbol or status is invalid.")
            # Reuse strict quantity validation, with the original identity, before parsing
            # ancillary fields. Binance's clientOrderId identifies the cancellation itself.
            canonical_body = dict(body, clientOrderId=client_order_id)
            self._parse_startup_order(canonical_body, symbol, client_order_id)
            order = self._parse_order(canonical_body)
        except BinanceTestnetError as exc:
            raise AmbiguousOrderError(
                "the cancel response was malformed or contradictory; the outcome is unknown "
                "and the cancellation is not retried."
            ) from exc
        self._orders[client_order_id] = order
        return order

    def order_status(
        self,
        symbol: str,
        client_order_id: str,
        *,
        require_exact_identity: bool = False,
    ) -> TestnetOrder:
        body = self._request(
            PATH_ORDER,
            {"symbol": symbol, "origClientOrderId": client_order_id},
            weight=_WEIGHT_READ_ORDER,
            is_write=False,
            signed=True,
        )
        if require_exact_identity:
            if (
                not isinstance(body, Mapping)
                or body.get("clientOrderId") != client_order_id
                or body.get("symbol") != symbol
                or type(body.get("orderId")) is not int
                or body.get("orderId") <= 0
            ):
                raise BinanceTestnetError(
                    "order status response did not prove the exact requested client order identity."
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

    def startup_open_orders(self, symbol: str) -> tuple[TestnetOrder, ...]:
        """Strict signed startup evidence; never adopts orders into the client cache."""
        body = self._request(
            PATH_OPEN_ORDERS, {"symbol": symbol}, weight=_WEIGHT_READ_OPEN_ORDERS,
            is_write=False, signed=True,
        )
        if not isinstance(body, list):
            raise BinanceTestnetError("startup openOrders must return a list.")
        orders = tuple(self._parse_startup_order(item, symbol) for item in body)
        if any(order.status not in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"} for order in orders):
            raise BinanceTestnetError("startup openOrders returned a terminal order.")
        return orders

    def startup_order_status(self, symbol: str, reference: str) -> TestnetOrder:
        """Strict targeted read, with exact request identity and typed negative evidence."""
        body = self._request(
            PATH_ORDER, {"symbol": symbol, "origClientOrderId": reference},
            weight=_WEIGHT_READ_ORDER, is_write=False, signed=True,
            classify_order_absence=True,
        )
        return self._parse_startup_order(body, symbol, reference)

    def startup_my_trades(self, symbol: str, venue_order_id: str) -> tuple[VenueTrade, ...]:
        """Strict signed trade-history evidence for one exact venue order."""
        try:
            requested_order_id = int(venue_order_id)
        except (TypeError, ValueError) as exc:
            raise BinanceTestnetError("startup trade history requires a numeric venue order id.") from exc
        if requested_order_id <= 0:
            raise BinanceTestnetError("startup trade history requires a positive venue order id.")
        body = self._request(
            PATH_MY_TRADES,
            {"symbol": symbol, "orderId": requested_order_id, "limit": 1000},
            weight=_WEIGHT_READ_MY_TRADES_WITH_ORDER,
            is_write=False,
            signed=True,
        )
        if not isinstance(body, list):
            raise BinanceTestnetError("startup myTrades must return a list.")
        if len(body) >= 1000:
            raise BinanceTestnetError(
                "startup myTrades returned the maximum page size; completeness is not provable."
            )
        trades = tuple(
            self._parse_startup_trade(item, symbol=symbol, order_id=requested_order_id)
            for item in body
        )
        ids = [int(trade.trade_id) for trade in trades]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise BinanceTestnetError(
                "startup myTrades trade ids must be strictly ordered and unique."
            )
        return trades

    def _parse_startup_trade(
        self, body: Any, *, symbol: str, order_id: int
    ) -> VenueTrade:
        if not isinstance(body, Mapping):
            raise BinanceTestnetError("startup trade evidence must be an object.")
        if body.get("symbol") != symbol or type(body.get("orderId")) is not int:
            raise BinanceTestnetError("startup trade symbol or order identity is invalid.")
        if body["orderId"] != order_id or type(body.get("id")) is not int or body["id"] < 0:
            raise BinanceTestnetError("startup trade order/trade identity is invalid.")
        values: dict[str, Decimal] = {}
        for name, positive in (
            ("price", True), ("qty", True), ("quoteQty", True), ("commission", False)
        ):
            raw = body.get(name)
            if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
                raise BinanceTestnetError(f"startup trade {name} is missing or malformed.")
            try:
                value = Decimal(str(raw))
            except InvalidOperation as exc:
                raise BinanceTestnetError(f"startup trade {name} is malformed.") from exc
            if not value.is_finite() or value < 0 or (positive and value <= 0):
                raise BinanceTestnetError(f"startup trade {name} is not a valid finite amount.")
            values[name] = value
        commission_asset = body.get("commissionAsset")
        timestamp = body.get("time")
        is_buyer = body.get("isBuyer")
        is_maker = body.get("isMaker")
        if not isinstance(commission_asset, str) or not commission_asset.strip():
            raise BinanceTestnetError("startup trade commissionAsset is missing or malformed.")
        if type(timestamp) is not int or timestamp <= 0:
            raise BinanceTestnetError("startup trade time is missing or malformed.")
        if type(is_buyer) is not bool or type(is_maker) is not bool:
            raise BinanceTestnetError("startup trade buyer/maker flags are missing or malformed.")
        trade_time = utc_from_ms(timestamp)
        assert trade_time is not None
        return VenueTrade(
            symbol=symbol,
            venue_order_id=str(order_id),
            trade_id=str(body["id"]),
            price=values["price"],
            quantity=values["qty"],
            quote_quantity=values["quoteQty"],
            commission=values["commission"],
            commission_asset=commission_asset,
            trade_time=trade_time,
            is_buyer=is_buyer,
            is_maker=is_maker,
        )

    def _parse_startup_order(
        self, body: Any, symbol: str, reference: str | None = None,
    ) -> TestnetOrder:
        if not isinstance(body, Mapping):
            raise BinanceTestnetError("startup order evidence must be an object.")
        identity = body.get("clientOrderId")
        status = body.get("status")
        venue_id = body.get("orderId")
        if (
            not isinstance(identity, str) or not identity.strip()
            or (reference is not None and identity != reference)
            or body.get("symbol") != symbol
            or not isinstance(status, str) or status not in STATUS_TO_STATE
            or type(venue_id) is not int or venue_id <= 0
        ):
            raise BinanceTestnetError("startup order identity, symbol or status is invalid.")
        quantities = []
        for name in ("origQty", "executedQty"):
            value = body.get(name)
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise BinanceTestnetError("startup order quantities are missing or malformed.")
            try:
                parsed = Decimal(str(value))
            except InvalidOperation as exc:
                raise BinanceTestnetError("startup order quantity is malformed.") from exc
            if not parsed.is_finite() or parsed < 0:
                raise BinanceTestnetError("startup order quantity is not finite/nonnegative.")
            quantities.append(parsed)
        original, executed = quantities
        if (
            original <= 0 or executed > original
            or (status == "FILLED" and executed != original)
            or (status == "NEW" and executed != 0)
            or (status == "PARTIALLY_FILLED" and not 0 < executed < original)
        ):
            raise BinanceTestnetError("startup order status/quantities are inconsistent.")
        # Construct only the reconciliation fields; ancillary legacy defaults cannot hide
        # missing identity/status/quantity evidence or clamp inconsistent quantities.
        return TestnetOrder(
            symbol=symbol, client_order_id=identity, venue_order_id=str(venue_id),
            status=status, state=STATUS_TO_STATE[status], original_quantity=original,
            executed_quantity=executed,
        )

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
        classify_order_absence: bool = False,
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

            if (
                classify_order_absence and not is_write and resolved_method == "GET"
                and path == PATH_ORDER and response.status_code == 400
                and isinstance(response.body, Mapping)
                and type(response.body.get("code")) is int
                and response.body["code"] == -2013
            ):
                raise OrderNotFoundError("the signed order read confirmed order absence (-2013).")
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
