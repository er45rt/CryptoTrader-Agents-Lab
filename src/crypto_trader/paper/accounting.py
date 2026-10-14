"""Simulated position/P&L accounting for paper sessions (Phase 10).

SIMULATED ACCOUNTING ONLY: this is a deterministic average-cost ledger over simulated fills. It
is NOT a performance claim and NOT a safety certification.

All monetary and quantity values are :class:`decimal.Decimal`; nothing is rounded silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from uuid import UUID
from typing import Any

from .errors import PaperStateError

__all__ = ["PositionLedger", "PositionState"]

ZERO = Decimal("0")


@dataclass(frozen=True)
class CurrentCashReading:
    """Transient cash observation from the active engine, never a restart token."""

    boundary_ns: int
    runtime_id: UUID
    sequence: int
    native: Any
    reflected_intents: frozenset[UUID]


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

    Ledger locks represent REMAINING commitments, not native cash locks. Fee headroom is
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
        self._original_cash_locks: dict[UUID, tuple[str, Decimal, Decimal]] = {}

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
        self._original_cash_locks[intent_id] = (side, quantity, price)

    def release(self, intent_id):
        self._cash_locks.pop(intent_id, None)
        self._original_cash_locks.pop(intent_id, None)

    def validate_native_state(self, native, reflected_intents):
        """Pin native 1.231.0 ORIGINAL-quantity locks; retain remaining commitments."""
        if type(reflected_intents) is not frozenset or not reflected_intents <= self._cash_locks.keys():
            raise PaperStateError("native cash reflection identities are unknown.")
        fields = ("initial_quote", "base_total", "base_free", "base_locked", "quote_total",
                  "quote_free", "quote_locked", "fees_quote", "position_quantity")
        if any(not isinstance(getattr(native, name), Decimal) or not getattr(native, name).is_finite()
               or getattr(native, name) < 0 for name in fields):
            raise PaperStateError("native cash balance is nonfinite or negative.")
        if native.account_type != "CASH" or native.allow_borrowing is not False:
            raise PaperStateError("native cash account identity is unsupported.")
        if type(native.unsettled) is not bool or native.unsettled != bool(self._cash_locks):
            raise PaperStateError("native unresolved-order evidence conflicts with current commitments.")
        if (native.initial_quote != self.starting_equity_quote or native.quote_total != self.quote_total
                or native.base_total != self.base_total or native.position_quantity != self.base_total
                or native.fees_quote != self.fees_quote()
                or native.quote_free + native.quote_locked != native.quote_total
                or native.base_free + native.base_locked != native.base_total):
            raise PaperStateError("current native cash and processed journal accounting disagree.")
        quote_lock = sum((q * p for identity, (side, q, p) in self._original_cash_locks.items()
                          if identity in reflected_intents and side == "buy"), ZERO)
        base_lock = sum((q for identity, (side, q, _) in self._original_cash_locks.items()
                         if identity in reflected_intents and side == "sell"), ZERO)
        if native.quote_locked != min(quote_lock, self.quote_total) or native.base_locked != min(base_lock, self.base_total):
            raise PaperStateError("Nautilus 1.231.0 original-quantity cash lock behavior changed or is uncertain.")

    def admission_evidence(self, side, quantity, price, *, native_quote_free,
                           native_base_free, reflected_intents):
        if side not in {"buy", "sell"} or any(not isinstance(v, Decimal) or not v.is_finite() or v < 0
            for v in (quantity, price, native_quote_free, native_base_free)) or quantity == 0 or price == 0:
            raise PaperStateError("cash admission evidence is invalid.")
        if type(reflected_intents) is not frozenset or not reflected_intents <= self._cash_locks.keys():
            raise PaperStateError("cash admission has unknown native reservation identities.")
        unreflected = sum((q * p for identity, (direction, q, p) in self._cash_locks.items()
                           if direction == "buy" and identity not in reflected_intents), ZERO)
        base_unreflected = sum((q for identity, (direction, q, _) in self._cash_locks.items()
                                if direction == "sell" and identity not in reflected_intents), ZERO)
        admissible = min(self.quote_free, native_quote_free)
        # Align the native view with obligations it has not reflected yet, THEN
        # take the minimum. Do not subtract all commitments from that minimum.
        spendable = min(self.quote_free, native_quote_free - unreflected)
        fee_headroom = self.quote_locked * self.fee_rate
        required = quantity * price * (1 + self.fee_rate)
        base_available = min(self.base_free, native_base_free - base_unreflected)
        allowed = required + fee_headroom <= spendable if side == "buy" else quantity <= base_available
        return {"ledger_free_quote": self.quote_free, "native_free_quote": native_quote_free,
            "admissible_free_quote": admissible, "spendable_free_quote": spendable,
            "remaining_order_commitment_quote": self.quote_locked,
            "unreflected_principal_quote": unreflected, "remaining_fee_headroom_quote": fee_headroom,
            "required_quote_including_fee": required,
            "ledger_free_base": self.base_free, "native_free_base": native_base_free,
            "unreflected_base_quantity": base_unreflected, "spendable_free_base": base_available,
            "selection_reason": "native_free_lower" if native_quote_free < self.quote_free else
                                "ledger_free_lower" if self.quote_free < native_quote_free else "sources_equal",
            "allowed": allowed, "rejection_reason": None if allowed else
                "insufficient_admissible_quote_including_fees_and_pending" if side == "buy" else
                "insufficient_admissible_base_inventory"}

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
