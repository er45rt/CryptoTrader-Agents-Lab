"""Deterministic decision validity gate (Phase 6).

Precedence (as defined by the program): EXPIRED -> STALE -> MARKET_MOVED -> SUPERSEDED ->
BACKPRESSURE_REJECTED -> ACCEPTED.

The gate is a pure function of its inputs (no wall-clock reads, no randomness), so the same
historical market-state sequence plus the same intent always produces the same disposition.

TOCTOU: local checks only *narrow* the race window up to the last locally controlled
admission point. They never make execution atomic with venue acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from crypto_trader.events import ValidityDispositionPayload, ValidityDispositionRecorded

from .errors import ValidityConfigError
from .market_state import MarketStateProvider
from .policy import ValidityPolicy

__all__ = [
    "DecisionValidityGate",
    "Disposition",
    "ValidityDecision",
    "ValidityRequest",
    "to_phase3_event",
]


class Disposition(str, Enum):
    """The fixed Phase 6 disposition vocabulary (engine-independent)."""

    ACCEPTED = "accepted"
    EXPIRED = "expired"
    STALE = "stale"
    MARKET_MOVED = "market_moved"
    SUPERSEDED = "superseded"
    BACKPRESSURE_REJECTED = "backpressure_rejected"


@dataclass(frozen=True)
class ValidityRequest:
    """A trade intent presented to the validity gate, with its original market reference."""

    correlation_id: UUID
    symbol: str
    decision_started_at: datetime
    decision_completed_at: datetime
    market_event_ts: datetime
    reference_price: Decimal
    market_reference: str
    intent_id: UUID = field(default_factory=uuid4)
    decision_scope: str = "default"
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("decision_started_at", "decision_completed_at", "market_event_ts", "valid_until"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValidityConfigError(f"{name} must be timezone-aware.")
        if not isinstance(self.reference_price, Decimal) or self.reference_price <= 0:
            raise ValidityConfigError("reference_price must be a positive Decimal.")


@dataclass(frozen=True)
class ValidityDecision:
    """The deterministic verdict for one intent, with the inputs used."""

    disposition: Disposition
    reason_code: str
    intent_id: UUID
    correlation_id: UUID
    symbol: str
    policy_id: str
    decision_age_seconds: float
    market_event_ts: datetime
    decision_completed_at: datetime
    market_move_bps: float | None
    inputs: dict[str, Any] = field(default_factory=dict)


class DecisionValidityGate:
    """Deterministic validity/staleness gate that runs before Hard Risk."""

    def __init__(self, policy: ValidityPolicy) -> None:
        if not isinstance(policy, ValidityPolicy):
            raise ValidityConfigError("a resolved ValidityPolicy is required (fail closed).")
        self._policy = policy

    @property
    def policy(self) -> ValidityPolicy:
        return self._policy

    def evaluate(
        self,
        request: ValidityRequest,
        *,
        market_state: MarketStateProvider,
        now: datetime,
        latest_scope_intent_id: UUID | None = None,
        capacity_available: bool = True,
    ) -> ValidityDecision:
        """Evaluate one intent. ``now`` is supplied by the caller so runs stay deterministic."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValidityConfigError("now must be timezone-aware.")
        now_utc = now.astimezone(timezone.utc)

        age_seconds = (now_utc - request.market_event_ts.astimezone(timezone.utc)).total_seconds()
        current = market_state.latest(request.symbol)
        move_bps: float | None = None
        if current is not None:
            move = (current.price - request.reference_price) / request.reference_price
            move_bps = float(abs(move) * Decimal(10_000))

        inputs: dict[str, Any] = {
            "now": now_utc.isoformat(),
            "market_event_ts": request.market_event_ts.astimezone(timezone.utc).isoformat(),
            "decision_started_at": request.decision_started_at.astimezone(timezone.utc).isoformat(),
            "decision_completed_at": request.decision_completed_at.astimezone(timezone.utc).isoformat(),
            "reference_price": str(request.reference_price),
            "market_reference": request.market_reference,
            "current_market_reference": None if current is None else current.market_reference,
            "current_price": None if current is None else str(current.price),
            "market_move_bps": move_bps,
            "max_age_seconds": self._policy.max_age_seconds,
            "max_market_move_bps": str(self._policy.max_market_move_bps),
            "valid_until_seconds": self._policy.valid_until_seconds,
            "capacity_available": capacity_available,
            "latest_scope_intent_id": None if latest_scope_intent_id is None else str(latest_scope_intent_id),
            "policy_id": self._policy.policy_id,
        }

        return self._decide(
            request=request,
            now_utc=now_utc,
            age_seconds=age_seconds,
            move_bps=move_bps,
            current_available=current is not None,
            latest_scope_intent_id=latest_scope_intent_id,
            capacity_available=capacity_available,
            inputs=inputs,
        )

    def _decide(
        self,
        *,
        request: ValidityRequest,
        now_utc: datetime,
        age_seconds: float,
        move_bps: float | None,
        current_available: bool,
        latest_scope_intent_id: UUID | None,
        capacity_available: bool,
        inputs: dict[str, Any],
    ) -> ValidityDecision:
        def decision(disposition: Disposition, reason_code: str) -> ValidityDecision:
            return ValidityDecision(
                disposition=disposition,
                reason_code=reason_code,
                intent_id=request.intent_id,
                correlation_id=request.correlation_id,
                symbol=request.symbol,
                policy_id=self._policy.policy_id,
                decision_age_seconds=age_seconds,
                market_event_ts=request.market_event_ts,
                decision_completed_at=request.decision_completed_at,
                market_move_bps=move_bps,
                inputs=inputs,
            )

        # 1. EXPIRED: an explicit deadline (intent-level or policy-level) has passed.
        deadlines: list[datetime] = []
        if request.valid_until is not None:
            deadlines.append(request.valid_until.astimezone(timezone.utc))
        if self._policy.valid_until_seconds is not None:
            deadlines.append(
                (
                    request.decision_completed_at
                    + timedelta(seconds=self._policy.valid_until_seconds)
                ).astimezone(timezone.utc)
            )
        if deadlines and now_utc > min(deadlines):
            return decision(Disposition.EXPIRED, "valid_until_exceeded")

        # 2. STALE: age measured from the ORIGINAL market observation. A negative age means the
        # market event is timestamped after "now" (clock skew, a bad producer, or malformed data)
        # and must fail closed exactly like an over-age observation, never be treated as fresh.
        if age_seconds < 0:
            return decision(Disposition.STALE, "market_event_ts_in_future")
        if age_seconds > self._policy.max_age_seconds:
            return decision(Disposition.STALE, "max_age_exceeded")

        # A missing current observation means movement cannot be verified: fail closed as STALE.
        if not current_available:
            return decision(Disposition.STALE, "market_state_unavailable")

        # 3. MARKET_MOVED: time-valid, but the reference market moved beyond tolerance.
        if move_bps is not None and Decimal(str(move_bps)) > self._policy.max_market_move_bps:
            return decision(Disposition.MARKET_MOVED, "market_move_exceeded")

        # 4. SUPERSEDED: a newer intent exists for the same decision scope.
        if latest_scope_intent_id is not None and latest_scope_intent_id != request.intent_id:
            return decision(Disposition.SUPERSEDED, "newer_intent_for_scope")

        # 5. BACKPRESSURE_REJECTED: otherwise valid, but bounded capacity is exhausted.
        if not capacity_available:
            return decision(Disposition.BACKPRESSURE_REJECTED, "bounded_capacity_exhausted")

        return decision(Disposition.ACCEPTED, "within_policy")


def to_phase3_event(
    decision: ValidityDecision,
    *,
    producer: str = "application.validity",
    occurred_at: datetime | None = None,
    notes: str | None = None,
    event_id: UUID | None = None,
) -> ValidityDispositionRecorded:
    """Translate a validity decision into the additive Phase 6 event contract."""
    return ValidityDispositionRecorded(
        event_id=event_id or uuid4(),
        # The event records when the DECISION was taken; the original market observation time
        # travels in the payload and is what freshness is measured from.
        occurred_at=occurred_at or decision.decision_completed_at,
        correlation_id=decision.correlation_id,
        producer=producer,
        symbol=decision.symbol,
        payload=ValidityDispositionPayload(
            disposition=decision.disposition.value,
            reason_code=decision.reason_code,
            intent_id=decision.intent_id,
            policy_id=decision.policy_id,
            decision_age_seconds=decision.decision_age_seconds,
            market_event_ts=decision.market_event_ts,
            decision_completed_at=decision.decision_completed_at,
            market_move_bps=decision.market_move_bps,
            notes=notes,
        ),
    )
