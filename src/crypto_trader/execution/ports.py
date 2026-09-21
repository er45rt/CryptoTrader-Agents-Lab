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
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Protocol, Sequence, runtime_checkable
from uuid import UUID, uuid4

from .errors import RuntimeStateError, UnsupportedExecutionRequestError

__all__ = [
    "BookLevel",
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
]

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")


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
