"""Testnet order runtime: our execution port + reconciliation source (Phase 11).

This is the only place where the authenticated testnet client is driven. It:

* implements the Phase 4 engine-independent ``ExecutionPort`` protocol;
* journals every lifecycle/fill event through the existing Phase 3 contracts;
* exposes the Phase 8 ``VenueStateSource`` view so ambiguous orders are reconciled with evidence;
* honours the Phase 9 safety controller: a halt refuses new orders and cancel-all travels through
  the caller's priority safety lane;
* never retries an ambiguous submission and never converts one into a failure.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from crypto_trader.events import OrderStateChanged
from crypto_trader.persistence import EventJournal
from crypto_trader.recovery.models import LocalOrderState, VenueOrderReport
from crypto_trader.safety import SafetyController

from ..errors import RuntimeStateError, UnsupportedExecutionRequestError
from ..ports import (
    ExecutionObservation,
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionRuntimeState,
    InstrumentSpec,
    OrderLifecycleState,
)
from .client import BinanceSpotTestnetClient
from .errors import AmbiguousOrderError, BinanceTestnetError, TestnetRejectedError
from .models import TestnetOrder, TestnetOrderRequest, format_decimal
from .translate import (
    PRODUCER,
    deterministic_order_uuid,
    lifecycle_state_name,
    testnet_order_to_phase3_events,
)

__all__ = ["TestnetOrderRuntime", "TestnetVenueStateSource"]


@dataclass
class TestnetVenueStateSource:
    """The venue view used by Phase 8 reconciliation (evidence, never authority)."""

    client: BinanceSpotTestnetClient
    order_references: tuple[str, ...] = ()

    def reports(self) -> Sequence[VenueOrderReport]:
        reports: list[VenueOrderReport] = []
        for reference in self.order_references:
            try:
                order = self.client.order_status(self.client.symbol, reference)
            except TestnetRejectedError:
                # The venue does not know this order (for example -2013 "order does not exist"),
                # which is evidence in itself: report nothing rather than inventing a state.
                continue
            reports.append(
                VenueOrderReport(
                    order_reference=order.client_order_id,
                    venue_order_id=order.venue_order_id,
                    state=lifecycle_state_name(order.state),
                    filled_quantity=order.executed_quantity,
                    remaining_quantity=order.remaining_quantity,
                    reported_at=order.transact_time,
                )
            )
        return tuple(reports)


@dataclass
class TestnetOrderRuntime:
    """The engine-independent execution port over the Spot Testnet client."""

    client: BinanceSpotTestnetClient
    instrument: InstrumentSpec
    journal: EventJournal | None = None
    safety: SafetyController | None = None
    producer: str = PRODUCER
    _state: ExecutionRuntimeState = ExecutionRuntimeState.STOPPED
    _observations: list[ExecutionObservation] = field(default_factory=list)
    _orders: dict[str, TestnetOrder] = field(default_factory=dict)
    _intents: dict[str, UUID] = field(default_factory=dict)
    _correlations: dict[str, UUID] = field(default_factory=dict)
    _pending_submissions: dict[str, UUID] = field(default_factory=dict)
    _journaled: list[OrderStateChanged] = field(default_factory=list)

    # --- lifecycle ---

    def start(self) -> None:
        if self._state is ExecutionRuntimeState.RUNNING:
            raise RuntimeStateError("the testnet runtime is already running.")
        self.client.verify_environment(symbol=self.instrument.symbol)
        self._state = ExecutionRuntimeState.RUNNING

    def stop(self) -> None:
        self._state = ExecutionRuntimeState.STOPPED

    def state(self) -> ExecutionRuntimeState:
        return self._state

    # --- orders ---

    def client_order_id_for(self, intent_id: UUID) -> str:
        """Deterministic client order id: it IS the idempotency key across restarts."""
        return f"ct-{intent_id.hex[:20]}"

    def submit(self, request: ExecutionRequest) -> str:
        """Place one order. Returns the deterministic client order id."""
        if self._state is not ExecutionRuntimeState.RUNNING:
            raise RuntimeStateError("the testnet runtime must be started before submitting.")
        request.validate_for(self.instrument)
        if self.safety is not None and not self.safety.allow_new_risk():
            raise BinanceTestnetError(
                "new risk is halted by the safety controller; the order was not sent."
            )

        client_order_id = self.client_order_id_for(request.intent_id)
        self._intents[client_order_id] = request.intent_id
        self._correlations[client_order_id] = request.correlation_id
        self._pending_submissions[client_order_id] = request.correlation_id

        order_request = TestnetOrderRequest(
            client_order_id=client_order_id,
            symbol=request.symbol,
            side="BUY" if request.side.value == "buy" else "SELL",
            order_type="LIMIT" if request.order_type is ExecutionOrderType.LIMIT else "MARKET",
            quantity=request.quantity,
            price=request.limit_price,
            time_in_force=(
                request.time_in_force.value.upper()
                if request.order_type is ExecutionOrderType.LIMIT
                else None
            ),
        )

        try:
            order = self.client.place_order(order_request)
        except AmbiguousOrderError:
            # UNKNOWN_PENDING_RECONCILIATION: keep the submission marker so recovery can reconcile
            # it against venue evidence. Never retried here.
            raise
        except TestnetRejectedError:
            self._pending_submissions.pop(client_order_id, None)
            raise
        self._record(order, correlation_id=request.correlation_id)
        self._pending_submissions.pop(client_order_id, None)
        return client_order_id

    def cancel(self, client_order_id: str) -> TestnetOrder:
        """Cancel one order (idempotent)."""
        order = self.client.cancel_order(self.instrument.symbol, client_order_id)
        self._record(order, correlation_id=self._correlations.get(client_order_id, UUID(int=0)))
        return order

    def cancel_all(self) -> tuple[TestnetOrder, ...]:
        """Safety-lane cancel-all: cancel every open order for the instrument."""
        results = self.client.cancel_all_open_orders(self.instrument.symbol)
        for order in results:
            self._record(order, correlation_id=self._correlations.get(order.client_order_id))
        return results

    def refresh(self, client_order_id: str) -> TestnetOrder:
        """Fetch the venue's current view of one order (evidence, idempotent read)."""
        order = self.client.order_status(self.instrument.symbol, client_order_id)
        self._record(order, correlation_id=self._correlations.get(client_order_id))
        return order

    def recover_order(self, intent_id: UUID, *, correlation_id: UUID | None = None) -> TestnetOrder | None:
        """Restart path: adopt an order that already exists at the venue WITHOUT re-sending it.

        Returns ``None`` when the venue does not know the deterministic client order id - that is
        the genuinely ambiguous case (``UNKNOWN_PENDING_RECONCILIATION``) and is left for the
        Phase 8 reconciliation to resolve with evidence.
        """
        if self._state is not ExecutionRuntimeState.RUNNING:
            raise RuntimeStateError("the testnet runtime must be started before recovering.")
        reference = self.client_order_id_for(intent_id)
        correlation = correlation_id or self._correlations.get(reference)
        self._intents[reference] = intent_id
        if correlation is not None:
            self._correlations[reference] = correlation
        try:
            order = self.client.order_status(self.instrument.symbol, reference)
        except TestnetRejectedError:
            self._pending_submissions.setdefault(reference, correlation or UUID(int=0))
            return None
        self._record(order, correlation_id=correlation)
        self._pending_submissions.pop(reference, None)
        return order

    # --- reads ---

    def observations(self) -> Sequence[ExecutionObservation]:
        return tuple(self._observations)

    def phase3_events(self) -> Sequence[OrderStateChanged]:
        """The Phase 3 events this runtime has journaled (for observability/tests)."""
        return tuple(self._journaled)

    def local_states(self) -> tuple[LocalOrderState, ...]:
        """Our best local view, for Phase 8 reconciliation."""
        states: list[LocalOrderState] = []
        for reference, order in self._orders.items():
            states.append(
                LocalOrderState(
                    order_id=deterministic_order_uuid(reference),
                    order_reference=reference,
                    intent_id=self._intents.get(reference),
                    correlation_id=self._correlations.get(reference),
                    symbol=order.symbol,
                    state=lifecycle_state_name(order.state),
                    filled_quantity=order.executed_quantity,
                    remaining_quantity=order.remaining_quantity,
                    admitted_locally=True,
                    submission_attempted=True,
                )
            )
        for reference, correlation in self._pending_submissions.items():
            states.append(
                LocalOrderState(
                    order_id=deterministic_order_uuid(reference),
                    order_reference=reference,
                    intent_id=self._intents.get(reference),
                    correlation_id=correlation,
                    symbol=self.instrument.symbol,
                    state=lifecycle_state_name(OrderLifecycleState.SUBMITTED),
                    admitted_locally=True,
                    submission_attempted=True,
                )
            )
        return tuple(states)

    def venue_state_source(self, *, order_references: Sequence[str] | None = None):
        """A Phase 8 ``VenueStateSource`` over the orders this runtime knows about."""
        references = (
            tuple(order_references)
            if order_references is not None
            else tuple(self._orders) + tuple(self._pending_submissions)
        )
        return TestnetVenueStateSource(client=self.client, order_references=references)

    # --- internals ---

    def _record(self, order: TestnetOrder, *, correlation_id: UUID | None) -> None:
        correlation = correlation_id or UUID(int=0)
        events = testnet_order_to_phase3_events(order, correlation_id=correlation)
        for event in events:
            if self.journal is not None:
                self.journal.append(event)
            self._journaled.append(event)
        intent_id = self._intents.get(order.client_order_id)
        if intent_id is not None:
            self._observations.append(
                order.to_observation(
                    intent_id=intent_id, correlation_id=correlation, producer=self.producer
                )
            )
        self._orders[order.client_order_id] = order
