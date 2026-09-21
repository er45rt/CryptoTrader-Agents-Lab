"""Deterministic in-memory execution port (Phase 4 test double).

Proves the boundary is dependency-injectable without any engine. Engine-independent module:
no Nautilus imports.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence
from uuid import uuid4

from .errors import RuntimeStateError, UnsupportedExecutionRequestError
from .ports import (
    ExecutionObservation,
    ExecutionRequest,
    ExecutionRuntimeState,
    InstrumentSpec,
    OrderLifecycleState,
)

__all__ = ["InMemoryExecutionPort"]


class InMemoryExecutionPort:
    """Records requests and emits scripted lifecycle observations."""

    def __init__(self, instrument: InstrumentSpec, *, fill_fully: bool = True) -> None:
        self._instrument = instrument
        self._fill_fully = fill_fully
        self._state = ExecutionRuntimeState.STOPPED
        self._observations: list[ExecutionObservation] = []
        self._submitted: list[str] = []

    def start(self) -> None:
        self._state = ExecutionRuntimeState.RUNNING

    def submit(self, request: ExecutionRequest) -> str:
        if self._state is not ExecutionRuntimeState.RUNNING:
            raise RuntimeStateError("port is not running.")
        request.validate_for(self._instrument)
        order_reference = f"FAKE-{uuid4().hex[:12]}"
        self._submitted.append(order_reference)
        filled = request.quantity if self._fill_fully else request.quantity / 2
        for state in (OrderLifecycleState.SUBMITTED, OrderLifecycleState.ACCEPTED):
            self._observations.append(
                self._observation(request, order_reference, state, Decimal("0"), request.quantity)
            )
        remaining = request.quantity - filled
        self._observations.append(
            self._observation(
                request,
                order_reference,
                OrderLifecycleState.PARTIALLY_FILLED if remaining > 0 else OrderLifecycleState.FILLED,
                filled,
                remaining,
                last_fill_quantity=filled,
                last_fill_price=request.limit_price or Decimal("0"),
            )
        )
        if remaining > 0:
            self._observations.append(
                self._observation(
                    request,
                    order_reference,
                    OrderLifecycleState.FILLED,
                    request.quantity,
                    Decimal("0"),
                    last_fill_quantity=remaining,
                    last_fill_price=request.limit_price or Decimal("0"),
                )
            )
        return order_reference

    def observations(self) -> Sequence[ExecutionObservation]:
        return tuple(self._observations)

    def state(self) -> ExecutionRuntimeState:
        return self._state

    def stop(self) -> None:
        self._state = ExecutionRuntimeState.STOPPED

    def _observation(
        self,
        request: ExecutionRequest,
        order_reference: str,
        state: OrderLifecycleState,
        filled: Decimal,
        remaining: Decimal,
        *,
        last_fill_quantity: Decimal | None = None,
        last_fill_price: Decimal | None = None,
    ) -> ExecutionObservation:
        return ExecutionObservation(
            order_reference=order_reference,
            intent_id=request.intent_id,
            correlation_id=request.correlation_id,
            symbol=request.symbol,
            state=state,
            filled_quantity=filled,
            remaining_quantity=remaining,
            last_fill_quantity=last_fill_quantity,
            last_fill_price=last_fill_price,
            producer="test_double.in_memory",
        )
