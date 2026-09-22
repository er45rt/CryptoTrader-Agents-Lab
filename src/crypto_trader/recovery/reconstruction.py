"""Rebuild pending application order state from the Phase 5 journal (Phase 8).

Restart recovery step 1-3: restore a snapshot (acceleration only), replay the journal tail
deterministically, and rebuild what our application believes about each order.

The rebuild consumes ONLY engine-independent Phase 3 contracts:

* ``order.state.changed``   - lifecycle transitions (the envelope carries ``order_id``).
* ``execution.fill.recorded`` - fill quantities and the remaining open quantity.

A fill that arrives for an ``order_id`` with no prior lifecycle record is a genuine
inconsistency and fails visibly rather than being guessed at. Delivery stays at-least-once: the
underlying :class:`IdempotentConsumer` applies each ``event_id`` at most once.

Snapshot state is serialized to plain JSON primitives (strings, ints, lists) - never pickle -
so quantities/prices keep exact Decimal precision across a restart.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

from crypto_trader.events import (
    EXECUTION_FILL_EVENT_TYPE,
    ORDER_STATE_EVENT_TYPE,
    EventEnvelope,
)
from crypto_trader.persistence import EventJournal, IdempotentConsumer, RebuildSummary, rebuild_state
from crypto_trader.persistence import SnapshotRecord

from .errors import RecoveryError, ReconciliationError
from .models import LocalOrderState, OrderStateName, STATE_RANK

__all__ = [
    "RECONSTRUCTION_SNAPSHOT_SCHEMA",
    "OrderStateReconstructor",
    "reconstruct_order_states",
]

#: Schema version for the reconstructor's serialized snapshot state.
RECONSTRUCTION_SNAPSHOT_SCHEMA = 1

#: The lifecycle rank at/after which a submission attempt must have been recorded.
_SUBMISSION_ATTEMPT_RANK = STATE_RANK["submitted"]


class OrderStateReconstructor(IdempotentConsumer):
    """Deterministic rebuild of our application's belief about each order."""

    def __init__(self) -> None:
        super().__init__()
        self._states: dict[str, LocalOrderState] = {}

    # --- application ---

    def apply(self, event: EventEnvelope) -> None:
        if event.event_type == ORDER_STATE_EVENT_TYPE:
            self._apply_state_change(event)
        elif event.event_type == EXECUTION_FILL_EVENT_TYPE:
            self._apply_fill(event)

    def _apply_state_change(self, event: EventEnvelope) -> None:
        if event.order_id is None:
            raise RecoveryError("order.state.changed without an order_id cannot be reconstructed.")
        reference = str(event.order_id)
        state = OrderStateName(event.payload.state)
        existing = self._states.get(reference)
        if existing is None:
            existing = LocalOrderState(
                order_id=event.order_id,
                order_reference=reference,
                intent_id=None,
                correlation_id=event.correlation_id,
                symbol=event.symbol or "",
                state=state,
                filled_quantity=Decimal("0"),
                remaining_quantity=Decimal("0"),
                admitted_locally=True,
                submission_attempted=state.rank >= _SUBMISSION_ATTEMPT_RANK,
            )
        else:
            existing = replace(
                existing,
                state=state,
                correlation_id=existing.correlation_id or event.correlation_id,
                symbol=existing.symbol or (event.symbol or ""),
                submission_attempted=(
                    existing.submission_attempted or state.rank >= _SUBMISSION_ATTEMPT_RANK
                ),
            )
        self._states[reference] = existing

    def _apply_fill(self, event: EventEnvelope) -> None:
        if event.order_id is None:
            raise RecoveryError("execution.fill.recorded without an order_id cannot be reconstructed.")
        reference = str(event.order_id)
        existing = self._states.get(reference)
        if existing is None:
            raise ReconciliationError(
                f"fill recorded for order {reference} with no lifecycle record; refusing to guess."
            )
        filled = existing.filled_quantity + event.payload.quantity
        remaining = event.payload.remaining_quantity
        if remaining is None:
            remaining = max(Decimal("0"), existing.remaining_quantity - event.payload.quantity)
        state = existing.state
        if remaining == 0:
            state = OrderStateName.FILLED
        elif state.rank < STATE_RANK["partially_filled"]:
            state = OrderStateName.PARTIALLY_FILLED
        self._states[reference] = replace(
            existing,
            state=state,
            filled_quantity=filled,
            remaining_quantity=remaining,
        )

    # --- reads ---

    def states(self) -> tuple[LocalOrderState, ...]:
        return tuple(self._states[reference] for reference in sorted(self._states))

    def state_for(self, order_reference: str) -> LocalOrderState | None:
        return self._states.get(order_reference)

    def open_states(self) -> tuple[LocalOrderState, ...]:
        return tuple(state for state in self.states() if state.is_open)

    # --- snapshot (acceleration only; journal stays authoritative) ---

    def restore_state(self, state: dict[str, Any]) -> None:
        super().restore_state(state)
        schema = int(state.get("reconstruction_schema", 0))
        if schema != RECONSTRUCTION_SNAPSHOT_SCHEMA:
            raise RecoveryError(
                f"reconstruction snapshot schema {schema} is not supported "
                f"(expected {RECONSTRUCTION_SNAPSHOT_SCHEMA})."
            )
        self._states = {
            entry["order_reference"]: _local_state_from_dict(entry)
            for entry in state.get("orders", [])
        }
        self.seed_applied({UUID(value) for value in state.get("applied_event_ids", [])})

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "reconstruction_schema": RECONSTRUCTION_SNAPSHOT_SCHEMA,
            "orders": [_local_state_to_dict(state) for state in self.states()],
            "applied_event_ids": [str(event_id) for event_id in self.applied_events],
        }


def reconstruct_order_states(
    journal: EventJournal,
    *,
    snapshot: SnapshotRecord | None = None,
) -> tuple[OrderStateReconstructor, RebuildSummary]:
    """Restore a snapshot (if any), replay the journal tail, and return the reconstructed state."""
    reconstructor = OrderStateReconstructor()
    summary = rebuild_state(journal, reconstructor, snapshot=snapshot)
    return reconstructor, summary


def _local_state_to_dict(state: LocalOrderState) -> dict[str, Any]:
    return {
        "order_id": str(state.order_id),
        "order_reference": state.order_reference,
        "intent_id": None if state.intent_id is None else str(state.intent_id),
        "correlation_id": None if state.correlation_id is None else str(state.correlation_id),
        "symbol": state.symbol,
        "state": state.state.value,
        "filled_quantity": str(state.filled_quantity),
        "remaining_quantity": str(state.remaining_quantity),
        "admitted_locally": bool(state.admitted_locally),
        "submission_attempted": bool(state.submission_attempted),
    }


def _local_state_from_dict(entry: dict[str, Any]) -> LocalOrderState:
    return LocalOrderState(
        order_id=UUID(entry["order_id"]),
        order_reference=str(entry["order_reference"]),
        intent_id=None if entry.get("intent_id") is None else UUID(entry["intent_id"]),
        correlation_id=(
            None if entry.get("correlation_id") is None else UUID(entry["correlation_id"])
        ),
        symbol=str(entry["symbol"]),
        state=OrderStateName(entry["state"]),
        filled_quantity=Decimal(str(entry["filled_quantity"])),
        remaining_quantity=Decimal(str(entry["remaining_quantity"])),
        admitted_locally=bool(entry.get("admitted_locally", True)),
        submission_attempted=bool(entry.get("submission_attempted", False)),
    )
