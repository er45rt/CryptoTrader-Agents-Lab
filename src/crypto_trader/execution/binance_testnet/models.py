"""Engine-independent models for the Spot Testnet adapter (Phase 11).

Orders, fills and account state are expressed in OUR vocabulary (the Phase 4 lifecycle states and
Phase 3 contracts) with :class:`decimal.Decimal` values. Nothing here holds a credential.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from ..ports import ExecutionObservation, OrderLifecycleState

__all__ = [
    "TestnetAccountState",
    "TestnetEnvironment",
    "TestnetFill",
    "TestnetOrder",
    "TestnetOrderRequest",
    "TestnetSymbolRules",
    "format_decimal",
]

ZERO = Decimal("0")

#: Binance Spot order statuses mapped to our lifecycle vocabulary. Explicit and total: an
#: unrecognised status raises rather than being coerced.
STATUS_TO_STATE: dict[str, OrderLifecycleState] = {
    "NEW": OrderLifecycleState.ACCEPTED,
    "PARTIALLY_FILLED": OrderLifecycleState.PARTIALLY_FILLED,
    "FILLED": OrderLifecycleState.FILLED,
    "CANCELED": OrderLifecycleState.CANCELED,
    "PENDING_CANCEL": OrderLifecycleState.ACCEPTED,
    "REJECTED": OrderLifecycleState.REJECTED,
    "EXPIRED": OrderLifecycleState.EXPIRED,
    "EXPIRED_IN_MATCH": OrderLifecycleState.EXPIRED,
}


@dataclass(frozen=True)
class TestnetOrderRequest:
    """An engine-independent order request for the Spot Testnet."""

    client_order_id: str
    symbol: str
    side: str  # BUY | SELL
    order_type: str  # LIMIT | MARKET
    quantity: Decimal
    price: Decimal | None = None
    time_in_force: str | None = None  # GTC | IOC | FOK (limit orders only)

    def __post_init__(self) -> None:
        if not self.client_order_id:
            raise ValueError("a client order id is required (it is the idempotency key).")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported side {self.side!r}.")
        if self.order_type not in {"LIMIT", "MARKET"}:
            raise ValueError(f"unsupported order type {self.order_type!r}.")
        if not isinstance(self.quantity, Decimal) or self.quantity <= 0:
            raise ValueError("quantity must be a positive Decimal.")
        if self.order_type == "LIMIT":
            if not isinstance(self.price, Decimal) or self.price <= 0:
                raise ValueError("LIMIT orders require a positive Decimal price.")
            if not self.time_in_force:
                raise ValueError("LIMIT orders require an explicit time_in_force.")
        elif self.price is not None or self.time_in_force is not None:
            raise ValueError("MARKET orders must not carry price or time_in_force.")


@dataclass(frozen=True)
class TestnetFill:
    """One reported fill."""

    price: Decimal
    quantity: Decimal
    commission: Decimal = ZERO
    commission_asset: str | None = None
    trade_id: str | None = None


@dataclass(frozen=True)
class TestnetOrder:
    """The venue's view of one order, in our vocabulary."""

    symbol: str
    client_order_id: str
    venue_order_id: str | None
    state: OrderLifecycleState
    status: str
    original_quantity: Decimal = ZERO
    executed_quantity: Decimal = ZERO
    cummulative_quote_quantity: Decimal = ZERO
    price: Decimal = ZERO
    fills: tuple[TestnetFill, ...] = ()
    transact_time: datetime | None = None
    deduplicated: bool = False

    @property
    def remaining_quantity(self) -> Decimal:
        return max(ZERO, self.original_quantity - self.executed_quantity)

    @property
    def is_terminal(self) -> bool:
        return self.state in {
            OrderLifecycleState.FILLED,
            OrderLifecycleState.CANCELED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.EXPIRED,
        }

    def to_observation(
        self,
        *,
        intent_id: UUID,
        correlation_id: UUID,
        producer: str = "infrastructure.binance_testnet",
    ) -> ExecutionObservation:
        """Expose the order through the Phase 4 engine-independent observation type."""
        last_fill = self.fills[-1] if self.fills else None
        return ExecutionObservation(
            order_reference=self.client_order_id,
            intent_id=intent_id,
            correlation_id=correlation_id,
            symbol=self.symbol,
            state=self.state,
            filled_quantity=self.executed_quantity,
            remaining_quantity=self.remaining_quantity,
            last_fill_quantity=None if last_fill is None else last_fill.quantity,
            last_fill_price=None if last_fill is None else last_fill.price,
            fee=None if last_fill is None else last_fill.commission,
            fee_asset=None if last_fill is None else last_fill.commission_asset,
            ts_ns=0 if self.transact_time is None else int(self.transact_time.timestamp() * 1e9),
            producer=producer,
        )


@dataclass(frozen=True)
class TestnetAccountState:
    """The minimal, non-secret account facts Phase 11 needs for least-privilege verification."""

    can_trade: bool
    can_withdraw: bool
    account_type: str
    permissions: tuple[str, ...] = ()
    maker_commission: int = 0
    taker_commission: int = 0
    free_balance: Decimal = ZERO
    locked_balance: Decimal = ZERO
    asset: str | None = None
    venue_label: str = "binance-spot-testnet"

    @property
    def withdrawals_available(self) -> bool:
        """Phase 11 requires this to be False; a True value fails verification closed."""
        return bool(self.can_withdraw)


@dataclass(frozen=True)
class TestnetEnvironment:
    """The verified runtime environment for the testnet adapter (no secrets)."""

    base_url: str
    host: str
    venue_label: str
    server_time_ms: int
    clock_offset_ms: int
    account: TestnetAccountState
    symbol: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_record(self) -> dict[str, object]:
        """A JSON-able, secret-free observability record."""
        return {
            "base_url": self.base_url,
            "host": self.host,
            "venue_label": self.venue_label,
            "server_time_ms": self.server_time_ms,
            "clock_offset_ms": self.clock_offset_ms,
            "symbol": self.symbol,
            "can_trade": self.account.can_trade,
            "can_withdraw": self.account.can_withdraw,
            "account_type": self.account.account_type,
            "permissions": list(self.account.permissions),
            "notes": list(self.notes),
        }


def format_decimal(value: Decimal) -> str:
    """Format a Decimal for the API without exponent notation and without rounding."""
    return format(value, "f")


@dataclass(frozen=True)
class TestnetSymbolRules:
    """Trading rules for one symbol, as reported by ``exchangeInfo``."""

    symbol: str
    base_asset: str
    quote_asset: str
    price_tick: Decimal
    quantity_step: Decimal
    min_quantity: Decimal = ZERO
    min_notional: Decimal = ZERO
    status: str = "TRADING"

    def quantity_is_valid(self, quantity: Decimal) -> bool:
        """True when the quantity satisfies step size and minimum (no rounding performed)."""
        if quantity < self.min_quantity:
            return False
        if self.quantity_step > 0 and quantity % self.quantity_step != 0:
            return False
        return True

    def price_is_valid(self, price: Decimal) -> bool:
        if self.price_tick > 0 and price % self.price_tick != 0:
            return False
        return price > 0

    def notional_is_valid(self, quantity: Decimal, price: Decimal) -> bool:
        return quantity * price >= self.min_notional


def utc_from_ms(value: int | None) -> datetime | None:
    """Convert a millisecond epoch to an aware UTC datetime."""
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
