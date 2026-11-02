"""Stop-loss / take-profit state machine for long Paper positions (pure, engine-independent).

Rules, in force for every protected entry:

* Exposure is armed only by a real fill, and only for trades strictly after that fill.
* One tape timestamp is one group of trades. If a group reaches both a stop and a target, the
  stop wins: the pessimistic reading of an unordered group.
* A stop is sticky. Once triggered it keeps exiting until the entry is flat, even if the price
  recovers, and every target of that entry is dead.
* A stop exits with a MARKET order, so a gap fills at the book and never at the stop price.
* A target exits with an immediate LIMIT at its level, so a gap up can only improve the fill and
  an unfilled part simply ends; the level stays open for the next print that reaches it.
* Exits are always immediate and Host entries too, so a Host session has nothing resting, needs
  no cancel, and a triggered stop never waits for a resting order to clear.
* Exits only ever size from filled, unexited quantity capped by the position, so they cannot open
  a short and cannot sell the same quantity twice.

The session replays this machine from the tape and the durable decisions: restart recovery is the
same code path as normal running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from uuid import UUID

from .decisions import StrategyDecision
from .errors import PaperStateError

__all__ = [
    "ImmediateDecision",
    "ProtectedDecision",
    "ProtectionBook",
    "ProtectionPlan",
    "ProtectiveTrigger",
    "TakeProfitLevel",
]

ZERO = Decimal("0")
STOP_LOSS = "stop_loss"
TAKE_PROFIT = "take_profit"
BLOCKED_RETRY_NS = 60 * 1_000_000_000
EXIT_RETRY_NS = 1_000_000_000


@dataclass(frozen=True)
class TakeProfitLevel:
    price: Decimal
    allocation_pct: Decimal

    def __post_init__(self) -> None:
        for value in (self.price, self.allocation_pct):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
                raise PaperStateError("take-profit values must be finite positive Decimals.")
        if self.allocation_pct > 100:
            raise PaperStateError("take-profit allocation cannot exceed 100%.")


@dataclass(frozen=True)
class ProtectionPlan:
    stop_price: Decimal
    take_profits: tuple[TakeProfitLevel, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.stop_price, Decimal) or not self.stop_price.is_finite()
                or self.stop_price <= ZERO):
            raise PaperStateError("a stop price must be a finite positive Decimal.")
        prices = [level.price for level in self.take_profits]
        if prices != sorted(set(prices)):
            raise PaperStateError("take-profit levels must be strictly ascending.")
        if prices and prices[0] <= self.stop_price:
            raise PaperStateError("take-profit levels must sit above the stop.")


@dataclass(frozen=True)
class ImmediateDecision(StrategyDecision):
    """An order that fills now or ends now. It never rests on the simulated book."""

    immediate: bool = True


@dataclass(frozen=True)
class ProtectedDecision(ImmediateDecision):
    """A buy with protective levels and an expiry. R-02 refuses this type on purpose."""

    protection: ProtectionPlan | None = None
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.action != "buy" or not isinstance(self.protection, ProtectionPlan):
            raise PaperStateError("a protected decision must be a buy with a protection plan.")
        if self.valid_until is not None and (
                self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None):
            raise PaperStateError("valid_until must be timezone-aware.")
        reference = self.limit_price
        if reference is not None and self.protection.stop_price >= reference:
            raise PaperStateError("a long stop must sit below the entry.")


@dataclass(frozen=True)
class ProtectiveTrigger:
    entry_intent: UUID
    leg: str
    sequence: int
    level: int
    quantity: Decimal
    limit_price: Decimal | None
    trigger_price: Decimal


@dataclass
class _Level:
    price: Decimal
    allocation_pct: Decimal
    done: Decimal = ZERO
    closed: bool = False


@dataclass
class _Protection:
    entry_intent: UUID
    plan: ProtectionPlan
    levels: list[_Level]
    entry_working: bool = True
    armed_ns: int | None = None
    filled: Decimal = ZERO
    open: Decimal = ZERO
    stopping: bool = False
    sequence: int = 0
    retry_ns: int = 0
    blocked: int = 0


@dataclass
class ProtectionBook:
    quantity_step: Decimal
    _items: dict[UUID, _Protection] = field(default_factory=dict)
    _exits: dict[UUID, tuple[UUID, str, int]] = field(default_factory=dict)
    log: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if (not isinstance(self.quantity_step, Decimal) or not self.quantity_step.is_finite()
                or self.quantity_step <= ZERO):
            raise PaperStateError("protection requires a positive quantity step.")

    # --- facts arriving from the session ---

    def register_entry(self, intent_id: UUID, plan: ProtectionPlan) -> None:
        if intent_id in self._items:
            raise PaperStateError("an entry can be protected only once.")
        self._items[intent_id] = _Protection(
            entry_intent=intent_id, plan=plan,
            levels=[_Level(level.price, level.allocation_pct) for level in plan.take_profits],
        )

    def on_fill(self, intent_id: UUID, side: str, quantity: Decimal, ts_ns: int) -> None:
        exit_owner = self._exits.get(intent_id)
        if exit_owner is not None:
            item = self._items[exit_owner[0]]
            if quantity > item.open:
                raise PaperStateError("a protective exit filled more than its protected quantity.")
            item.open -= quantity
            if exit_owner[1] == TAKE_PROFIT:
                item.levels[exit_owner[2]].done += quantity
            self.log.append({"event": "exit_fill", "leg": exit_owner[1], "quantity": str(quantity),
                             "ts_ns": str(ts_ns), "entry": str(item.entry_intent)})
            return
        item = self._items.get(intent_id)
        if item is not None:
            if item.stopping:
                self.log.append({"event": "fill_after_stop", "quantity": str(quantity),
                                 "ts_ns": str(ts_ns), "entry": str(intent_id)})
            item.filled += quantity
            item.open += quantity
            if item.armed_ns is None:
                item.armed_ns = ts_ns  # Earlier exposure stays armed through later partial fills.
            return
        if side == "sell":
            # A strategy sell reduces the oldest protected exposure first.
            remaining = quantity
            for candidate in self._items.values():
                taken = min(candidate.open, remaining)
                candidate.open -= taken
                remaining -= taken

    def on_terminal(self, intent_id: UUID, ts_ns: int) -> None:
        exit_owner = self._exits.pop(intent_id, None)
        if exit_owner is not None:
            if exit_owner[1] == TAKE_PROFIT:
                self._items[exit_owner[0]].retry_ns = ts_ns + EXIT_RETRY_NS
            return
        item = self._items.get(intent_id)
        if item is not None:
            item.entry_working = False

    def exit_placed(self, trigger: ProtectiveTrigger, intent_id: UUID) -> None:
        if intent_id in self._exits:
            raise PaperStateError("duplicate protective exit identity.")
        item = self._items[trigger.entry_intent]
        item.sequence += 1
        self._exits[intent_id] = (trigger.entry_intent, trigger.leg, trigger.level)
        self.log.append({"event": "exit_placed", "leg": trigger.leg, "quantity": str(trigger.quantity),
                         "trigger_price": str(trigger.trigger_price), "entry": str(trigger.entry_intent)})

    def exit_blocked(self, trigger: ProtectiveTrigger, ts_ns: int, reasons: tuple[str, ...]) -> None:
        """Hard Risk refused the exit: it stays armed, audited, and retries later."""
        item = self._items[trigger.entry_intent]
        item.sequence += 1
        item.retry_ns = ts_ns + BLOCKED_RETRY_NS
        item.blocked += 1
        self.log.append({"event": "exit_blocked", "leg": trigger.leg, "ts_ns": str(ts_ns),
                         "entry": str(trigger.entry_intent), "reasons": ",".join(reasons)})

    # --- questions from the session ---

    @property
    def has_work(self) -> bool:
        return any(item.entry_working or item.open > ZERO for item in self._items.values())

    def needs_every_tick(self) -> bool:
        return bool(self._exits) or any(
            item.entry_working or (item.stopping and item.open > ZERO)
            for item in self._items.values()
        )

    def crosses(self, low: Decimal, high: Decimal) -> bool:
        for item in self._items.values():
            if item.open <= ZERO:
                # A working entry may have filled since the last capture: a print through its
                # stop must force one, or a stop that recovers within the throttle is lost.
                if item.entry_working and low <= item.plan.stop_price:
                    return True
                continue
            if low <= item.plan.stop_price:
                return True
            if any(not level.closed and high >= level.price for level in item.levels):
                return True
        return False

    def evaluate(self, ts_ns: int, low: Decimal, high: Decimal, *,
                 position: Decimal) -> ProtectiveTrigger | None:
        """Return the next exit to place, stop first; None while one is already working."""
        if position <= ZERO:
            return None
        for item in self._items.values():
            if item.open <= ZERO or item.armed_ns is None or item.armed_ns >= ts_ns:
                continue
            if item.stopping or low <= item.plan.stop_price:
                item.stopping = True
                if any(owner == (item.entry_intent, STOP_LOSS, -1)
                       for owner in self._exits.values()) or ts_ns < item.retry_ns:
                    return None
                return ProtectiveTrigger(
                    entry_intent=item.entry_intent, leg=STOP_LOSS, sequence=item.sequence, level=-1,
                    quantity=min(item.open, position), limit_price=None, trigger_price=low,
                )
        if self._exits:
            return None
        for item in self._items.values():
            if item.open <= ZERO or item.armed_ns is None or item.armed_ns >= ts_ns:
                continue
            if ts_ns < item.retry_ns or item.stopping:
                continue
            for index, level in enumerate(item.levels):
                if level.closed or high < level.price:
                    continue
                quantity = self._target_quantity(item, index)
                if quantity <= ZERO:
                    level.closed = True
                    continue
                return ProtectiveTrigger(
                    entry_intent=item.entry_intent, leg=TAKE_PROFIT, sequence=item.sequence,
                    level=index, quantity=min(quantity, position), limit_price=level.price,
                    trigger_price=high,
                )
        return None

    def _target_quantity(self, item: _Protection, index: int) -> Decimal:
        level = item.levels[index]
        if index == len(item.levels) - 1:
            return item.open
        share = item.filled * level.allocation_pct / Decimal("100")
        floored = (share / self.quantity_step).to_integral_value(rounding=ROUND_FLOOR) * self.quantity_step
        return min(floored - level.done, item.open)

    def snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "entry_intent": str(item.entry_intent), "stop_price": str(item.plan.stop_price),
                "filled": str(item.filled), "open": str(item.open), "stopping": item.stopping,
                "entry_working": item.entry_working, "blocked_attempts": item.blocked,
                "targets": [{"price": str(level.price), "allocation_pct": str(level.allocation_pct),
                             "done": str(level.done), "closed": level.closed}
                            for level in item.levels],
            }
            for item in self._items.values() if item.entry_working or item.open > ZERO
        ]
