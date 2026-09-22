"""Engine-independent recovery/reconciliation models (Phase 8).

Two views are reconciled without either silently overwriting the other:

* ``LocalOrderState``   - what OUR application journal says about an order.
* ``VenueOrderReport``  - what the execution/venue view reports about an order.

Every correction is an explicit :class:`ReconciliationAdjustment` that REQUIRES provenance
(``source``), the ``before`` state, the ``after`` state and a ``reason_code``. There is no
silent state correction anywhere in this module.

Nothing here imports Nautilus, CrewAI, the LLM layer or any storage engine: the recovery layer
is engine-independent domain/application code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID

from .errors import ReconciliationError

__all__ = [
    "AdjustmentSource",
    "LocalOrderState",
    "OrderStateName",
    "ReconciliationAdjustment",
    "ReconciliationOutcome",
    "ReconciliationReport",
    "STATE_RANK",
    "TERMINAL_STATES",
    "VenueOrderReport",
]

ZERO = Decimal("0")

#: States after which no further order progress is possible.
TERMINAL_STATES = frozenset({"filled", "canceled", "rejected", "expired"})

#: Deterministic lifecycle progression used to decide which side is "ahead".
STATE_RANK = {
    "created": 0,
    "submitted": 1,
    "accepted": 2,
    "partially_filled": 3,
    "filled": 4,
    "canceled": 4,
    "rejected": 4,
    "expired": 4,
}


class OrderStateName(str, Enum):
    """The engine-independent lifecycle vocabulary (mirrors the execution boundary)."""

    CREATED = "created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self.value in TERMINAL_STATES

    @property
    def rank(self) -> int:
        return STATE_RANK[self.value]


class AdjustmentSource(str, Enum):
    """Where the evidence for a reconciliation adjustment came from."""

    VENUE_REPORT = "venue_report"
    APPLICATION_JOURNAL = "application_journal"
    OPERATOR_POLICY = "operator_policy"


def _require_non_negative_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal) or value < ZERO:
        raise ReconciliationError(f"{name} must be a non-negative Decimal.")


@dataclass(frozen=True)
class LocalOrderState:
    """What our application journal believes about one order.

    ``order_id`` is the Phase 3 order identity (the envelope ``order_id`` carried by
    ``order.state.changed`` / ``execution.fill.recorded``). ``order_reference`` is the stable
    correlation key shared with the execution/venue view.

    ``submission_attempted`` MUST be recorded BEFORE a send is attempted, so its absence proves
    that no submission could have reached the venue. That is what makes a crash *before* a send
    distinguishable from an ambiguous (possibly sent) submission.
    """

    order_id: UUID
    order_reference: str
    intent_id: UUID | None
    correlation_id: UUID | None
    symbol: str
    state: OrderStateName
    filled_quantity: Decimal = ZERO
    remaining_quantity: Decimal = ZERO
    admitted_locally: bool = True
    submission_attempted: bool = False

    def __post_init__(self) -> None:
        if not self.order_reference:
            raise ReconciliationError("local order state requires an order reference.")
        if not self.symbol:
            raise ReconciliationError("local order state requires a symbol.")
        if not isinstance(self.state, OrderStateName):
            raise ReconciliationError("local order state requires an OrderStateName.")
        _require_non_negative_decimal("filled_quantity", self.filled_quantity)
        _require_non_negative_decimal("remaining_quantity", self.remaining_quantity)

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @property
    def is_open(self) -> bool:
        return not self.is_terminal


@dataclass(frozen=True)
class VenueOrderReport:
    """What the venue/execution view reports about one order."""

    order_reference: str
    venue_order_id: str | None
    state: OrderStateName
    filled_quantity: Decimal = ZERO
    remaining_quantity: Decimal = ZERO
    reported_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.order_reference:
            raise ReconciliationError("venue report requires an order reference.")
        if not isinstance(self.state, OrderStateName):
            raise ReconciliationError("venue report requires an OrderStateName.")
        _require_non_negative_decimal("filled_quantity", self.filled_quantity)
        _require_non_negative_decimal("remaining_quantity", self.remaining_quantity)
        if self.reported_at is not None and (
            self.reported_at.tzinfo is None or self.reported_at.utcoffset() is None
        ):
            raise ReconciliationError("reported_at must be timezone-aware when present.")

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    def normalized_key(self) -> tuple:
        """A normalized, comparable key (used to detect consistent duplicate reports)."""
        reported = (
            None
            if self.reported_at is None
            else self.reported_at.astimezone(timezone.utc).isoformat()
        )
        return (
            self.state.value,
            str(self.filled_quantity),
            str(self.remaining_quantity),
            self.venue_order_id,
            reported,
        )


@dataclass(frozen=True)
class ReconciliationAdjustment:
    """An explicit, evidenced correction. Every field is mandatory by construction."""

    source: AdjustmentSource
    before: LocalOrderState
    after: LocalOrderState
    reason_code: str

    def __post_init__(self) -> None:
        if not self.reason_code:
            raise ReconciliationError("every adjustment requires a reason code.")
        if self.before.order_reference != self.after.order_reference:
            raise ReconciliationError("an adjustment cannot change the order reference.")
        if self.before.order_id != self.after.order_id:
            raise ReconciliationError("an adjustment cannot change the order identity.")
        if not isinstance(self.source, AdjustmentSource):
            raise ReconciliationError("an adjustment requires an AdjustmentSource.")


class ReconciliationOutcome(str, Enum):
    """The deterministic outcome for one order reference."""

    MATCHED = "matched"
    RESOLVED_FROM_VENUE = "resolved_from_venue"
    LOCAL_AHEAD_OF_VENUE = "local_ahead_of_venue"
    AMBIGUOUS_PENDING_RECONCILIATION = "unknown_pending_reconciliation"
    NEVER_SUBMITTED = "never_submitted"
    UNKNOWN_EXTERNAL_ORDER = "unknown_external_order"
    VENUE_DUPLICATE_REPORT = "venue_duplicate_report"

    @property
    def is_unresolved(self) -> bool:
        """Outcomes that need external evidence / operator action and MUST block new risk."""
        return self.value in {
            "local_ahead_of_venue",
            "unknown_pending_reconciliation",
            "unknown_external_order",
        }


@dataclass(frozen=True)
class ReconciliationReport:
    """The complete deterministic reconciliation result."""

    outcomes: dict[str, ReconciliationOutcome] = field(default_factory=dict)
    reason_codes: dict[str, str] = field(default_factory=dict)
    order_ids: dict[str, UUID] = field(default_factory=dict)
    correlations: dict[str, UUID] = field(default_factory=dict)
    adjustments: list[ReconciliationAdjustment] = field(default_factory=list)
    resolved_orders: list[str] = field(default_factory=list)
    ambiguous_orders: list[str] = field(default_factory=list)
    unknown_external_orders: list[str] = field(default_factory=list)
    duplicate_reports: list[str] = field(default_factory=list)
    never_submitted: list[str] = field(default_factory=list)
    local_ahead_of_venue: list[str] = field(default_factory=list)
    requires_halt_new_risk: bool = False

    def outcome_for(self, order_reference: str) -> ReconciliationOutcome | None:
        return self.outcomes.get(order_reference)

    def reason_for(self, order_reference: str) -> str | None:
        return self.reason_codes.get(order_reference)

    @property
    def unresolved_orders(self) -> list[str]:
        """Orders that stay unresolved and MUST NOT be retried blindly."""
        return sorted(
            {
                *self.ambiguous_orders,
                *self.local_ahead_of_venue,
                *self.unknown_external_orders,
            }
        )

    @property
    def is_fully_resolved(self) -> bool:
        return not self.unresolved_orders

    def affected_order_ids(self) -> list[UUID]:
        """The known Phase 3 order identities touched by this reconciliation."""
        return sorted(
            {
                self.order_ids[reference]
                for reference in self.outcomes
                if reference in self.order_ids
            },
            key=str,
        )

    def distinct_correlation_ids(self) -> list[UUID]:
        """The distinct correlation identities touched by this reconciliation."""
        return sorted(set(self.correlations.values()), key=str)
