"""Simulated position/P&L accounting for paper sessions (Phase 10).

SIMULATED ACCOUNTING ONLY: this is a deterministic average-cost ledger over simulated fills. It
is NOT a performance claim and NOT a safety certification.

All monetary and quantity values are :class:`decimal.Decimal`; nothing is rounded silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .errors import PaperStateError

__all__ = ["PositionLedger", "PositionState"]

ZERO = Decimal("0")


@dataclass(frozen=True)
class PositionState:
    symbol: str
    quantity: Decimal
    average_cost: Decimal
    realized_pnl_quote: Decimal
    fees_quote: Decimal

    @property
    def is_flat(self) -> bool:
        return self.quantity == ZERO


@dataclass
class PositionLedger:
    """Deterministic average-cost ledger over simulated fills."""

    starting_equity_quote: Decimal
    _positions: dict[str, PositionState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.starting_equity_quote, Decimal) or self.starting_equity_quote <= 0:
            raise PaperStateError("starting equity must be a positive Decimal.")

    def position(self, symbol: str) -> PositionState:
        return self._positions.get(
            symbol,
            PositionState(
                symbol=symbol,
                quantity=ZERO,
                average_cost=ZERO,
                realized_pnl_quote=ZERO,
                fees_quote=ZERO,
            ),
        )

    def apply_fill(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal = ZERO,
    ) -> PositionState:
        """Apply one simulated fill (``side`` is the order's side: buy/sell)."""
        if side not in {"buy", "sell"}:
            raise PaperStateError(f"unknown fill side {side!r}.")
        if not isinstance(quantity, Decimal) or quantity <= 0:
            raise PaperStateError("fill quantity must be a positive Decimal.")
        if not isinstance(price, Decimal) or price <= 0:
            raise PaperStateError("fill price must be a positive Decimal.")
        if not isinstance(fee, Decimal) or fee < 0:
            raise PaperStateError("fee must be a non-negative Decimal.")

        current = self.position(symbol)
        signed = quantity if side == "buy" else -quantity
        quantity_after = current.quantity + signed
        realized = current.realized_pnl_quote
        average_cost = current.average_cost

        if current.quantity == ZERO or (current.quantity > ZERO) == (signed > ZERO):
            # Opening or increasing: blend the average cost.
            opened = abs(current.quantity) + abs(signed)
            average_cost = (
                (current.average_cost * abs(current.quantity) + price * quantity) / opened
            )
        else:
            # Reducing or flipping: realize P&L on the closed quantity.
            closed = min(abs(signed), abs(current.quantity))
            direction = Decimal("1") if current.quantity > ZERO else Decimal("-1")
            realized += (price - current.average_cost) * closed * direction
            if abs(signed) > abs(current.quantity):
                average_cost = price
            elif quantity_after == ZERO:
                average_cost = ZERO

        if quantity_after == ZERO:
            average_cost = ZERO

        state = PositionState(
            symbol=symbol,
            quantity=quantity_after,
            average_cost=average_cost,
            realized_pnl_quote=realized - fee,
            fees_quote=current.fees_quote + fee,
        )
        self._positions[symbol] = state
        return state

    def positions(self) -> dict[str, PositionState]:
        return dict(self._positions)

    def gross_exposure_quote(self, marks: dict[str, Decimal]) -> Decimal:
        total = ZERO
        for symbol, state in self._positions.items():
            mark = marks.get(symbol)
            if mark is None or state.quantity == ZERO:
                continue
            total += abs(state.quantity) * mark
        return total

    def unrealized_pnl_quote(self, marks: dict[str, Decimal]) -> Decimal:
        total = ZERO
        for symbol, state in self._positions.items():
            mark = marks.get(symbol)
            if mark is None or state.quantity == ZERO:
                continue
            total += (mark - state.average_cost) * state.quantity
        return total

    def realized_pnl_quote(self) -> Decimal:
        return sum((state.realized_pnl_quote for state in self._positions.values()), ZERO)

    def fees_quote(self) -> Decimal:
        return sum((state.fees_quote for state in self._positions.values()), ZERO)

    def equity_quote(self, marks: dict[str, Decimal]) -> Decimal:
        return (
            self.starting_equity_quote
            + self.realized_pnl_quote()
            + self.unrealized_pnl_quote(marks)
        )
