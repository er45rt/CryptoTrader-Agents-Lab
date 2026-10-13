"""Simulated position/P&L accounting for paper sessions (Phase 10).

SIMULATED ACCOUNTING ONLY: this is a deterministic average-cost ledger over simulated fills. It
is NOT a performance claim and NOT a safety certification.

All monetary and quantity values are :class:`decimal.Decimal`; nothing is rounded silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from uuid import UUID

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
        if (not isinstance(self.starting_equity_quote, Decimal)
                or not self.starting_equity_quote.is_finite()
                or self.starting_equity_quote <= 0):
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
        if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
            raise PaperStateError("fill quantity must be a positive Decimal.")
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise PaperStateError("fill price must be a positive Decimal.")
        if not isinstance(fee, Decimal) or not fee.is_finite() or fee < 0:
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

    @staticmethod
    def _checked_mark(marks: dict[str, Decimal], symbol: str) -> Decimal:
        mark = marks.get(symbol)
        if not isinstance(mark, Decimal) or not mark.is_finite() or mark <= ZERO:
            raise PaperStateError("active position requires a finite positive market mark.")
        return mark

    def gross_exposure_quote(self, marks: dict[str, Decimal]) -> Decimal:
        total = ZERO
        for symbol, state in self._positions.items():
            if state.quantity == ZERO:
                continue
            total += abs(state.quantity) * self._checked_mark(marks, symbol)
        return total

    def unrealized_pnl_quote(self, marks: dict[str, Decimal]) -> Decimal:
        total = ZERO
        for symbol, state in self._positions.items():
            if state.quantity == ZERO:
                continue
            mark = self._checked_mark(marks, symbol)
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


class SpotCashLedger(PositionLedger):
    """R-02 cash inventory with separate cash locks and conservative fee admission.

    Locks are principal only (as in the native CASH account); fee headroom is
    an application admission reserve, never mislabelled as native locked cash.
    Legacy PositionLedger retains its historical short/flip semantics.
    """

    def __init__(self, starting_equity_quote, *, symbol, base_asset, quote_asset,
                 fee_rate=ZERO):
        super().__init__(starting_equity_quote)
        self.symbol, self.base_asset, self.quote_asset = symbol, base_asset, quote_asset
        if base_asset == quote_asset or not isinstance(fee_rate, Decimal) or not fee_rate.is_finite() or fee_rate < 0 or fee_rate >= 1:
            raise PaperStateError("invalid cash assets/fee reserve.")
        self.fee_rate = fee_rate
        self.quote_total = starting_equity_quote
        self._cash_locks: dict[UUID, tuple[str, Decimal, Decimal]] = {}

    @property
    def quote_locked(self):
        return sum((q * p for side, q, p in self._cash_locks.values() if side == "buy"), ZERO)

    @property
    def base_locked(self):
        return sum((q for side, q, _ in self._cash_locks.values() if side == "sell"), ZERO)

    @property
    def quote_free(self):
        return self.quote_total - self.quote_locked

    @property
    def base_total(self):
        return self.position(self.symbol).quantity

    @property
    def base_free(self):
        return self.base_total - self.base_locked

    def check_admission(self, side, quantity, price):
        if side not in {"buy", "sell"} or any(not isinstance(x, Decimal) or not x.is_finite() or x <= 0 for x in (quantity, price)):
            raise PaperStateError("invalid cash admission.")
        fee_headroom = self.quote_locked * self.fee_rate
        if side == "buy" and quantity * price * (1 + self.fee_rate) > self.quote_free - fee_headroom:
            raise PaperStateError("insufficient quote cash balance including fee reserve.")
        if side == "sell" and quantity > self.base_free:
            raise PaperStateError("insufficient base inventory for sell.")

    def reserve(self, intent_id, side, quantity, price):
        if intent_id in self._cash_locks:
            raise PaperStateError("duplicate cash reservation.")
        self.check_admission(side, quantity, price)
        self._cash_locks[intent_id] = (side, quantity, price)

    def release(self, intent_id):
        self._cash_locks.pop(intent_id, None)

    def apply_fill(self, *, symbol, side, quantity, price, fee=ZERO,
                   fee_asset=None, intent_id=None):
        with localcontext() as context:
            context.prec, context.rounding = 34, ROUND_HALF_EVEN
            return self._apply_cash_fill(symbol=symbol, side=side, quantity=quantity,
                price=price, fee=fee, fee_asset=fee_asset, intent_id=intent_id)

    def _apply_cash_fill(self, *, symbol, side, quantity, price, fee, fee_asset, intent_id):
        # Validate everything before altering a cash lock or the position.
        if symbol != self.symbol or side not in {"buy", "sell"}:
            raise PaperStateError("cash fill scope/side mismatch.")
        if fee_asset != self.quote_asset:
            raise PaperStateError("unsupported fee asset: Quote fees only.")
        if any(not isinstance(x, Decimal) or not x.is_finite() or x <= 0 for x in (quantity, price)) or not isinstance(fee, Decimal) or not fee.is_finite() or fee < 0:
            raise PaperStateError("invalid cash fill quantity, price or fee.")
        if side == "sell" and quantity > self.base_total:
            raise PaperStateError("sell exceeds base inventory.")
        debit = quantity * price + fee
        if side == "buy" and debit > self.quote_total:
            raise PaperStateError("fill exceeds quote cash balance.")
        quote_after = self.quote_total + (-quantity * price if side == "buy" else quantity * price) - fee
        if quote_after < ZERO:
            raise PaperStateError("fill fee exceeds quote cash balance.")
        lock = self._cash_locks.get(intent_id)
        if (intent_id is not None or self._cash_locks) and (lock is None or lock[0] != side or quantity > lock[1]):
            raise PaperStateError("cash fill has no matching reservation.")
        state = super().apply_fill(symbol=symbol, side=side, quantity=quantity, price=price, fee=fee)
        self.quote_total = quote_after
        if lock is not None:
            remaining = lock[1] - quantity
            if remaining:
                self._cash_locks[intent_id] = (side, remaining, lock[2])
            else:
                self.release(intent_id)
        return state

    def equity_quote(self, marks):
        return self.quote_total + self.base_total * self._checked_mark(marks, self.symbol)

    def economic_state(self, mark):
        with localcontext() as context:
            context.prec, context.rounding = 34, ROUND_HALF_EVEN
            return self._economic_state(mark)

    def _economic_state(self, mark):
        marks = {self.symbol: mark}
        position = self.position(self.symbol)
        return {"base_total": self.base_total, "base_free": self.base_free,
                "base_locked": self.base_locked, "quote_total": self.quote_total,
                "quote_free": self.quote_free, "quote_locked": self.quote_locked,
                "average_cost": position.average_cost, "fees_quote": self.fees_quote(),
                "realized_pnl_quote": self.realized_pnl_quote(),
                "unrealized_pnl_quote": self.unrealized_pnl_quote(marks),
                "equity_quote": self.equity_quote(marks),
                "gross_exposure_quote": self.gross_exposure_quote(marks)}
