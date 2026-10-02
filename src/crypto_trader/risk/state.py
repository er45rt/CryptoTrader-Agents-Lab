"""Deterministic risk state inputs and pending-intent protection (Phase 7).

Every monetary value here is a quote-currency :class:`decimal.Decimal`; positions are signed
base quantities (positive long, negative short). Percentages are expressed as percent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from .errors import RiskStateError

__all__ = ["PendingIntent", "PendingIntentRegistry", "RiskStateSnapshot", "Side"]


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class RiskStateSnapshot:
    """The typed, deterministic inputs the risk engine evaluates against."""

    equity_quote: Decimal
    peak_equity_quote: Decimal
    realized_pnl_quote: Decimal
    gross_exposure_quote: Decimal
    net_exposure_quote: Decimal
    open_orders: int
    open_positions: int
    symbol_exposure_quote: Mapping[str, Decimal] = field(default_factory=dict)
    strategy_exposure_quote: Mapping[str, Decimal] = field(default_factory=dict)
    position_quantity: Mapping[str, Decimal] = field(default_factory=dict)
    strategy_position_quantity: Mapping[str, Decimal] = field(default_factory=dict)
    strategy_open_sell_quantity: Mapping[str, Decimal] = field(default_factory=dict)
    snapshot_ts: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("equity_quote", "peak_equity_quote", "gross_exposure_quote"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or value < 0:
                raise RiskStateError(f"{name} must be a non-negative Decimal.")
        if not isinstance(self.realized_pnl_quote, Decimal):
            raise RiskStateError("realized_pnl_quote must be a Decimal.")
        if not isinstance(self.net_exposure_quote, Decimal):
            raise RiskStateError("net_exposure_quote must be a Decimal (signed).")
        for name in ("open_orders", "open_positions"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise RiskStateError(f"{name} must be a non-negative integer.")
        if self.snapshot_ts is not None and (
            self.snapshot_ts.tzinfo is None or self.snapshot_ts.utcoffset() is None
        ):
            raise RiskStateError("snapshot_ts must be timezone-aware when present.")

    def symbol_exposure(self, symbol: str) -> Decimal:
        return Decimal(self.symbol_exposure_quote.get(symbol, Decimal("0")))

    def strategy_exposure(self, strategy_id: str) -> Decimal:
        return Decimal(self.strategy_exposure_quote.get(strategy_id, Decimal("0")))

    def position(self, symbol: str) -> Decimal:
        return Decimal(self.position_quantity.get(symbol, Decimal("0")))

    def strategy_position(self, strategy_id: str) -> Decimal:
        return Decimal(self.strategy_position_quantity.get(strategy_id, Decimal("0")))

    def strategy_open_sell(self, strategy_id: str) -> Decimal:
        return Decimal(self.strategy_open_sell_quantity.get(strategy_id, Decimal("0")))


@dataclass(frozen=True)
class PendingIntent:
    intent_id: UUID
    symbol: str
    strategy_id: str
    side: Side
    quantity: Decimal | None = None
    notional: Decimal | None = None

    def __post_init__(self) -> None:
        if self.quantity is None and self.notional is None:
            return
        if self.quantity is None or self.notional is None:
            raise RiskStateError("pending reservation economics must be complete when supplied.")
        if not isinstance(self.quantity, Decimal) or not self.quantity.is_finite() or self.quantity <= 0:
            raise RiskStateError("pending quantity must be a positive finite Decimal.")
        if not isinstance(self.notional, Decimal) or not self.notional.is_finite() or self.notional <= 0:
            raise RiskStateError("pending notional must be a positive finite Decimal.")

    @property
    def signed_notional(self) -> Decimal:
        if self.notional is None:
            raise RiskStateError("pending reservation notional is unavailable.")
        return self.notional if self.side is Side.BUY else -self.notional


class PendingIntentRegistry:
    """Deterministic duplicate/conflicting-intent protection (in-memory, bounded by caller)."""

    def __init__(self) -> None:
        self._items: dict[UUID, PendingIntent] = {}

    def has_intent(self, intent_id: UUID) -> bool:
        return intent_id in self._items

    def conflicting(self, *, symbol: str, strategy_id: str, side: Side) -> PendingIntent | None:
        for item in self._items.values():
            if item.symbol == symbol and item.strategy_id == strategy_id and item.side is not side:
                return item
        return None

    def register(self, item: PendingIntent) -> None:
        self._items[item.intent_id] = item

    def remove(self, intent_id: UUID) -> bool:
        return self._items.pop(intent_id, None) is not None

    def clear(self) -> None:
        self._items.clear()

    def items(self) -> tuple[PendingIntent, ...]:
        return tuple(self._items.values())

    def reserved_notional(
        self, *, symbol: str | None = None, strategy_id: str | None = None
    ) -> Decimal:
        total = Decimal("0")
        for item in self._items.values():
            if (symbol is not None and item.symbol != symbol) or (
                strategy_id is not None and item.strategy_id != strategy_id
            ):
                continue
            if item.notional is None:
                raise RiskStateError("pending reservation economics are unavailable.")
            total += item.notional
        return total

    def reserved_gross_notional(self) -> Decimal:
        return self.reserved_notional()

    def reserved_signed_notional(
        self, *, symbol: str | None = None, strategy_id: str | None = None
    ) -> Decimal:
        return sum(
            (
                item.signed_notional
                for item in self._items.values()
                if (symbol is None or item.symbol == symbol)
                and (strategy_id is None or item.strategy_id == strategy_id)
            ),
            Decimal("0"),
        )

    def reserved_sell_quantity(self, *, symbol: str, strategy_id: str) -> Decimal:
        total = Decimal("0")
        for item in self._items.values():
            if (
                item.symbol != symbol
                or item.strategy_id != strategy_id
                or item.side is not Side.SELL
            ):
                continue
            if item.quantity is None:
                raise RiskStateError("pending reservation economics are unavailable.")
            total += item.quantity
        return total

    def __len__(self) -> int:
        return len(self._items)
