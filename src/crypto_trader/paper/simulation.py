"""Simulated execution outcome + the engine-independent simulator interface (Phase 10).

The paper session depends only on the :class:`ExecutionSimulator` protocol. The Nautilus
implementation lives under ``crypto_trader.execution.nautilus`` (infrastructure); this module
contains only engine-independent types, so no engine name, type or import leaks into the
paper/shadow domain path.

The original scripted simulator is a legacy deterministic test double with batch-only semantics.
The separate TemporalExecutionSimulator protocol supports incremental, causal Paper replay
against a persistent execution engine; synthetic test-double fills are not performance evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid5

from crypto_trader.events import (
    ExecutionFillRecorded,
    FillRecordPayload,
    OrderStateChanged,
    OrderStateChangePayload,
)
from crypto_trader.execution import ExecutionObservation, ExecutionRequest, OrderLifecycleState

from .errors import SimulationError

__all__ = [
    "ExecutionSimulator",
    "TemporalExecutionSimulator",
    "SCRIPTED_SIM_NAMESPACE",
    "ScriptedSimulationRunner",
    "SimulationOutcome",
    "scripted_order_uuid",
]

#: Namespace for the scripted simulator's deterministic order identities.
SCRIPTED_SIM_NAMESPACE = UUID("5c5f1c2b-8f5e-4a0d-9f77-2f9c6b1d8e42")

#: Fixed simulated timestamp used by the scripted double when none is supplied. The double must
#: never read the wall clock: the same request must always yield byte-identical events so the
#: journal stays idempotent across restarts.
SCRIPTED_SIMULATED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def scripted_order_uuid(order_reference: str) -> UUID:
    """Stable engine-independent order id derived from the order reference."""
    return uuid5(SCRIPTED_SIM_NAMESPACE, order_reference)


@dataclass(frozen=True)
class SimulationOutcome:
    """Everything a simulated execution pass exposes to the session (our types only)."""

    observations: tuple[ExecutionObservation, ...] = ()
    phase3_events: tuple[Any, ...] = ()
    engine_version: str | None = None
    book_type: str | None = None
    queue_aware: bool = False

    def order_references(self) -> tuple[str, ...]:
        seen: list[str] = []
        for observation in self.observations:
            if observation.order_reference not in seen:
                seen.append(observation.order_reference)
        return tuple(seen)


@dataclass(frozen=True)
class CashEndEvidence:
    """Transient native final accounting, not a recoverable engine checkpoint."""

    initial_quote: Decimal
    base_total: Decimal
    base_free: Decimal
    base_locked: Decimal
    quote_total: Decimal
    quote_free: Decimal
    quote_locked: Decimal
    fees_quote: Decimal
    position_quantity: Decimal
    unsettled: bool
    account_type: str = "CASH"
    allow_borrowing: bool = False


@runtime_checkable
class HistoricalCashSimulator(Protocol):
    def configure_frozen(self, bundle: Any) -> None: ...
    def cash_end_evidence(self) -> CashEndEvidence: ...


@runtime_checkable
class ExecutionSimulator(Protocol):
    """The only simulated-execution surface the paper session may depend on."""

    def simulate(self, requests: Sequence[ExecutionRequest]) -> SimulationOutcome: ...


@runtime_checkable
class TemporalExecutionSimulator(ExecutionSimulator, Protocol):
    """Paper-only streaming surface, not a venue-writing execution port."""

    def start_temporal(self) -> None: ...
    def advance_until(self, timestamp_ns: int) -> SimulationOutcome: ...
    def submit_temporal(self, request: ExecutionRequest, *, decided_ns: int) -> None: ...
    def finish_temporal(self) -> SimulationOutcome: ...
    def abort_temporal(self) -> None: ...


@dataclass(frozen=True)
class ScriptedSimulationRunner:
    """Deterministic, offline simulator double (no engine, no network)."""

    fill_fraction: Decimal = Decimal("1")
    observed_at: datetime | None = None

    def simulate(self, requests: Sequence[ExecutionRequest]) -> SimulationOutcome:
        if not Decimal("0") < self.fill_fraction <= Decimal("1"):
            raise SimulationError("fill_fraction must be in (0, 1].")
        observations: list[ExecutionObservation] = []
        events: list[Any] = []
        for request in requests:
            reference = f"SIM-{request.intent_id.hex[:12]}"
            order_id = scripted_order_uuid(reference)
            price = request.limit_price or Decimal("0")
            if price <= 0:
                raise SimulationError("the scripted simulator requires a limit price.")
            fill_quantity = (request.quantity * self.fill_fraction).quantize(Decimal("0.00000001"))
            remaining = request.quantity - fill_quantity
            stamp = self.observed_at or SCRIPTED_SIMULATED_AT

            observations.append(
                self._observation(request, reference, OrderLifecycleState.SUBMITTED, Decimal("0"), request.quantity)
            )
            events.append(self._state_event(order_id, request, "submitted", stamp))
            observations.append(
                self._observation(request, reference, OrderLifecycleState.ACCEPTED, Decimal("0"), request.quantity)
            )
            events.append(self._state_event(order_id, request, "accepted", stamp))

            final_state = (
                OrderLifecycleState.FILLED if remaining == 0 else OrderLifecycleState.PARTIALLY_FILLED
            )
            observations.append(
                self._observation(
                    request,
                    reference,
                    final_state,
                    fill_quantity,
                    remaining,
                    last_fill_quantity=fill_quantity,
                    last_fill_price=price,
                )
            )
            events.append(self._state_event(order_id, request, final_state.value, stamp))
            events.append(
                ExecutionFillRecorded(
                    event_id=uuid5(SCRIPTED_SIM_NAMESPACE, f"fill:{reference}"),
                    occurred_at=stamp,
                    correlation_id=request.correlation_id,
                    producer="test_double.scripted_simulator",
                    order_id=order_id,
                    symbol=request.symbol,
                    payload=FillRecordPayload(
                        quantity=fill_quantity,
                        price=price,
                        remaining_quantity=remaining,
                        liquidity="taker",
                    ),
                )
            )
        return SimulationOutcome(observations=tuple(observations), phase3_events=tuple(events))

    @staticmethod
    def _observation(
        request: ExecutionRequest,
        reference: str,
        state: OrderLifecycleState,
        filled: Decimal,
        remaining: Decimal,
        *,
        last_fill_quantity: Decimal | None = None,
        last_fill_price: Decimal | None = None,
    ) -> ExecutionObservation:
        return ExecutionObservation(
            order_reference=reference,
            intent_id=request.intent_id,
            correlation_id=request.correlation_id,
            symbol=request.symbol,
            state=state,
            filled_quantity=filled,
            remaining_quantity=remaining,
            last_fill_quantity=last_fill_quantity,
            last_fill_price=last_fill_price,
            producer="test_double.scripted_simulator",
        )

    @staticmethod
    def _state_event(order_id: UUID, request: ExecutionRequest, state: str, stamp: datetime) -> OrderStateChanged:
        return OrderStateChanged(
            event_id=uuid5(SCRIPTED_SIM_NAMESPACE, f"state:{order_id}:{state}"),
            occurred_at=stamp,
            correlation_id=request.correlation_id,
            producer="test_double.scripted_simulator",
            order_id=order_id,
            symbol=request.symbol,
            payload=OrderStateChangePayload(state=state, reason=None),
        )
