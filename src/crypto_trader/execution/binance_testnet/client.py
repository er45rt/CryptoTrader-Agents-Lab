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
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
import math
import re
import time
from threading import Lock
from types import MappingProxyType
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from ...events import OrderSubmissionAttempted
from ...persistence import JournalError, SQLiteEventJournal, check_journalable
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
    RateLimitBanError,
    RateLimitError,
    WriteIpBannedOutcomeUnknownError,
    WriteRateLimitedOutcomeUnknownError,
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
    VenueAssetBalance,
    VenueAccountSnapshot,
    MarkEvidence,
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
    RequestsTransport,
    ScriptedTransport,
    Transport,
    TransportError,
    TransportTimeout,
    safe_transport_error,
    redact_venue_message,
)
from .translate import (
    PRODUCER,
    deterministic_order_uuid,
    testnet_order_to_phase3_events,
    testnet_order_venue_identity_event,
)

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
    journal: SQLiteEventJournal = field(repr=False, compare=False)
    journal_id: UUID
    marker: OrderSubmissionAttempted = field(repr=False, compare=False)
    transport: Transport = field(repr=False, compare=False)
    writer_lease: object | None = field(repr=False, compare=False)


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
    # Mint before journaling so historical markers cannot authorize a fresh preparation.
    attempt_marker_event_id: UUID = field(default_factory=uuid4)

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
_REST_PATHS = frozenset({
    PATH_TIME, PATH_EXCHANGE_INFO, PATH_ACCOUNT, PATH_ORDER,
    PATH_OPEN_ORDERS, PATH_MY_TRADES, PATH_PRICE, f"{PATH_ORDER}/test",
})

_WEIGHT_READ_ORDER = 2
_WEIGHT_READ_OPEN_ORDERS = 3
_WEIGHT_READ_MY_TRADES_WITH_ORDER = 5
_WEIGHT_ACCOUNT = 20
_ACCOUNT_BALANCE_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_MARK_PRICE_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_WEIGHT_EXCHANGE_INFO = 20
_WEIGHT_WRITE = 1
# Review V1/V3: the decimal exponents a venue number may have (see _in_domain).
_VENUE_EXPONENT_LIMIT = 100

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
    _prepared_writes: dict[str, PreparedTestnetOrder] = field(default_factory=dict, init=False, repr=False)
    _authorized_writes: dict[str, PreparedTestnetOrder | None] = field(default_factory=dict, init=False, repr=False)
    _authorization_lock: Any = field(default_factory=Lock, init=False, repr=False)
    # Only populated by a fully parsed signed startup account snapshot.
    _authenticated_venue_account_id: str | None = field(default=None, init=False, repr=False)
    # Bound at authorization and checked again immediately before HTTPS POST.
    _canonical_attempt_journal: SQLiteEventJournal | None = field(default=None, init=False, repr=False)
    canonical_scope: object | None = field(default=None, repr=False)
    _canonical_writer_lease: object | None = field(default=None, init=False, repr=False)
    _authenticated_credentials: TestnetCredentials | None = field(default=None, init=False, repr=False)
    _authenticated_transport: Transport | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self._verified_transport_origin()
        if not math.isfinite(self.rules_ttl_seconds) or self.rules_ttl_seconds <= 0:
            raise TestnetEndpointError("rules_ttl_seconds must be a finite positive number.")
        if self.budget is None:
            self.budget = RateLimitBudget(bucket=TokenBucket(capacity=1200, refill_per_second=20.0))

    def _require_canonical_venue_writer(self, journal: SQLiteEventJournal | None = None, *, request=None) -> None:
        """A real HTTPS POST must share the signed account's canonical FULL journal.

        Scripted offline transports remain test-only observations; no independent
        custom transport is certified as a production network sender by this gate.
        """
        if type(self.transport) is ScriptedTransport:
            # Explicit offline fixture, whose implementation never opens a socket.
            return
        if type(self.transport) is not RequestsTransport:
            raise AttemptJournalRequiredError("unapproved write transport; refusing POST.")
        from ..ports import (
            CanonicalWriterLease, CanonicalWriterScope, CanonicalWriterScopeError,
        )
        scope = self.canonical_scope
        if (type(scope) is not CanonicalWriterScope
                or self._authenticated_venue_account_id is None
                or self._authenticated_venue_account_id != scope.account_id
                or scope.environment != self.venue_label
                or self.credentials is not self._authenticated_credentials
                or self.transport is not self._authenticated_transport):
            raise AttemptJournalRequiredError(
                "real venue POST requires authenticated account and canonical writer scope."
            )
        bound = journal if journal is not None else self._canonical_attempt_journal
        lease = self._canonical_writer_lease
        try:
            scope.require_journal(bound)
            if type(lease) is not CanonicalWriterLease or lease.scope != scope:
                raise CanonicalWriterScopeError("no production-held normal writer proof.")
            lease.require_current(bound)
            if request is not None:
                lease.require_request(request)
        except Exception:
            raise AttemptJournalRequiredError(
                "real venue POST requires a current canonical FULL journal and held normal writer lease."
            ) from None
        if journal is not None:
            self._canonical_attempt_journal = journal

    def _require_independent_write_authority(self) -> None:
        if type(self.transport) is ScriptedTransport:
            return
        # E1/S3: every local proof is in the rollback domain. No independent
        # monotonic authority has been deployed or approved. The offline reference
        # protocol is not deployment evidence or an enabling switch. A static checkpoint
        # or caller-provided reader cannot authorize a real write. This boundary is
        # also rechecked under the final send lock, so restoring files cannot enable
        # an old writer. Replace only with a reviewed durable compare-and-advance
        # protocol bound to this account, session, journal hash and exact intent.
        raise AttemptJournalRequiredError(
            "real venue order-creation POST requires verified independent monotonic authority; "
            "deployment unavailable, writer remains fail-closed."
        )

    def _verified_transport_origin(self) -> str:
        """Capture a canonical Testnet origin before any sensitive request construction.

        The frozen shared endpoint validator still handles documented mainnet host
        rejection. Reject all extra URL components *before* calling that parser,
        so malformed userinfo can never appear in a parser exception.
        """
        candidate = self.base_url
        if type(candidate) is not str or not re.fullmatch(
            r"https://[a-z0-9.-]+/?", candidate.lower()
        ):
            raise TestnetEndpointError(
                "Spot Testnet REST requests require the canonical HTTPS origin."
            )
        checked = require_testnet_base_url(candidate)
        if checked.lower().rstrip("/") != SPOT_TESTNET_REST_BASE:
            raise TestnetEndpointError(
                "Spot Testnet REST requests require the canonical HTTPS origin."
            )
        return SPOT_TESTNET_REST_BASE

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

    def startup_mark_evidence(self, symbol: str) -> MarkEvidence:
        """Strict timestamped mark evidence for production equity/risk state."""
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.strip()
            or symbol != symbol.upper()
            or not symbol.isascii()
            or not symbol.isalnum()
        ):
            raise BinanceTestnetError(
                "startup mark symbol must be an uppercase ASCII alphanumeric string."
            )
        body = self._request(PATH_PRICE, {"symbol": symbol}, weight=1, is_write=False)
        if not isinstance(body, Mapping):
            raise BinanceTestnetError("startup mark evidence must be an object.")
        if body.get("symbol") != symbol:
            raise BinanceTestnetError("startup mark evidence symbol does not match the request.")
        raw = body.get("price")
        if not isinstance(raw, str) or not _MARK_PRICE_RE.fullmatch(raw):
            raise BinanceTestnetError("startup mark price is missing or malformed.")
        try:
            value = Decimal(raw)
        except InvalidOperation as exc:
            raise BinanceTestnetError("startup mark price is malformed.") from exc
        if not value.is_finite() or value <= 0:
            raise BinanceTestnetError("startup mark price must be a positive finite Decimal.")
        observed_at = utc_from_ms(self.clock.timestamp_ms())
        if observed_at is None:
            raise BinanceTestnetError("startup mark observation time could not be represented.")
        return MarkEvidence(
            symbol=symbol,
            price=value,
            observed_at=observed_at,
            reference=f"binance.ticker.price:{symbol}",
        )

    def account_snapshot(self, *, asset: str | None = None) -> TestnetAccountState:
        body = self._request(PATH_ACCOUNT, {}, weight=_WEIGHT_ACCOUNT, is_write=False, signed=True)
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

    def startup_account_snapshot(self, *assets: str) -> VenueAccountSnapshot:
        """Strict signed account evidence for the explicitly requested assets."""
        self._authenticated_venue_account_id = None
        self._authenticated_credentials = None
        self._authenticated_transport = None
        credentials, transport = self.credentials, self.transport
        if not assets:
            raise BinanceTestnetError("startup account snapshot requires at least one asset.")
        requested: list[str] = []
        for asset in assets:
            if (
                not isinstance(asset, str)
                or not asset
                or asset != asset.strip()
                or asset != asset.upper()
                or not asset.isascii()
                or not asset.isalnum()
            ):
                raise BinanceTestnetError(
                    "startup account assets must be uppercase ASCII alphanumeric strings."
                )
            if asset in requested:
                raise BinanceTestnetError("startup account assets must be unique.")
            requested.append(asset)

        body = self._request(
            PATH_ACCOUNT,
            {"omitZeroBalances": "false"},
            weight=_WEIGHT_ACCOUNT,
            is_write=False,
            signed=True,
        )
        if not isinstance(body, Mapping):
            raise BinanceTestnetError("startup account evidence must be an object.")
        uid = body.get("uid")
        update_time = body.get("updateTime")
        balances = body.get("balances")
        if type(uid) is not int or uid <= 0:
            raise BinanceTestnetError("startup account uid is missing or malformed.")
        if type(update_time) is not int or update_time <= 0:
            raise BinanceTestnetError("startup account updateTime is missing or malformed.")
        if not isinstance(balances, list):
            raise BinanceTestnetError("startup account balances must be a list.")

        parsed: dict[str, VenueAssetBalance] = {}
        seen_assets: set[str] = set()
        for entry in balances:
            if not isinstance(entry, Mapping):
                raise BinanceTestnetError("startup account balance entries must be objects.")
            name = entry.get("asset")
            if (
                not isinstance(name, str)
                or not name
                or name != name.strip()
                or name != name.upper()
                or not name.isascii()
                or not name.isalnum()
            ):
                raise BinanceTestnetError("startup account contains a malformed asset code.")
            if name in seen_assets:
                raise BinanceTestnetError("startup account contains duplicate asset entries.")
            seen_assets.add(name)
            values: list[Decimal] = []
            for field_name in ("free", "locked"):
                raw = entry.get(field_name)
                if not isinstance(raw, str) or not _ACCOUNT_BALANCE_RE.fullmatch(raw):
                    raise BinanceTestnetError(
                        f"startup account {name} {field_name} balance is missing or malformed."
                    )
                try:
                    value = Decimal(raw)
                except InvalidOperation as exc:
                    raise BinanceTestnetError(
                        f"startup account {name} {field_name} balance is malformed."
                    ) from exc
                if not value.is_finite() or value < 0:
                    raise BinanceTestnetError(
                        f"startup account {name} {field_name} balance must be finite and nonnegative."
                    )
                values.append(value)
            balance = VenueAssetBalance(asset=name, free=values[0], locked=values[1])
            if name in requested:
                parsed[name] = balance

        missing = [asset for asset in requested if asset not in parsed]
        if missing:
            raise BinanceTestnetError(
                "startup account is missing required asset balance(s): " + ", ".join(missing)
            )
        fetched_at = utc_from_ms(self.clock.timestamp_ms())
        if fetched_at is None:
            raise BinanceTestnetError("startup account fetch time could not be represented.")
        snapshot = VenueAccountSnapshot(
            venue_account_id=str(uid),
            venue_update_time_ms=update_time,
            fetched_at=fetched_at,
            balances=tuple(parsed[asset] for asset in requested),
        )
        if self.credentials is not credentials or self.transport is not transport:
            raise BinanceTestnetError("signed account evidence changed during collection.")
        self._authenticated_venue_account_id = snapshot.venue_account_id
        self._authenticated_credentials = credentials
        self._authenticated_transport = transport
        return snapshot

    def verify_environment(self, *, symbol: str | None = None) -> TestnetEnvironment:
        """Verify endpoint, clock, account privileges and instrument rules.

        Fails closed when trading is disabled, and - unless the caller explicitly selected
        ``WithdrawalPolicy.RECORD_ONLY`` for the Binance **testnet** default - when the venue
        reports withdrawals enabled. The observed flag is always recorded.
        """
        trusted_origin = self._verified_transport_origin()
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
            base_url=trusted_origin,
            host=trusted_origin.split("//", 1)[-1],
            venue_label=self.venue_label,
            server_time_ms=self.clock.timestamp_ms(),
            clock_offset_ms=offset,
            account=account,
            symbol=target,
            notes=notes,
        )

    def symbol_rules(
        self, symbol: str | None = None, *, refresh: bool = False, wait: bool = True
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
            PATH_EXCHANGE_INFO,
            {"symbol": target},
            weight=_WEIGHT_EXCHANGE_INFO,
            is_write=False,
            wait=wait,
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

    def await_order_write_admission(self, symbol: str) -> None:
        """Wait outside the submission lock until order preparation should be non-blocking."""
        self.symbol_rules(symbol)
        assert self.budget is not None
        self.budget.wait_for_admission(_WEIGHT_WRITE)

    def prepare_order(
        self, request: TestnetOrderRequest, *, wait: bool = True
    ) -> PreparedTestnetOrder:
        """Do every locally rejectable step for a write, without performing the write.

        Read-only venue calls (an idempotency status lookup and symbol-rule retrieval) are allowed
        here: they happen before the durable attempt marker is committed and cannot mutate an order.
        Rate/backoff admission, the clock precondition, timestamp generation, recvWindow
        construction, signing and serialization also happen here, so the caller can commit a durable
        attempt marker knowing that nothing after it may reject the write locally.

        Budget consumption is intentionally conservative: a token spent before a marker write that
        later fails is never refunded.
        """
        if (
            type(request) is not TestnetOrderRequest
            or any(type(value) is not str for value in (
                request.client_order_id, request.symbol, request.side, request.order_type,
            ))
            or (request.time_in_force is not None and type(request.time_in_force) is not str)
            or type(request.quantity) is not Decimal
            or (request.price is not None and type(request.price) is not Decimal)
            or type(wait) is not bool
        ):
            raise AttemptJournalRequiredError("order security anchors require exact builtin types.")
        trusted_origin = self._verified_transport_origin()
        trusted_url = self._verified_request_url(trusted_origin, PATH_ORDER)
        existing = self._orders.get(request.client_order_id)
        if existing is not None:
            try:
                current = self.adopt_order_status(
                    request,
                    expected_venue_order_id=existing.venue_order_id,
                    wait=wait,
                )
            except BinanceTestnetError as exc:
                raise AmbiguousOrderError(
                    "the cached client-order identity could not be proven to match the venue "
                    "order economics; refusing adoption without another write."
                ) from exc
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

        rules = self.symbol_rules(request.symbol, wait=wait)
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
        if wait:
            self.budget.enforce_backoff()
            self.budget.bucket.acquire(_WEIGHT_WRITE)
        else:
            self.budget.admit_now(_WEIGHT_WRITE)
        signed_payload = self.clock.signed_timestamp_params(params)
        query = signed_query_string(signed_payload, self.credentials.with_secret())
        http_request = HttpRequest(
            method="POST",
            url=trusted_url,
            query=query,
            headers=MappingProxyType(self._headers()),
            weight=_WEIGHT_WRITE,
            is_write=True,
        )
        prepared = PreparedTestnetOrder(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side.lower(),
            order_type=request.order_type.lower(),
            quantity=request.quantity,
            limit_price=request.price,
            time_in_force=(request.time_in_force or "").lower(),
            request=http_request,
        )
        with self._authorization_lock:
            self._prepared_writes[prepared.client_order_id] = prepared
        return prepared

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
        journal: SQLiteEventJournal | None = None,
    ) -> PreparedTestnetOrder:
        """Bind a durably appended attempt marker to the exact prepared write.

        Called ONLY by the production runtime (``execution/binance_testnet/runtime.py``) and only
        after ``journal.append(marker)`` has returned successfully. Verify the committed marker
        through the existing durable SQLite journal, then bind a single-use capability to the
        exact prepared object issued by this client. No persisted contracts change.
        """
        if type(prepared) is not PreparedTestnetOrder:
            raise AttemptJournalRequiredError("attempt authorization expects a prepared order.")
        if (
            any(type(value) is not UUID for value in (
                marker_event_id, order_id, intent_id, correlation_id,
            ))
            or any(type(value) is not str for value in (
                venue_reference, symbol, side, order_type, time_in_force,
            ))
            or type(quantity) is not Decimal
            or (limit_price is not None and type(limit_price) is not Decimal)
        ):
            raise AttemptJournalRequiredError("attempt security anchors require exact builtin types.")
        if prepared.request is None:
            raise AttemptJournalRequiredError(
                "attempt authorization was requested for an order that requires no write."
            )
        expected_order_id = deterministic_order_uuid(prepared.client_order_id)
        mismatches: list[str] = []
        if type(marker_event_id) is not UUID or marker_event_id != prepared.attempt_marker_event_id:
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
        if type(journal) is not SQLiteEventJournal:
            raise AttemptJournalRequiredError("a durable SQLite attempt journal is required.")
        self._require_canonical_venue_writer(journal)
        journal_failed = False
        try:
            journal.require_venue_write_ready()
            marker = journal.read_by_id(marker_event_id)
        except Exception:
            # Any storage-proof failure refuses authority; this adapter never owns DB I/O.
            journal_failed = True
        if journal_failed:
            raise AttemptJournalRequiredError("the committed attempt could not be verified.")
        if type(marker) is not OrderSubmissionAttempted or (
            marker.order_id, marker.correlation_id, marker.symbol,
            marker.payload.venue_reference, marker.payload.intent_id,
            marker.payload.side, marker.payload.order_type, marker.payload.quantity,
            marker.payload.limit_price, marker.payload.time_in_force,
            marker.payload.venue_producer,
        ) != (
            expected_order_id, correlation_id, prepared.symbol, prepared.client_order_id, intent_id,
            prepared.side, prepared.order_type, prepared.quantity, prepared.limit_price,
            prepared.time_in_force, PRODUCER,
        ):
            raise AttemptJournalRequiredError("no matching committed attempt marker exists.")
        if type(self.transport) is RequestsTransport:
            try:
                self._canonical_writer_lease.require_attempt(marker)
            except Exception:
                raise AttemptJournalRequiredError("attempt has no matching production request admission.") from None
        authorized = replace(
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
                journal=journal,
                journal_id=journal.journal_id(),
                marker=marker,
                transport=self.transport,
                writer_lease=(self._canonical_writer_lease
                              if type(self.transport) is RequestsTransport else None),
            ),
        )
        with self._authorization_lock:
            if (
                self._prepared_writes.get(prepared.client_order_id) is not prepared
                or prepared.client_order_id in self._authorized_writes
            ):
                raise AttemptJournalRequiredError("the prepared write is replaced or already authorized.")
            self._authorized_writes[prepared.client_order_id] = authorized
        return authorized

    def _verify_attempt_authorization(self, prepared: PreparedTestnetOrder) -> None:
        """Fail closed unless the prepared write carries a matching durable authorization."""
        authorization = prepared.attempt_authorization
        if type(authorization) is not AttemptAuthorization:
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
        # Consume atomically BEFORE transport: success, pre-send failure and uncertainty
        # all retire this capability. Journal/recovery remain the source of restart truth.
        with self._authorization_lock:
            if self._authorized_writes.get(prepared.client_order_id) is not prepared:
                raise AttemptJournalRequiredError("the write authorization is unknown, replaced or consumed.")
            self._authorized_writes[prepared.client_order_id] = None
            self._prepared_writes.pop(prepared.client_order_id, None)

    def retire_attempt_authorization(self, prepared: PreparedTestnetOrder) -> None:
        """Retire a one-shot capability that a pre-send refusal will never use.

        Consumption in :meth:`submit_prepared` only covers refusals that reach it. A runtime
        pre-send refusal must retire the capability here too: restoring the journal afterwards
        must not resurrect a usable authorization. Idempotent and never adopts a foreign order.
        """
        if type(prepared) is not PreparedTestnetOrder:
            raise AttemptJournalRequiredError(
                "authorization retirement expects a prepared order."
            )
        with self._authorization_lock:
            current = self._authorized_writes.get(prepared.client_order_id)
            if current is prepared:
                self._authorized_writes[prepared.client_order_id] = None
            elif current is not None:
                raise AttemptJournalRequiredError(
                    "the authorization to retire is replaced or foreign."
                )
            self._prepared_writes.pop(prepared.client_order_id, None)

    def submit_prepared(
        self, prepared: PreparedTestnetOrder, *, journal: SQLiteEventJournal | None = None,
    ) -> TestnetOrder:
        """Send the exact prepared request. This is the ONLY venue-write call site.

        The caller has already committed the durable attempt. Recheck its exact
        journal, sender and scoped held lease under the final ownership lock;
        send the prepared request without rate waits, signing or reconstruction.
        """
        if not isinstance(prepared, PreparedTestnetOrder):
            raise BinanceTestnetError("submit_prepared expects a PreparedTestnetOrder.")
        if prepared.existing is not None:
            return replace(prepared.existing, deduplicated=True)
        http_request = prepared.request
        if http_request is None:
            raise BinanceTestnetError("a prepared order without a request cannot be submitted.")
        # Consume before lock acquisition or final checks: refusal never resurrects
        # a capability. The committed attempt remains reserved for reconciliation.
        self._verify_attempt_authorization(prepared)
        authorization = prepared.attempt_authorization
        bound = authorization.journal
        if (self.transport is not authorization.transport
                or (journal is not None and journal is not bound)
                or (type(self.transport) is RequestsTransport and journal is None)):
            raise AttemptJournalRequiredError("final runtime journal or write transport changed.")
        lease = authorization.writer_lease
        guard = nullcontext()
        if lease is not None:
            from ..ports import CanonicalWriterLease
            if (type(lease) is not CanonicalWriterLease
                    or lease is not self._canonical_writer_lease
                    or self._canonical_attempt_journal is not bound):
                raise AttemptJournalRequiredError("final held writer or committed journal changed.")
            guard = lease.send_guard(bound)

        from ...breakglass.errors import OwnershipStateError
        from ..ports import CanonicalWriterScopeError
        failure = None
        try:
            # Keep the canonical ownership lock through the actual send, including
            # transport failure. Never upgrade a fresh/stale fence by rereading it.
            with guard:
                self._require_canonical_venue_writer(bound)
                self._require_independent_write_authority()
                try:
                    bound.require_venue_write_ready()
                    if (bound.journal_id() != authorization.journal_id
                            or bound.read_by_id(authorization.marker_event_id) != authorization.marker):
                        raise AttemptJournalRequiredError("committed attempt journal binding changed.")
                except Exception:
                    raise AttemptJournalRequiredError("final committed attempt proof is unavailable.") from None
                self._request_log.append(http_request.safe_repr())
                response = authorization.transport.send(http_request)
        except (CanonicalWriterScopeError, OwnershipStateError):
            raise AttemptJournalRequiredError("final normal writer lease is unavailable or stale.") from None
        except (TransportTimeout, TransportError) as exc:
            if isinstance(exc, PreSendTransportError):
                failure = safe_transport_error(exc)
            else:
                failure = AmbiguousOrderError(
                    "the write outcome is unknown (transport failure before a response); the "
                    "order is NOT retried and must go through reconciliation."
                )
        if failure is not None:
            raise failure from None
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
            retry_after = response.retry_after_seconds
            try:
                self.budget.record_418(retry_after)
            except RateLimitBanError as exc:
                raise WriteIpBannedOutcomeUnknownError(
                    "the venue signalled an IP ban after the order POST; the final order outcome "
                    "is unknown and must be reconciled.",
                    retry_after_seconds=retry_after,
                ) from exc
        if status_code == 429:
            assert self.budget is not None
            delay = self.budget.record_429(response.retry_after_seconds)
            raise WriteRateLimitedOutcomeUnknownError(
                f"the venue rate-limited the order POST (429); final order outcome is unknown; "
                f"backoff {delay}s and reconcile without retrying.",
                retry_after_seconds=response.retry_after_seconds,
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
                order = self.adopt_order_status(
                    TestnetOrderRequest(
                        client_order_id=prepared.client_order_id,
                        symbol=prepared.symbol,
                        side=prepared.side.upper(),
                        order_type=prepared.order_type.upper(),
                        quantity=prepared.quantity,
                        price=prepared.limit_price,
                        time_in_force=(
                            prepared.time_in_force.upper()
                            if prepared.order_type.lower() == "limit"
                            else None
                        ),
                    ),
                    wait=False,
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
        # HTTP 429/418 were classified above as typed ambiguous rate-limit outcomes: the venue
        # responded, but neither status proves the final order-processing outcome. The generic
        # deterministic-rejection branch below is only for responses whose order outcome is known.
        raise TestnetRejectedError(
            f"the testnet venue rejected the request (HTTP {status_code}, code "
            f"{code}): {self._safe_message(message)}"
        )

    @staticmethod
    def _require_matching_fills(body: Mapping[str, Any]) -> None:
        """Our write answer's fills sum to its executed quantity and its cumulative quote."""
        fills = body.get("fills")
        if fills is None:
            fills = []
        if not isinstance(fills, list) or not all(isinstance(item, Mapping) for item in fills):
            raise BinanceTestnetError("the write answer fills are malformed.")
        # Review RV1-RV3: each fill is proved before anything is journaled. The journal keys a fill
        # by its trade id, so a repeated one would be journaled once while counted twice here.
        quantity = quote = Decimal("0")
        trade_ids: set[str] = set()
        try:
            for item in fills:
                fill_quantity = Decimal(str(item["qty"]))
                fill_price = Decimal(str(item["price"]))
                commission = Decimal(str(item.get("commission", "0")))
                trade_id = item.get("tradeId")
                if (
                    not fill_quantity.is_finite() or fill_quantity <= 0
                    or not fill_price.is_finite() or fill_price <= 0
                    or not commission.is_finite() or commission < 0
                    or isinstance(trade_id, bool) or trade_id is None or not str(trade_id).strip()
                    or str(trade_id) in trade_ids
                ):
                    raise BinanceTestnetError("the write answer has a malformed or repeated fill.")
                trade_ids.add(str(trade_id))
                quantity += fill_quantity
                quote += fill_quantity * fill_price
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise BinanceTestnetError("the write answer fills are malformed.") from exc
        if quantity != Decimal(str(body["executedQty"])) or quote != Decimal(str(body["cummulativeQuoteQty"])):
            raise BinanceTestnetError("the write answer fills contradict its execution.")

    def _parse_write_success(self, body: Any, prepared: PreparedTestnetOrder) -> TestnetOrder:
        """A write must be mechanically recognizable as successful, or it is ambiguous."""
        if not isinstance(body, Mapping):
            raise AmbiguousOrderError(
                "the write returned HTTP 200 without a usable order payload (empty or non-JSON "
                "body); the order outcome is unknown and the order is not retried."
            )
        try:
            # Review A-1: this answer becomes our journaled state and venue identity, so it gets the
            # strict proof a cancel answer or an adoption read gets before the lenient parse.
            self._parse_startup_order(body, prepared.symbol, prepared.client_order_id)
            # Review A1-W1-01: the fills are journaled as our executions, so they must agree with the
            # answer they came in. Our FULL answer lists every fill of an execution.
            self._require_matching_fills(body)
            order = self._parse_order(body)
            # Review RV-F1..RV-F4, L1/L2: accepted only once every event it will be journaled as, our
            # venue identity included, can be built and stored, so no part of it can fail after it is
            # cached or after our identity or state is written.
            events = list(testnet_order_to_phase3_events(order, correlation_id=UUID(int=0)))
            if order.client_order_id and order.venue_order_id:
                events.append(
                    testnet_order_venue_identity_event(order, intent_id=UUID(int=0), correlation_id=UUID(int=0))
                )
            for event in events:
                check_journalable(event)
            # Review V1: and once the observation it is reported as can be built.
            order.to_observation(intent_id=UUID(int=0), correlation_id=UUID(int=0))
        except (BinanceTestnetError, JournalError, ValueError, TypeError, ArithmeticError) as exc:
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

    def adopt_order_status(
        self,
        request: TestnetOrderRequest,
        *,
        expected_venue_order_id: str | None = None,
        wait: bool = True,
    ) -> TestnetOrder:
        """Strictly re-read and adopt an existing order without cache mutation on mismatch."""
        body = self._request(
            PATH_ORDER,
            {"symbol": request.symbol, "origClientOrderId": request.client_order_id},
            weight=_WEIGHT_READ_ORDER,
            is_write=False,
            signed=True,
            wait=wait,
        )
        expected_id = expected_venue_order_id
        if expected_id is None:
            cached = self._orders.get(request.client_order_id)
            if cached is not None and cached.venue_order_id is not None:
                expected_id = cached.venue_order_id
        try:
            order = self._validate_adoption_response(
                body, request, expected_venue_order_id=expected_id
            )
        except BinanceTestnetError:
            raise
        except Exception as exc:
            raise BinanceTestnetError("order adoption response was malformed.") from exc
        self._orders[order.client_order_id] = order
        return order

    def _validate_adoption_response(
        self,
        body: Any,
        request: TestnetOrderRequest,
        *,
        expected_venue_order_id: str | None = None,
    ) -> TestnetOrder:
        if not isinstance(body, Mapping):
            raise BinanceTestnetError("order adoption response must be an object.")
        for name in ("origQty", "executedQty"):
            raw = body.get(name)
            if isinstance(raw, (bool, float)) or not isinstance(raw, (str, int)):
                raise BinanceTestnetError(
                    f"order adoption {name} must be a canonical decimal value."
                )

        strict = self._parse_startup_order(body, request.symbol, request.client_order_id)
        if strict.side != request.side:
            raise BinanceTestnetError("order adoption side contradicts the prepared order.")
        if strict.original_quantity != request.quantity:
            raise BinanceTestnetError("order adoption quantity contradicts the prepared order.")
        if (
            expected_venue_order_id is not None
            and strict.venue_order_id != expected_venue_order_id
        ):
            raise BinanceTestnetError("order adoption venue identity changed unexpectedly.")
        if body.get("type") != request.order_type:
            raise BinanceTestnetError("order adoption type contradicts the prepared order.")
        raw_tif = body.get("timeInForce")
        if not isinstance(raw_tif, str):
            raise BinanceTestnetError("order adoption timeInForce is missing or malformed.")
        if request.order_type == "LIMIT":
            if raw_tif != request.time_in_force:
                raise BinanceTestnetError(
                    "order adoption timeInForce contradicts the prepared order."
                )
            if strict.price != request.price:
                raise BinanceTestnetError("order adoption price contradicts the prepared order.")

        # Strict proof above prevents the legacy parser's permissive defaults from hiding missing
        # fields, while this parse preserves fills and venue timestamps for downstream recording.
        order = replace(self._parse_order(body), side=strict.side)
        if (
            order.client_order_id != request.client_order_id
            or order.symbol != request.symbol
            or order.venue_order_id != strict.venue_order_id
            or order.status != strict.status
            or order.side != strict.side
            or order.original_quantity != strict.original_quantity
            or order.executed_quantity != strict.executed_quantity
        ):
            raise BinanceTestnetError("order adoption parsing changed validated identity/economics.")
        return order

    def order_status(
        self,
        symbol: str,
        client_order_id: str,
        *,
        require_exact_identity: bool = False,
        wait: bool = True,
    ) -> TestnetOrder:
        body = self._request(
            PATH_ORDER,
            {"symbol": symbol, "origClientOrderId": client_order_id},
            weight=_WEIGHT_READ_ORDER,
            is_write=False,
            signed=True,
            wait=wait,
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
        if (
            not isinstance(venue_order_id, str)
            or not venue_order_id.isascii()
            or not venue_order_id.isdigit()
            or venue_order_id.startswith("0")
        ):
            raise BinanceTestnetError(
                "startup trade history requires a canonical positive ASCII venue order id."
            )
        requested_order_id = int(venue_order_id)
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
        if body["orderId"] != order_id or type(body.get("id")) is not int or body["id"] <= 0:
            raise BinanceTestnetError("startup trade order/trade identity is invalid.")
        values: dict[str, Decimal] = {}
        for name, positive in (
            ("price", True), ("qty", True), ("quoteQty", True), ("commission", False)
        ):
            raw = body.get(name)
            if not isinstance(raw, str):
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
        try:
            trade_time = utc_from_ms(timestamp)
        except (OSError, OverflowError, ValueError) as exc:
            raise BinanceTestnetError("startup trade time is outside the supported range.") from exc
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
        side = body.get("side")
        if (
            not isinstance(identity, str) or not identity.strip()
            or (reference is not None and identity != reference)
            or body.get("symbol") != symbol
            or not isinstance(status, str) or status not in STATUS_TO_STATE
            or type(venue_id) is not int or venue_id <= 0
            or not isinstance(side, str) or side not in {"BUY", "SELL"}
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
        raw_price = body.get("price")
        if not isinstance(raw_price, str):
            raise BinanceTestnetError("startup order price is missing or malformed.")
        try:
            order_price = Decimal(raw_price)
        except InvalidOperation as exc:
            raise BinanceTestnetError("startup order price is malformed.") from exc
        if not order_price.is_finite() or order_price < 0:
            raise BinanceTestnetError("startup order price is not finite/nonnegative.")
        raw_quote = body.get("cummulativeQuoteQty")
        if not isinstance(raw_quote, str):
            raise BinanceTestnetError("startup order cumulative quote quantity is missing or malformed.")
        try:
            cumulative_quote = Decimal(raw_quote)
        except InvalidOperation as exc:
            raise BinanceTestnetError("startup order cumulative quote quantity is malformed.") from exc
        if not cumulative_quote.is_finite() or cumulative_quote < 0:
            raise BinanceTestnetError(
                "startup order cumulative quote quantity is not finite/nonnegative."
            )
        if (
            original <= 0 or executed > original
            or (status == "FILLED" and executed != original)
            or (status == "NEW" and executed != 0)
            or (status == "PARTIALLY_FILLED" and not 0 < executed < original)
            or ((executed == 0) != (cumulative_quote == 0))
        ):
            raise BinanceTestnetError("startup order status/quantities are inconsistent.")
        # Construct only the reconciliation fields; ancillary legacy defaults cannot hide
        # missing identity/status/quantity evidence or clamp inconsistent quantities.
        return TestnetOrder(
            symbol=symbol, client_order_id=identity, venue_order_id=str(venue_id),
            status=status, state=STATUS_TO_STATE[status], side=side, price=order_price,
            original_quantity=original, executed_quantity=executed,
            cummulative_quote_quantity=cumulative_quote,
        )

    def cancel_all_open_orders(self, symbol: str) -> tuple[TestnetOrder, ...]:
        """Cancel every open order for ``symbol`` (the Safety Lane cancel-all action).

        A failure part-way never forgets what was already done. The raised error carries
        ``cancelled_order_references`` (the cancels the venue confirmed before the failure) and
        ``unconfirmed_order_reference`` (the order whose cancel failed, or None when listing the
        open orders failed). Only the former are venue-confirmed cancels.
        """
        results: list[TestnetOrder] = []
        pending: str | None = None
        try:
            # Review FZ-10: the strict listing refuses a body that is not a list (a 200 that is empty
            # or not JSON arrives as None), so it is never taken as "no open orders".
            for order in self.startup_open_orders(symbol):
                # Adopted as the lenient listing did, so the cancel cross-checks the listed order.
                self._orders[order.client_order_id] = order
                pending = order.client_order_id
                results.append(self.cancel_order(symbol, pending))
                pending = None
        except BinanceTestnetError as exc:
            exc.cancelled_order_references = tuple(done.client_order_id for done in results)
            exc.unconfirmed_order_reference = pending
            raise
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
            f"{PATH_ORDER}/test", params, weight=1, is_write=True,
            signed=True, method="POST",
        )
        return body if isinstance(body, Mapping) else {}

    def reconnect(self) -> TestnetEnvironment:
        """Re-verify connectivity, clock and permissions after an interruption."""
        return self.verify_environment()

    # --- transport plumbing -------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {API_KEY_HEADER: self.credentials.api_key}

    @staticmethod
    def _verified_request_url(origin: str, path: str) -> str:
        """Validate the complete destination before credentials are attached."""
        if type(path) is not str or path not in _REST_PATHS:
            raise TestnetEndpointError("the REST path is not allowlisted.")
        url = f"{origin}{path}"
        parsed = urlsplit(url)
        if (
            f"{parsed.scheme}://{parsed.netloc}" != SPOT_TESTNET_REST_BASE
            or parsed.username is not None or parsed.password is not None
            or parsed.path != path or parsed.query or parsed.fragment
        ):
            raise TestnetEndpointError("the REST destination is not a trusted Testnet URL.")
        return url

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
        wait: bool = True,
    ) -> Any:
        # Never invoke subclass equality/formatting/upper hooks at the fence.
        # These immutable builtin values are shared by validation, send and retries.
        if (
            type(path) is not str
            or (method is not None and type(method) is not str)
            or any(type(flag) is not bool for flag in (
                is_write, signed, classify_order_absence, wait,
            ))
        ):
            raise AttemptJournalRequiredError(
                "REST security inputs must use exact builtin types."
            )
        trusted_origin = self._verified_transport_origin()
        trusted_url = self._verified_request_url(trusted_origin, path)
        resolved_method = method or ("POST" if is_write else "GET")
        allowed_method = (
            (resolved_method == "GET" and not is_write)
            or (resolved_method == "DELETE" and is_write and signed and path == PATH_ORDER)
            or (
                resolved_method == "POST" and is_write and signed
                and path == f"{PATH_ORDER}/test"
            )
        )
        if not allowed_method:
            raise AttemptJournalRequiredError(
                "the direct REST helper permits only reads, signed safety cancels, and "
                "non-placing order tests; creating an order requires a durable attempt."
            )
        assert self.budget is not None
        attempt = 0
        resync_used = False
        while True:
            attempt += 1
            if wait:
                self.budget.enforce_backoff()
                self.budget.bucket.acquire(weight)
            else:
                self.budget.admit_now(weight)

            payload = dict(params)
            if signed:
                payload = self.clock.signed_timestamp_params(payload)
                query = signed_query_string(payload, self.credentials.with_secret())
            else:
                query = build_query_string(payload)

            request = HttpRequest(
                method=resolved_method,
                url=trusted_url,
                query=query,
                headers=self._headers(),
                weight=weight,
                is_write=is_write,
            )
            # Only the redacted form is ever recorded: the API key and signature never enter
            # the log, the journal, a report or a test failure diff.
            self._request_log.append(request.safe_repr())

            failure = None
            try:
                response = self.transport.send(request)
            except (TransportTimeout, TransportError) as exc:
                failure = safe_transport_error(exc)
                if is_write and not isinstance(exc, PreSendTransportError):
                    failure = AmbiguousOrderError(
                        "the write outcome is unknown (transport failure before a response); the "
                        "order is NOT retried and must go through reconciliation."
                    )
            if failure is not None:
                if is_write or not wait or attempt >= self.retry.max_attempts:
                    raise failure from None
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
                if not wait or attempt >= self.retry.max_attempts:
                    raise RateLimitError("rate limited (429) and retries are exhausted.")
                continue

            if 500 <= response.status_code < 600:
                if is_write:
                    raise AmbiguousOrderError(
                        f"the venue returned {response.status_code} for a write; the outcome is "
                        "unknown and the order is not retried."
                    )
                if not wait or attempt >= self.retry.max_attempts:
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
                if not wait:
                    raise ClockDriftError(
                        "the venue rejected the timestamp (-1021); no-wait reads never resync "
                        "inside the submission lock."
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
        """Mask encoded credentials and signed query echoes, retaining safe diagnostics."""
        return redact_venue_message(
            message, (self.credentials.api_key, self.credentials.with_secret())
        )

    @staticmethod
    def _in_domain(value: Decimal) -> Decimal:
        """Review V1/V3: a venue number lies in the domain our arithmetic runs in, or it is malformed.

        Real venue numbers are far inside 10**-100..10**100; outside it, a later sum or product of
        them (remaining quantity, fees, risk) can overflow after the order is journaled.
        """
        if value.is_finite() and value and not -_VENUE_EXPONENT_LIMIT <= value.adjusted() <= _VENUE_EXPONENT_LIMIT:
            raise ValueError("a venue number is outside the arithmetic domain.")
        return value

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
                    price=self._in_domain(Decimal(str(item.get("price", "0")))),
                    quantity=self._in_domain(Decimal(str(item.get("qty", "0")))),
                    commission=self._in_domain(Decimal(str(item.get("commission", "0")))),
                    commission_asset=(
                        None if item.get("commissionAsset") is None
                        else str(item["commissionAsset"])
                    ),
                    trade_id=None if item.get("tradeId") is None else str(item["tradeId"]),
                )
                for item in (body.get("fills") or ())
            )
            # A missing executedQty in a venue READ cannot mean zero execution.
            if "executedQty" not in body or isinstance(body["executedQty"], bool):
                raise BinanceTestnetError("venue order read lacks explicit executedQty.")
            executed = self._in_domain(Decimal(str(body["executedQty"])))
            return TestnetOrder(
                symbol=str(body.get("symbol", self.symbol)),
                client_order_id=str(
                    body.get("clientOrderId") or body.get("origClientOrderId") or ""
                ),
                venue_order_id=None if body.get("orderId") is None else str(body["orderId"]),
                state=state,
                status=status,
                original_quantity=self._in_domain(Decimal(str(body.get("origQty", executed)))),
                executed_quantity=executed,
                cummulative_quote_quantity=self._in_domain(Decimal(
                    str(body.get("cummulativeQuoteQty", body.get("cumulativeQuoteQty", "0")))
                )),
                price=self._in_domain(Decimal(str(body.get("price", "0")))),
                fills=fills,
                transact_time=utc_from_ms(
                    body.get("transactTime") or body.get("updateTime") or body.get("time")
                ),
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise BinanceTestnetError(f"the testnet order response was malformed: {exc}") from exc
