"""Structured shadow/paper run reports (Phase 10).

The report is the observable outcome of a session: decisions, validity outcomes, risk outcomes,
queue/backpressure events, safety events, recovery events, simulated orders and fills, exposure
and simulated accounting.

It is engine-independent, deterministic and secret-free. SIMULATED ACCOUNTING IS NOT A
PERFORMANCE CLAIM AND NOT A SAFETY CERTIFICATION.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from crypto_trader.events import (
    EXECUTION_FILL_EVENT_TYPE,
    ORDER_STATE_EVENT_TYPE,
    RECOVERY_EVENT_TYPE,
    RISK_DECISION_EVENT_TYPE,
    SAFETY_EVENT_TYPE,
    STRATEGY_DECISION_EVENT_TYPE,
    VALIDITY_EVENT_TYPE,
)
from crypto_trader.recovery.models import OrderStateName

from .errors import PaperStateError

__all__ = ["DecisionRecord", "FillSummary", "RunReport", "OrderSummary"]


@dataclass(frozen=True)
class DecisionRecord:
    """One processed market observation and everything that happened to it."""

    index: int
    market_reference: str
    observed_at: datetime
    action: str
    confidence: str
    rationale: str
    intent_id: UUID | None = None
    validity_disposition: str | None = None
    validity_reason: str | None = None
    risk_decision: str | None = None
    risk_reason_codes: tuple[str, ...] = ()
    submission: str = "not_applicable"


@dataclass(frozen=True)
class OrderSummary:
    """Final simulated state of one order."""

    order_reference: str
    intent_id: UUID | None
    correlation_id: UUID | None
    symbol: str
    state: str
    filled_quantity: Decimal
    remaining_quantity: Decimal


@dataclass(frozen=True)
class FillSummary:
    """One simulated fill."""

    order_reference: str
    intent_id: UUID | None
    correlation_id: UUID | None
    symbol: str
    quantity: Decimal
    price: Decimal
    remaining_quantity: Decimal


@dataclass(frozen=True)
class RunReport:
    """The complete, deterministic outcome of one shadow/paper session."""

    session_id: UUID
    mode: str
    symbol: str
    strategy_id: str
    observations_processed: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    decisions: tuple[DecisionRecord, ...] = ()
    validity_counts: Mapping[str, int] = field(default_factory=dict)
    risk_counts: Mapping[str, int] = field(default_factory=dict)
    backpressure_rejections: int = 0
    stale_decisions: tuple[str, ...] = ()
    superseded_decisions: tuple[str, ...] = ()
    safety_blocked: int = 0
    safety_events: tuple[str, ...] = ()
    recovery_events: tuple[str, ...] = ()
    orders: tuple[OrderSummary, ...] = ()
    fills: tuple[FillSummary, ...] = ()
    positions: Mapping[str, Decimal] = field(default_factory=dict)
    realized_pnl_quote: Decimal = Decimal("0")
    unrealized_pnl_quote: Decimal = Decimal("0")
    equity_quote: Decimal = Decimal("0")
    engine_version: str | None = None
    book_type: str | None = None
    queue_aware: bool = False
    notes: tuple[str, ...] = ()

    # --- serialization ---

    def to_dict(self) -> dict[str, Any]:
        """A JSON-compatible mapping (Decimals/UUIDs/datetimes become strings)."""

        def convert(value: Any) -> Any:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, (UUID, datetime)):
                return str(value)
            if isinstance(value, Mapping):
                return {str(key): convert(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [convert(item) for item in value]
            return value

        return convert(asdict(self))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def write(self, path: str | Path) -> Path:
        """Write the report as one JSON document (never a secret)."""
        target = Path(path)
        target.write_text(self.to_json() + "\n", encoding="utf-8")
        return target

    def semantic_hash(self) -> str:
        """Hash of the semantic content only (no wall-clock, no process identity)."""
        payload = self.to_dict()
        for volatile in ("started_at", "ended_at"):
            payload.pop(volatile, None)
        key = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


def report_from_journal(
    events: Iterable[Any],
    *,
    session_id: UUID,
    mode: str,
    symbol: str,
    strategy_id: str,
) -> RunReport:
    """Rebuild the semantic outcome of a recorded session from its journal events.

    This is the replay check: the same recorded journal must always produce the same counts,
    dispositions, order states and fills.
    """
    decisions: list[tuple[str, str]] = []
    order_state: dict[str, str] = {}
    filled: dict[str, Decimal] = {}
    remaining: dict[str, Decimal] = {}
    fills: list[tuple[datetime, str, FillSummary]] = []
    validity_counts: dict[str, int] = {}
    risk_counts: dict[str, int] = {}
    stale: list[str] = []
    superseded: list[str] = []
    safety_events: list[str] = []
    recovery_events: list[str] = []
    backpressure = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    correlation_id: UUID | None = None

    def lifecycle_rank(state: str) -> int:
        try:
            return OrderStateName(state).rank
        except (ValueError, KeyError):
            return -1

    for event in events:
        # A journal may contain multiple sessions. Never attribute another
        # session's decisions, fills, risk or time range to this replay.
        if event.correlation_id != session_id:
            continue
        occurred_at = event.occurred_at
        started_at = occurred_at if started_at is None or occurred_at < started_at else started_at
        ended_at = occurred_at if ended_at is None or occurred_at > ended_at else ended_at
        if correlation_id is None and event.correlation_id == session_id:
            correlation_id = event.correlation_id
        event_type = event.event_type
        if event_type == STRATEGY_DECISION_EVENT_TYPE:
            decisions.append((event.payload.action, event.payload.confidence))
        elif event_type == VALIDITY_EVENT_TYPE:
            disposition = event.payload.disposition
            validity_counts[disposition] = validity_counts.get(disposition, 0) + 1
            if disposition == "stale":
                stale.append(event.payload.reason_code)
            elif disposition == "superseded":
                superseded.append(event.payload.reason_code)
            elif disposition == "backpressure_rejected":
                backpressure += 1
        elif event_type == RISK_DECISION_EVENT_TYPE:
            # The semantic decision is counted once; the additive rule audit is not double-counted.
            key = "allowed" if event.payload.approved else "rejected"
            risk_counts[key] = risk_counts.get(key, 0) + 1
        elif event_type == ORDER_STATE_EVENT_TYPE:
            key = str(event.order_id)
            current = order_state.get(key)
            if current is None or lifecycle_rank(event.payload.state) >= lifecycle_rank(current):
                order_state[key] = event.payload.state
        elif event_type == EXECUTION_FILL_EVENT_TYPE:
            key = str(event.order_id)
            filled[key] = filled.get(key, Decimal("0")) + event.payload.quantity
            remaining[key] = event.payload.remaining_quantity or Decimal("0")
            fills.append(
                (
                    occurred_at,
                    key,
                    FillSummary(
                        order_reference=key,
                        intent_id=None,
                        correlation_id=event.correlation_id,
                        symbol=event.symbol or symbol,
                        quantity=event.payload.quantity,
                        price=event.payload.price,
                        remaining_quantity=remaining[key],
                    ),
                )
            )
        elif event_type == SAFETY_EVENT_TYPE:
            safety_events.append(event.payload.mode)
        elif event_type == RECOVERY_EVENT_TYPE:
            recovery_events.append(event.payload.state)

    if not decisions:
        raise PaperStateError(f"no recorded decisions for session {session_id}.")

    ordered_fills = [item[2] for item in sorted(fills, key=lambda item: (item[0], item[1]))]

    return RunReport(
        session_id=session_id,
        mode=mode,
        symbol=symbol,
        strategy_id=strategy_id,
        observations_processed=len(decisions),
        started_at=started_at,
        ended_at=ended_at,
        validity_counts=validity_counts,
        risk_counts=risk_counts,
        backpressure_rejections=backpressure,
        stale_decisions=tuple(stale),
        superseded_decisions=tuple(superseded),
        safety_events=tuple(safety_events),
        recovery_events=tuple(recovery_events),
        orders=tuple(
            OrderSummary(
                order_reference=reference,
                intent_id=None,
                correlation_id=correlation_id,
                symbol=symbol,
                state=state,
                filled_quantity=filled.get(reference, Decimal("0")),
                remaining_quantity=remaining.get(reference, Decimal("0")),
            )
            for reference, state in sorted(order_state.items())
        ),
        fills=tuple(ordered_fills),
        notes=("rebuilt from the persisted journal (replay check)",),
    )
