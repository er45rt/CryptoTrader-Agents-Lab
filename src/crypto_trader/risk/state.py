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
    snapshot_ts: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("equity_quote", "peak_equity_quote", "gross_exposure_quote"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise RiskStateError(f"{name} must be a finite non-negative Decimal.")
        if (
            not isinstance(self.realized_pnl_quote, Decimal)
            or not self.realized_pnl_quote.is_finite()
        ):
            raise RiskStateError("realized_pnl_quote must be a finite Decimal.")
        if (
            not isinstance(self.net_exposure_quote, Decimal)
            or not self.net_exposure_quote.is_finite()
        ):
            raise RiskStateError("net_exposure_quote must be a finite Decimal (signed).")
        for name in ("open_orders", "open_positions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RiskStateError(f"{name} must be a non-negative integer.")

        for name in (
            "symbol_exposure_quote",
            "strategy_exposure_quote",
            "position_quantity",
        ):
            values = getattr(self, name)
            if not isinstance(values, Mapping):
                raise RiskStateError(f"{name} must be a mapping.")
            for key, value in values.items():
                if not isinstance(key, str) or not key:
                    raise RiskStateError(f"{name} keys must be non-empty strings.")
                if not isinstance(value, Decimal) or not value.is_finite():
                    raise RiskStateError(f"{name} values must be finite Decimals.")
        if self.peak_equity_quote < self.equity_quote:
            raise RiskStateError(
                "peak_equity_quote must be greater than or equal to equity_quote."
            )
        if self.gross_exposure_quote < abs(self.net_exposure_quote):
            raise RiskStateError(
                "gross_exposure_quote must be greater than or equal to abs(net_exposure_quote)."
            )
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


@dataclass(frozen=True)
class PendingIntent:
    intent_id: UUID
    symbol: str
    strategy_id: str
    side: Side


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

    def __len__(self) -> int:
        return len(self._items)
