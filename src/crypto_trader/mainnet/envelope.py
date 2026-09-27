"""The locked pilot envelope, execution price rules and the FOK canary order policy (Phase 15).

These are OWNER-APPROVED FUTURE CANARY LIMITS, not trading permission. They are represented,
validated and reported; they are never activated by this phase, and no order may be submitted.

Price rules are deliberately distinct concepts:

* ``max_spread_bps = 30`` - a top-of-book market-quality / admission sanity check.
* ``max_normal_slippage_bps = 15`` - the maximum allowed normal execution deviation. This is NOT an
  expected slippage and NOT a target slippage.
* ``absolute_price_bound_bps = 50`` - the venue-enforced catastrophic execution ceiling for a
  future canary.

Spread and execution deviation overlap but are not identical: spread observes top-of-book, while the
execution bound must also constrain depth consumed through the order book. During DRY-RUN only the
hypothetical values are computed and recorded; no order may be submitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from crypto_trader.execution import ExecutionSide

from .errors import CanaryPolicyError, LockedEnvelopeError, OldSnapshotReuseError

__all__ = [
    "CANARY_ENTRY_ORDER_TYPE",
    "CANARY_ENTRY_TIME_IN_FORCE",
    "EXECUTION_PRICE_RULES",
    "FIRST_CANARY_TRADES_REVIEW_CHECKPOINT",
    "INSUFFICIENT_EXECUTION_SAMPLE",
    "LOCKED_PILOT_ENVELOPE",
    "MIN_FOK_FILLS_FOR_OWNER_REVIEW",
    "CanaryEntryOrder",
    "EnvelopeActivation",
    "ExecutionPriceRules",
    "FillSlippageRecord",
    "LockedPilotEnvelope",
    "NoChasingPolicy",
    "assert_envelope_is_reference_only",
    "build_canary_entry",
    "classify_execution_sample",
    "envelope_record",
    "fill_slippage_record",
    "require_admissible_spread",
    "require_within_absolute_bound",
    "spread_bps",
]

HUNDRED = Decimal("100")
TEN_THOUSAND = Decimal("10000")

#: The ONLY entry order policy for the future canary: price-bounded LIMIT + FOK.
CANARY_ENTRY_ORDER_TYPE = "LIMIT"
CANARY_ENTRY_TIME_IN_FORCE = "FOK"

#: The first N canary trades are a mandatory review checkpoint only - nothing changes automatically.
FIRST_CANARY_TRADES_REVIEW_CHECKPOINT = 10

#: A future canary review window needs at least this many completed FOK fills to be sampled.
MIN_FOK_FILLS_FOR_OWNER_REVIEW = 5
INSUFFICIENT_EXECUTION_SAMPLE = "INSUFFICIENT_EXECUTION_SAMPLE"


class EnvelopeActivation(str, Enum):
    """The envelope is a reference for a future phase; it is never an activation."""

    REFERENCE_ONLY = "reference_only"


@dataclass(frozen=True)
class LockedPilotEnvelope:
    """The owner-approved future live canary envelope (SAR-denominated)."""

    capital_envelope_sar: Decimal
    max_order_value_sar: Decimal
    max_open_exposure_sar: Decimal
    daily_loss_halt_sar: Decimal
    max_open_orders: int
    symbols: tuple[str, ...]
    venue_scope: str = "SPOT"
    margin_allowed: bool = False
    futures_allowed: bool = False
    leverage_allowed: bool = False
    borrowing_allowed: bool = False
    withdrawals_allowed: bool = False
    activation: EnvelopeActivation = EnvelopeActivation.REFERENCE_ONLY

    def __post_init__(self) -> None:
        for name in (
            "capital_envelope_sar",
            "max_order_value_sar",
            "max_open_exposure_sar",
            "daily_loss_halt_sar",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise LockedEnvelopeError(f"{name} must be a finite positive Decimal.")
        if (
            isinstance(self.max_open_orders, bool)
            or not isinstance(self.max_open_orders, int)
            or self.max_open_orders <= 0
        ):
            raise LockedEnvelopeError("max_open_orders must be a positive integer.")
        if not self.symbols or not all(isinstance(item, str) and item for item in self.symbols):
            raise LockedEnvelopeError("the envelope requires at least one symbol.")
        if self.max_order_value_sar > self.capital_envelope_sar:
            raise LockedEnvelopeError("max_order_value_sar cannot exceed the capital envelope.")
        if self.max_open_exposure_sar > self.capital_envelope_sar:
            raise LockedEnvelopeError("max_open_exposure_sar cannot exceed the capital envelope.")
        if self.daily_loss_halt_sar > self.capital_envelope_sar:
            raise LockedEnvelopeError("daily_loss_halt_sar cannot exceed the capital envelope.")
        if not isinstance(self.activation, EnvelopeActivation):
            raise LockedEnvelopeError("activation must remain an EnvelopeActivation.")
        if self.venue_scope != "SPOT":
            raise LockedEnvelopeError("the locked envelope is SPOT only.")
        if any(
            (
                self.margin_allowed,
                self.futures_allowed,
                self.leverage_allowed,
                self.borrowing_allowed,
                self.withdrawals_allowed,
            )
        ):
            raise LockedEnvelopeError(
                "the locked envelope forbids margin, futures, leverage, borrowing and withdrawals."
            )

    def to_record(self) -> dict[str, object]:
        """A JSON-able record. These values are future limits, NOT permission to trade."""
        return {
            "capital_envelope_sar": str(self.capital_envelope_sar),
            "max_order_value_sar": str(self.max_order_value_sar),
            "max_open_exposure_sar": str(self.max_open_exposure_sar),
            "daily_loss_halt_sar": str(self.daily_loss_halt_sar),
            "max_open_orders": self.max_open_orders,
            "symbols": list(self.symbols),
            "venue_scope": self.venue_scope,
            "margin_allowed": self.margin_allowed,
            "futures_allowed": self.futures_allowed,
            "leverage_allowed": self.leverage_allowed,
            "borrowing_allowed": self.borrowing_allowed,
            "withdrawals_allowed": self.withdrawals_allowed,
            "activation": self.activation.value,
            "permission_to_trade": False,
        }


#: The owner-approved future live canary envelope (SAR amounts, SPOT only).
LOCKED_PILOT_ENVELOPE = LockedPilotEnvelope(
    capital_envelope_sar=Decimal("300"),
    max_order_value_sar=Decimal("50"),
    max_open_exposure_sar=Decimal("150"),
    daily_loss_halt_sar=Decimal("15"),
    max_open_orders=2,
    symbols=("BTCUSDT", "ETHUSDT"),
)


def envelope_record() -> dict[str, object]:
    """The envelope plus the explicit "not permission to trade" marker."""
    record = LOCKED_PILOT_ENVELOPE.to_record()
    record["note"] = (
        "future canary limits only; no FX conversion is implemented and no order may be placed "
        "in this phase"
    )
    return record


def assert_envelope_is_reference_only() -> None:
    """Fail closed if anything tries to treat the locked envelope as trading permission."""
    if LOCKED_PILOT_ENVELOPE.activation is not EnvelopeActivation.REFERENCE_ONLY:
        raise LockedEnvelopeError(
            "the locked pilot envelope is reference-only in this phase; it confers no permission "
            "to trade."
        )


@dataclass(frozen=True)
class ExecutionPriceRules:
    """The admission/quality rules and the future catastrophic execution ceiling."""

    max_spread_bps: Decimal = Decimal("30")
    max_normal_slippage_bps: Decimal = Decimal("15")
    absolute_price_bound_bps: Decimal = Decimal("50")

    def __post_init__(self) -> None:
        for name in (
            "max_spread_bps",
            "max_normal_slippage_bps",
            "absolute_price_bound_bps",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise LockedEnvelopeError(f"{name} must be a finite positive Decimal.")
        if self.max_normal_slippage_bps > self.absolute_price_bound_bps:
            raise LockedEnvelopeError(
                "the normal slippage allowance cannot exceed the absolute price bound."
            )

    def to_record(self) -> dict[str, object]:
        return {
            "max_spread_bps": str(self.max_spread_bps),
            "max_normal_slippage_bps": str(self.max_normal_slippage_bps),
            "absolute_price_bound_bps": str(self.absolute_price_bound_bps),
            "notes": [
                "spread is a top-of-book admission sanity check",
                "slippage is a maximum allowed normal execution deviation, not an expectation",
                "the absolute bound is the future venue-enforced catastrophic ceiling",
            ],
        }


EXECUTION_PRICE_RULES = ExecutionPriceRules()


def spread_bps(best_bid: Decimal, best_ask: Decimal) -> Decimal:
    """Top-of-book spread in basis points."""
    for name, value in (("best_bid", best_bid), ("best_ask", best_ask)):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise CanaryPolicyError(f"{name} must be a finite positive Decimal.")
    if best_ask < best_bid:
        raise CanaryPolicyError("the top of book is crossed; refusing to evaluate the spread.")
    mid = (best_bid + best_ask) / Decimal(2)
    return ((best_ask - best_bid) / mid) * TEN_THOUSAND


def require_admissible_spread(
    best_bid: Decimal, best_ask: Decimal, rules: ExecutionPriceRules = EXECUTION_PRICE_RULES
) -> Decimal:
    """Fail closed when the top-of-book spread exceeds the admission rule."""
    observed = spread_bps(best_bid, best_ask)
    if observed > rules.max_spread_bps:
        raise CanaryPolicyError(
            f"the top-of-book spread ({observed:.4f} bps) exceeds the admission limit "
            f"({rules.max_spread_bps} bps)."
        )
    return observed


def absolute_bound_price(
    reference_mid: Decimal,
    side: ExecutionSide,
    rules: ExecutionPriceRules = EXECUTION_PRICE_RULES,
) -> Decimal:
    """The furthest price a future order may ever carry, relative to the reference mid."""
    if (
        not isinstance(reference_mid, Decimal)
        or not reference_mid.is_finite()
        or reference_mid <= 0
    ):
        raise CanaryPolicyError("reference_mid must be a finite positive Decimal.")
    if not isinstance(side, ExecutionSide):
        raise CanaryPolicyError("side must be an ExecutionSide.")
    delta = reference_mid * rules.absolute_price_bound_bps / TEN_THOUSAND
    return reference_mid + delta if side is ExecutionSide.BUY else reference_mid - delta


def require_within_absolute_bound(
    reference_mid: Decimal,
    limit_price: Decimal,
    side: ExecutionSide,
    rules: ExecutionPriceRules = EXECUTION_PRICE_RULES,
) -> None:
    """Fail closed when a hypothetical limit price breaches the catastrophic execution ceiling."""
    if (
        not isinstance(limit_price, Decimal)
        or not limit_price.is_finite()
        or limit_price <= 0
    ):
        raise CanaryPolicyError("limit_price must be a finite positive Decimal.")
    bound = absolute_bound_price(reference_mid, side, rules)
    if side is ExecutionSide.BUY and limit_price > bound:
        raise CanaryPolicyError(
            f"limit price {limit_price} breaches the absolute upper bound {bound}."
        )
    if side is ExecutionSide.SELL and limit_price < bound:
        raise CanaryPolicyError(
            f"limit price {limit_price} breaches the absolute lower bound {bound}."
        )


@dataclass(frozen=True)
class CanaryEntryOrder:
    """A future canary entry order - price-bounded LIMIT + FOK only, never sent in this phase."""

    symbol: str
    side: ExecutionSide
    quantity: Decimal
    limit_price: Decimal
    reference_mid: Decimal
    market_snapshot_id: str
    order_type: str = CANARY_ENTRY_ORDER_TYPE
    time_in_force: str = CANARY_ENTRY_TIME_IN_FORCE

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise CanaryPolicyError("a canary entry requires a symbol.")
        if not isinstance(self.side, ExecutionSide):
            raise CanaryPolicyError("side must be an ExecutionSide.")
        for name in ("quantity", "limit_price", "reference_mid"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise CanaryPolicyError(f"{name} must be a finite positive Decimal.")
        if not isinstance(self.market_snapshot_id, str) or not self.market_snapshot_id.strip():
            raise CanaryPolicyError("a market snapshot id is required.")
        if self.order_type != CANARY_ENTRY_ORDER_TYPE:
            raise CanaryPolicyError(
                f"the canary entry policy requires {CANARY_ENTRY_ORDER_TYPE}."
            )
        if self.time_in_force != CANARY_ENTRY_TIME_IN_FORCE:
            raise CanaryPolicyError(
                f"the canary entry policy requires {CANARY_ENTRY_TIME_IN_FORCE}."
            )
        require_within_absolute_bound(self.reference_mid, self.limit_price, self.side)

    def to_record(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "limit_price": str(self.limit_price),
            "reference_mid": str(self.reference_mid),
            "market_snapshot_id": self.market_snapshot_id,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
        }


def build_canary_entry(
    *,
    symbol: str,
    side: ExecutionSide,
    quantity: Decimal,
    limit_price: Decimal,
    reference_mid: Decimal,
    market_snapshot_id: str,
    order_type: str,
    time_in_force: str,
) -> CanaryEntryOrder:
    """Build the ONLY supported future entry order shape: price-bounded LIMIT + FOK.

    IOC and GTC entries are refused, and there is no automatic fallback of any kind.
    """
    normalised_type = str(order_type).strip().upper()
    normalised_tif = str(time_in_force).strip().upper()
    if normalised_type != CANARY_ENTRY_ORDER_TYPE:
        raise CanaryPolicyError(
            f"the canary entry policy requires a {CANARY_ENTRY_ORDER_TYPE} order; "
            f"{normalised_type!r} is refused and there is no automatic fallback."
        )
    if normalised_tif != CANARY_ENTRY_TIME_IN_FORCE:
        raise CanaryPolicyError(
            f"the canary entry policy requires {CANARY_ENTRY_TIME_IN_FORCE}; {normalised_tif!r} is "
            "refused (no IOC, no GTC, no fallback)."
        )
    if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
        raise CanaryPolicyError("quantity must be a finite positive Decimal.")
    if (
        not isinstance(limit_price, Decimal)
        or not limit_price.is_finite()
        or limit_price <= 0
    ):
        raise CanaryPolicyError("limit_price must be a finite positive Decimal.")
    if not market_snapshot_id:
        raise CanaryPolicyError("a market snapshot id is required.")
    require_within_absolute_bound(reference_mid, limit_price, side)
    return CanaryEntryOrder(
        symbol=symbol,
        side=side,
        quantity=quantity,
        limit_price=limit_price,
        reference_mid=reference_mid,
        market_snapshot_id=market_snapshot_id,
    )


class NoChasingPolicy:
    """Single-use market snapshots: no re-price, no re-submit, no strategist retry.

    No-chasing stays enabled indefinitely. A fill that does not fully complete simply expires the
    opportunity: the old snapshot can never be reused and a brand new snapshot, decision, validity
    evaluation and deterministic risk evaluation are required.
    """

    def __init__(self) -> None:
        self._consumed: set[str] = set()

    @property
    def no_chasing_enabled(self) -> bool:
        return True

    @property
    def consumed(self) -> frozenset[str]:
        return frozenset(self._consumed)

    def consume(self, market_snapshot_id: str) -> str:
        """Mark one snapshot as used; reusing it is refused deterministically."""
        if not isinstance(market_snapshot_id, str) or not market_snapshot_id.strip():
            raise CanaryPolicyError("a market snapshot id is required.")
        if market_snapshot_id in self._consumed:
            raise OldSnapshotReuseError(
                "this market snapshot was already consumed; no re-price, no re-submit and no "
                "retry may reuse it. Wait for a NEW snapshot, decision, validity and risk pass."
            )
        self._consumed.add(market_snapshot_id)
        return market_snapshot_id


@dataclass(frozen=True)
class FillSlippageRecord:
    """Every future fill must be able to record these four values."""

    reference_mid: Decimal
    limit_price: Decimal
    average_fill_price: Decimal
    realized_slippage_bps: Decimal
    side: ExecutionSide

    def __post_init__(self) -> None:
        for name in ("reference_mid", "limit_price", "average_fill_price"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise CanaryPolicyError(f"{name} must be a finite positive Decimal.")
        if (
            not isinstance(self.realized_slippage_bps, Decimal)
            or not self.realized_slippage_bps.is_finite()
        ):
            raise CanaryPolicyError("realized_slippage_bps must be a finite Decimal.")
        if not isinstance(self.side, ExecutionSide):
            raise CanaryPolicyError("side must be an ExecutionSide.")

    def to_record(self) -> dict[str, object]:
        return {
            "reference_mid": str(self.reference_mid),
            "limit_price": str(self.limit_price),
            "average_fill_price": str(self.average_fill_price),
            "realized_slippage_bps": str(self.realized_slippage_bps),
            "side": self.side.value,
            "note": "realized slippage is recorded, never targeted",
        }


def realized_slippage_bps(
    reference_mid: Decimal, average_fill_price: Decimal, side: ExecutionSide
) -> Decimal:
    """Signed adverse-positive slippage of an average fill against the reference mid."""
    for name, value in (
        ("reference_mid", reference_mid),
        ("average_fill_price", average_fill_price),
    ):
        if not isinstance(value, Decimal) or value <= 0:
            raise CanaryPolicyError(f"{name} must be a positive Decimal.")
    if not isinstance(side, ExecutionSide):
        raise CanaryPolicyError("side must be an ExecutionSide.")
    raw = (average_fill_price - reference_mid) / reference_mid * TEN_THOUSAND
    return raw if side is ExecutionSide.BUY else -raw


def fill_slippage_record(
    *,
    reference_mid: Decimal,
    limit_price: Decimal,
    average_fill_price: Decimal,
    side: ExecutionSide,
) -> FillSlippageRecord:
    """Build the mandatory four-value fill record for a future canary fill."""
    return FillSlippageRecord(
        reference_mid=reference_mid,
        limit_price=limit_price,
        average_fill_price=average_fill_price,
        realized_slippage_bps=realized_slippage_bps(reference_mid, average_fill_price, side),
        side=side,
    )


def classify_execution_sample(completed_fills: int) -> str:
    """Classify a future canary review window by its completed FOK fill count.

    Fewer than the minimum is ``INSUFFICIENT_EXECUTION_SAMPLE``: not a system failure, and it never
    widens the 15 bps allowance automatically. Any parameter change needs explicit owner review.
    """
    if (
        isinstance(completed_fills, bool)
        or not isinstance(completed_fills, int)
        or completed_fills < 0
    ):
        raise CanaryPolicyError("completed_fills must be a non-negative integer.")
    if completed_fills < MIN_FOK_FILLS_FOR_OWNER_REVIEW:
        return INSUFFICIENT_EXECUTION_SAMPLE
    return "SUFFICIENT_EXECUTION_SAMPLE"
