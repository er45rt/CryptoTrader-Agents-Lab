"""Engine-independent execution boundary (Phase 4).

This module defines what our application/domain layer is allowed to know about execution:
typed requests, typed observations, a port protocol, and typed errors. It contains **zero**
Nautilus imports and no Nautilus types, by design and enforced by a regression test.

Scope discipline:
* The boundary does not claim risk approval. A request is an *execution instruction*, and the
  Phase 3 ``RiskDecisionRecorded`` contract remains the only place risk outcomes live.
* Monetary and quantity values are :class:`decimal.Decimal`; nothing is silently rounded.
* Unsupported combinations raise :class:`UnsupportedExecutionRequestError` (fail closed).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Protocol, Sequence, runtime_checkable
from uuid import UUID, uuid4

from .errors import NonDurableExecutionError, RuntimeStateError, UnsupportedExecutionRequestError

__all__ = [
    "BookLevel",
    "DurableAttemptJournal",
    "DurableVenueWritePort",
    "ExecutionCapability",
    "ExecutionObservation",
    "ExecutionOrderType",
    "ExecutionPort",
    "ExecutionRequest",
    "ExecutionRuntimeState",
    "ExecutionSide",
    "ExecutionTimeInForce",
    "InstrumentSpec",
    "MarketBookUpdate",
    "MarketTrade",
    "OrderLifecycleState",
    "PreparedVenueWrite",
]

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")


class ExecutionCapability(str, Enum):
    """What an execution port is explicitly declared able to do with venue state.

    UNKNOWN CAPABILITY IS NEVER SAFE CAPABILITY: a missing, malformed or unsupported declaration
    must fail closed at the production gate and never be treated as read-only.

    * ``NON_WRITING`` - the port never writes to an external venue (simulation, research, dry-run
      refusal gates, in-memory doubles). Legitimate non-venue behaviour is preserved.
    * ``DURABLE_VENUE_WRITING`` - the port writes to a venue ONLY through the approved durable
      submission boundary. This value is never sufficient on its own: the port must also inherit
      :class:`DurableVenueWritePort`, whose runtime-owned sequence commits the durable
      ``order.submission.attempted`` marker into the production runtime's own journal before any
      send.
    * ``VENUE_WRITING_UNPROVEN`` - the port may write to a venue without the approved durable
      boundary. The production runtime always refuses it with :class:`NonDurableExecutionError`.

    The declaration is read from the ``capability`` attribute. It is deliberately NOT read via a
    permissive fallback such as ``getattr(port, "writes_to_venue", False)``.
    """

    NON_WRITING = "non_writing"
    DURABLE_VENUE_WRITING = "durable_venue_writing"
    VENUE_WRITING_UNPROVEN = "venue_writing_unproven"

    @property
    def writes_to_venue(self) -> bool:
        """Whether this capability may create risk at an external venue."""
        return self is not ExecutionCapability.NON_WRITING


@runtime_checkable
class DurableAttemptJournal(Protocol):
    """Structural shape of the durable attempt journal used by the approved boundary.

    ``isinstance`` against this Protocol proves only that the members exist: it says nothing about
    whether ``append`` is durable or whether ``read_by_id`` reads the same history. The behavioural
    guarantee comes from the production runtime requiring the port's journal to be *its own*
    authoritative journal object (identity), not from this shape check.
    """

    def append(self, event: object) -> bool: ...

    def read_by_id(self, event_id: object) -> object | None: ...


@dataclass(frozen=True)
class PreparedVenueWrite:
    """An adapter-prepared venue write, opaque to the durable boundary.

    ``handle`` is the adapter's own prepared object (for example a signed request plus its client
    order identity). ``adopted_reference`` marks the idempotent no-write case: the venue already
    knows this order, so no marker is required and no request is sent.
    """

    handle: object
    adopted_reference: str | None = None

    @property
    def requires_write(self) -> bool:
        return self.adopted_reference is None


class DurableVenueWritePort(ABC):
    """The approved durable venue-write boundary (Stage 1 + Stage 2).

    A venue-writing execution port must INHERIT this contract; structural shape alone is never
    accepted. The approved submission sequence is implemented once, here, and cannot be overridden
    by a subclass:

        prepare  ->  build the durable submission-attempt event  ->  durably commit it (append and
        read it back)  ->  bind the exact prepared write to that committed marker  ->  exactly one
        send

    Subclasses supply only the adapter-specific steps. A future implementation error that declares
    durable venue writing while bypassing the approved sequence therefore cannot be accepted: it
    either fails the nominal contract, or its submission still runs this runtime-owned sequence
    against the production runtime's own durable journal.

    The dedicated ``hard-no-submit`` dry-run gate (``crypto_trader.mainnet.nosubmit``) deliberately
    remains undeclared: it is only ever injected into the read-only dry-run pipeline, never into the
    production runtime, so the production gate can never accept it as a writer.
    """

    capability: ExecutionCapability = ExecutionCapability.DURABLE_VENUE_WRITING

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "submit_durably" in cls.__dict__:
            raise TypeError(
                "the approved durable submission sequence must not be overridden: implement the "
                "adapter hooks instead."
            )

    # --- adapter hooks -------------------------------------------------------

    @abstractmethod
    def durable_attempt_journal(self) -> DurableAttemptJournal | None:
        """The durable journal this port commits its submission-attempt marker into."""

    @abstractmethod
    def _prepare_durable_write(self, request: ExecutionRequest) -> PreparedVenueWrite:
        """All locally rejectable preparation (reads, validation, signing, rate admission)."""

    @abstractmethod
    def _durable_attempt_event(
        self, prepared: PreparedVenueWrite, request: ExecutionRequest
    ) -> object:
        """Build the ``order.submission.attempted`` event for this exact prepared write."""

    @abstractmethod
    def _bind_durable_authorization(
        self, prepared: PreparedVenueWrite, marker: object
    ) -> PreparedVenueWrite:
        """Bind the committed marker to the exact prepared write (identity + economic anchors)."""

    @abstractmethod
    def _send_authorized_write(self, prepared: PreparedVenueWrite, request: ExecutionRequest) -> str:
        """Send the already-authorized write exactly once and classify the outcome."""

    # --- the runtime-owned approved sequence ---------------------------------

    def submit_durably(self, request: ExecutionRequest) -> str:
        """The ONLY approved venue-write entry: marker -> authorization -> one send."""
        prepared = self._prepare_durable_write(request)
        if not prepared.requires_write:
            return str(prepared.adopted_reference)
        marker = self._durable_attempt_event(prepared, request)
        journal = self.durable_attempt_journal()
        if journal is None or not isinstance(journal, DurableAttemptJournal):
            raise NonDurableExecutionError(
                "the durable venue-write boundary requires a durable attempt journal; none is "
                "available, so no request may be sent."
            )
        journal.append(marker)
        event_id = getattr(marker, "event_id", None)
        if event_id is None or journal.read_by_id(event_id) is None:
            raise NonDurableExecutionError(
                "the durable submission-attempt marker could not be read back after the append; "
                "refusing to send any request."
            )
        authorized = self._bind_durable_authorization(prepared, marker)
        return self._send_authorized_write(authorized, request)


class ExecutionSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class ExecutionOrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class ExecutionTimeInForce(str, Enum):
    """Only the time-in-force values this boundary can map without guessing."""

    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderLifecycleState(str, Enum):
    """States that the Phase 3 ``order.state.changed`` contract can represent truthfully."""

    CREATED = "created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ExecutionRuntimeState(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    FAILED = "failed"


@dataclass(frozen=True)
class InstrumentSpec:
    """Engine-independent description of the traded instrument."""

    symbol: str
    venue: str = "SIM"
    base_currency: str = "ETH"
    quote_currency: str = "USDT"
    price_precision: int = 2
    size_precision: int = 8
    starting_balance: str = "1_000_000 USDT"

    def __post_init__(self) -> None:
        if not _SYMBOL_PATTERN.match(self.symbol):
            raise UnsupportedExecutionRequestError(
                f"symbol must be an uppercase market symbol such as ETHUSDT; got {self.symbol!r}."
            )
        if self.price_precision < 0 or self.size_precision < 0:
            raise UnsupportedExecutionRequestError("precision values must be non-negative.")


@dataclass(frozen=True)
class ExecutionRequest:
    """An engine-independent instruction to place one order.

    ``intent_id`` and ``correlation_id`` come from the workflow (a Phase 3
    ``TradeIntentCreated`` event carries both) and must survive the round trip unchanged.
    """

    intent_id: UUID
    correlation_id: UUID
    symbol: str
    side: ExecutionSide
    order_type: ExecutionOrderType
    quantity: Decimal
    limit_price: Decimal | None = None
    time_in_force: ExecutionTimeInForce = ExecutionTimeInForce.GTC

    @classmethod
    def create(
        cls,
        *,
        correlation_id: UUID,
        symbol: str,
        side: ExecutionSide,
        order_type: ExecutionOrderType,
        quantity: Decimal,
        limit_price: Decimal | None = None,
        time_in_force: ExecutionTimeInForce = ExecutionTimeInForce.GTC,
        intent_id: UUID | None = None,
    ) -> "ExecutionRequest":
        return cls(
            intent_id=intent_id or uuid4(),
            correlation_id=correlation_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            time_in_force=time_in_force,
        )

    def validate_for(self, instrument: InstrumentSpec) -> None:
        """Fail closed on anything the engine cannot represent faithfully."""
        if self.symbol != instrument.symbol:
            raise UnsupportedExecutionRequestError(
                f"request symbol {self.symbol!r} does not match the configured instrument "
                f"{instrument.symbol!r}."
            )
        if not isinstance(self.quantity, Decimal):
            raise UnsupportedExecutionRequestError("quantity must be a Decimal.")
        if self.quantity <= 0:
            raise UnsupportedExecutionRequestError("quantity must be greater than zero.")
        if -self.quantity.as_tuple().exponent > instrument.size_precision:
            raise UnsupportedExecutionRequestError(
                f"quantity {self.quantity} exceeds the instrument size precision "
                f"({instrument.size_precision}); the boundary never rounds quantities."
            )

        if self.order_type is ExecutionOrderType.LIMIT:
            if self.limit_price is None:
                raise UnsupportedExecutionRequestError("limit orders require a limit_price.")
            if not isinstance(self.limit_price, Decimal) or self.limit_price <= 0:
                raise UnsupportedExecutionRequestError("limit_price must be a positive Decimal.")
            if -self.limit_price.as_tuple().exponent > instrument.price_precision:
                raise UnsupportedExecutionRequestError(
                    f"limit_price {self.limit_price} exceeds the instrument price precision "
                    f"({instrument.price_precision}); the boundary never rounds prices."
                )
        elif self.limit_price is not None:
            raise UnsupportedExecutionRequestError("market orders must not carry a limit_price.")


@dataclass(frozen=True)
class ExecutionObservation:
    """Engine-independent observation of order lifecycle / fill state."""

    order_reference: str
    intent_id: UUID
    correlation_id: UUID
    symbol: str
    state: OrderLifecycleState | None
    filled_quantity: Decimal
    remaining_quantity: Decimal
    last_fill_quantity: Decimal | None = None
    last_fill_price: Decimal | None = None
    fee: Decimal | None = None
    fee_asset: str | None = None
    ts_ns: int = 0
    producer: str = "infrastructure.nautilus"


@dataclass(frozen=True)
class BookLevel:
    """One displayed price level (engine-independent inbound market data)."""

    side: str  # "BUY" | "SELL"
    price: Decimal
    size: Decimal
    order_id: int


@dataclass(frozen=True)
class MarketBookUpdate:
    """A book snapshot/update expressed in engine-independent terms."""

    levels: Sequence[BookLevel]
    ts_ns: int
    sequence: int = 1
    action: str = "ADD"  # ADD | UPDATE | DELETE


@dataclass(frozen=True)
class MarketTrade:
    """An aggressive trade expressed in engine-independent terms."""

    price: Decimal
    size: Decimal
    aggressor: str  # BUYER | SELLER
    ts_ns: int
    trade_id: str


@runtime_checkable
class ExecutionPort(Protocol):
    """The only execution surface the application layer may depend on."""

    def start(self) -> None: ...

    def submit(self, request: ExecutionRequest) -> str: ...

    def observations(self) -> Sequence[ExecutionObservation]: ...

    def state(self) -> ExecutionRuntimeState: ...

    def stop(self) -> None: ...
