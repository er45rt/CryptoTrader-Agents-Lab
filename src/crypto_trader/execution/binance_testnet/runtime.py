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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from crypto_trader.events import (
    OrderStateChanged,
    OrderSubmissionAttemptPayload,
    OrderSubmissionAttempted,
)
from crypto_trader.persistence import EventJournal
from crypto_trader.recovery.models import LocalOrderState, VenueOrderReport
from crypto_trader.safety import SafetyController

from ..errors import RuntimeStateError, UnsupportedExecutionRequestError
from ..ports import (
    DurableAttemptJournal,
    DurableVenueWritePort,
    ExecutionObservation,
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionRuntimeState,
    InstrumentSpec,
    OrderLifecycleState,
    PreparedVenueWrite,
)
from .client import BinanceSpotTestnetClient, PreparedTestnetOrder
from .errors import (
    AmbiguousOrderError,
    AttemptJournalRequiredError,
    BinanceTestnetError,
    TestnetRejectedError,
    VenueProtocolRejectionError,
)
from .models import TestnetOrder, TestnetOrderRequest, format_decimal
from .transport import PreSendTransportError
from .translate import (
    PRODUCER,
    deterministic_order_uuid,
    lifecycle_state_name,
    legacy_fill_event_ids,
    testnet_order_to_phase3_events,
    testnet_order_venue_identity_event,
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
class TestnetOrderRuntime(DurableVenueWritePort):
    """The engine-independent execution port over the Spot Testnet client.

    Stage 3: this is the approved durable venue-writing port. It inherits
    :class:`DurableVenueWritePort`, whose runtime-owned ``submit_durably`` sequence commits the
    durable ``order.submission.attempted`` marker into the production runtime's own journal before
    the single send, and binds the exact prepared write to that committed marker. The adapter
    supplies only the hooks below, so the approved ordering cannot be bypassed by an implementation
    error.
    """

    client: BinanceSpotTestnetClient
    instrument: InstrumentSpec
    journal: EventJournal | None = None
    safety: SafetyController | None = None
    producer: str = PRODUCER
    #: Injected clock for durable markers/resolutions (never read at import time).
    clock: Callable[[], datetime] | None = None
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
        """Wait outside any ownership lock, then run the approved durable submission sequence."""
        self.await_write_admission()
        return self.submit_durably(request)

    def await_write_admission(self) -> None:
        """Wait on venue read/rate prerequisites outside the ownership submission lock."""
        if self._state is not ExecutionRuntimeState.RUNNING:
            raise RuntimeStateError("the testnet runtime must be started before submitting.")
        self.client.await_order_write_admission(self.instrument.symbol)

    # --- the approved durable boundary hooks ---------------------------------

    def durable_attempt_journal(self) -> DurableAttemptJournal | None:
        """The durable journal this port commits its attempt marker into."""
        return self.journal

    def _prepare_durable_write(self, request: ExecutionRequest) -> PreparedVenueWrite:
        """Every locally rejectable step, before the durable marker exists."""
        if self._state is not ExecutionRuntimeState.RUNNING:
            raise RuntimeStateError("the testnet runtime must be started before submitting.")
        request.validate_for(self.instrument)
        if self.safety is not None and not self.safety.allow_new_risk():
            raise BinanceTestnetError(
                "new risk is halted by the safety controller; the order was not sent."
            )
        if self.journal is None:
            raise AttemptJournalRequiredError(
                "refusing a venue write without a durable attempt journal: the "
                "order.submission.attempted marker cannot be committed before the send. No order "
                "was prepared and no request was sent."
            )

        client_order_id = self.client_order_id_for(request.intent_id)
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

        prior_attempt = self._has_durable_attempt(client_order_id, request=request)
        self._intents[client_order_id] = request.intent_id
        self._correlations[client_order_id] = request.correlation_id

        if (
            client_order_id in self._pending_submissions
            or prior_attempt
        ):
            try:
                order = self.client.adopt_order_status(order_request, wait=False)
            except BinanceTestnetError as exc:
                raise AmbiguousOrderError(
                    "a durable submission attempt already reserves this client order identity; "
                    "the venue identity could not be established, so the identity remains "
                    "permanently ineligible for another venue write."
                ) from exc
            self._record(order, correlation_id=request.correlation_id)
            self._pending_submissions.pop(client_order_id, None)
            return PreparedVenueWrite(handle=order, adopted_reference=client_order_id)

        prepared = self.client.prepare_order(order_request, wait=False)

        if not prepared.requires_write:
            # The venue already knows this order: adopt it without a marker and without a POST.
            order = self.client.submit_prepared(prepared)
            self._record(order, correlation_id=request.correlation_id)
            return PreparedVenueWrite(handle=prepared, adopted_reference=client_order_id)
        return PreparedVenueWrite(handle=prepared)

    def _durable_attempt_event(
        self, prepared: PreparedVenueWrite, request: ExecutionRequest
    ) -> OrderSubmissionAttempted:
        """Build the durable attempt marker for the exact prepared write."""
        client_prepared = prepared.handle
        assert isinstance(client_prepared, PreparedTestnetOrder)
        return OrderSubmissionAttempted(
            event_id=uuid4(),
            occurred_at=self._now(),
            correlation_id=request.correlation_id,
            producer=self.producer,
            strategy_id=request.strategy_id,
            order_id=deterministic_order_uuid(client_prepared.client_order_id),
            symbol=client_prepared.symbol,
            payload=OrderSubmissionAttemptPayload(
                venue_reference=client_prepared.client_order_id,
                intent_id=request.intent_id,
                side=request.side.value,
                order_type=request.order_type.value,
                quantity=request.quantity,
                limit_price=request.limit_price,
                time_in_force=request.time_in_force.value,
                venue_producer=PRODUCER,
            ),
        )

    def _bind_durable_authorization(
        self, prepared: PreparedVenueWrite, marker: object
    ) -> PreparedVenueWrite:
        """Bind the committed marker to the exact prepared write (after the append succeeded)."""
        client_prepared = prepared.handle
        assert isinstance(client_prepared, PreparedTestnetOrder)
        authorized = self.client._authorize_attempt(
            client_prepared,
            marker_event_id=marker.event_id,
            venue_reference=marker.payload.venue_reference,
            order_id=marker.order_id,
            intent_id=marker.payload.intent_id,
            correlation_id=marker.correlation_id,
            symbol=marker.symbol,
            side=marker.payload.side,
            order_type=marker.payload.order_type,
            quantity=marker.payload.quantity,
            limit_price=marker.payload.limit_price,
            time_in_force=marker.payload.time_in_force,
        )
        self._pending_submissions[authorized.client_order_id] = marker.correlation_id
        return PreparedVenueWrite(handle=authorized)

    def _send_authorized_write(
        self, prepared: PreparedVenueWrite, request: ExecutionRequest
    ) -> str:
        """Send the authorized write exactly once and classify the outcome (Stage 2 semantics)."""
        client_prepared = prepared.handle
        assert isinstance(client_prepared, PreparedTestnetOrder)
        client_order_id = client_prepared.client_order_id
        try:
            order = self.client.submit_prepared(client_prepared)
        except PreSendTransportError as exc:
            # Proven not transmitted: record the resolution, never retry inside this call.
            self._journal_attempt_resolution(
                client_prepared,
                request,
                state=OrderLifecycleState.CANCELED,
                reason=f"pre_send_failure_proven:{type(exc).__name__}",
            )
            self._pending_submissions.pop(client_order_id, None)
            raise
        except VenueProtocolRejectionError as exc:
            # The venue refused the request before any order was created (-1021).
            self._journal_attempt_resolution(
                client_prepared,
                request,
                state=OrderLifecycleState.REJECTED,
                reason=f"{type(exc).reason_code}:{type(exc).evidence_code}",
            )
            self._pending_submissions.pop(client_order_id, None)
            raise
        except AmbiguousOrderError:
            # UNKNOWN_PENDING_RECONCILIATION: keep the durable attempt and the pending marker so
            # recovery can reconcile it against authoritative venue evidence. Never retried here.
            raise
        except TestnetRejectedError as exc:
            self._journal_attempt_resolution(
                client_prepared,
                request,
                state=OrderLifecycleState.REJECTED,
                reason=f"venue_order_rejected:{type(exc).__name__}",
            )
            self._pending_submissions.pop(client_order_id, None)
            raise
        if not order.deduplicated:
            # Review G-4: only the venue's answer to our own write names our venue order; a later
            # read by client order id may show another order reusing it. Journaled before the
            # state, so no accepted state exists without its identity.
            assert self.journal is not None
            self.journal.append(
                testnet_order_venue_identity_event(
                    order, intent_id=request.intent_id, correlation_id=request.correlation_id
                )
            )
        self._record(order, correlation_id=request.correlation_id)
        self._pending_submissions.pop(client_order_id, None)
        return client_order_id

    def _journal_attempt_resolution(
        self,
        prepared: PreparedTestnetOrder,
        request: ExecutionRequest,
        *,
        state: OrderLifecycleState,
        reason: str,
    ) -> None:
        """Record a mechanically established outcome for a durable attempt."""
        assert self.journal is not None
        order = TestnetOrder(
            symbol=prepared.symbol,
            client_order_id=prepared.client_order_id,
            venue_order_id=None,
            state=state,
            status=state.value.upper(),
            original_quantity=request.quantity,
        )
        for event in testnet_order_to_phase3_events(
            order, correlation_id=request.correlation_id, occurred_at=self._now(), reason=reason
        ):
            self.journal.append(event)

    def _has_durable_attempt(self, client_order_id: str, *, request: ExecutionRequest | None = None) -> bool:
        """True when this deterministic venue identity has ever crossed the durable send boundary."""
        assert self.journal is not None
        read_range = getattr(self.journal, "read_range", None)
        if not callable(read_range):
            raise AttemptJournalRequiredError(
                "the durable journal cannot prove prior submission attempts; refusing a venue write."
            )
        attempts = [event for event in read_range() if isinstance(event, OrderSubmissionAttempted)
                    and event.payload.venue_reference == client_order_id]
        if len(attempts) > 1:
            raise AmbiguousOrderError("multiple durable attempts reserve the same venue identity.")
        if attempts and request is not None:
            event = attempts[0]
            payload = event.payload
            if (payload.intent_id, event.correlation_id, event.symbol, event.strategy_id,
                payload.side, payload.order_type, payload.quantity, payload.limit_price,
                payload.time_in_force) != (
                request.intent_id, request.correlation_id, request.symbol, request.strategy_id,
                request.side.value, request.order_type.value, request.quantity, request.limit_price,
                request.time_in_force.value,
            ):
                raise AmbiguousOrderError("full intent UUID or persisted attempt economics do not match.")
        return bool(attempts)

    def _now(self) -> datetime:
        stamp = self.clock() if self.clock is not None else datetime.now(timezone.utc)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise RuntimeStateError("the testnet runtime clock must return timezone-aware time.")
        return stamp.astimezone(timezone.utc)

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
        historical_fills = []
        if self.journal is not None and order.fills:
            historical_fills = [event for event in self.journal.read_range()
                                if event.order_id == deterministic_order_uuid(order.client_order_id)
                                and event.event_type == "execution.fill.recorded"]
        for index, event in enumerate(events):
            if self.journal is not None:
                existing = self.journal.read_by_id(event.event_id)
                if existing is None and event.event_type == "execution.fill.recorded":
                    trade_id = order.fills[index - 1].trade_id
                    candidates = legacy_fill_event_ids(
                        order.client_order_id, trade_id, len(historical_fills)
                    )
                    matches = [item for item in historical_fills if item.event_id in candidates]
                    if len(matches) > 1:
                        raise RuntimeStateError("legacy journal contains duplicate trade identities")
                    existing = matches[0] if matches else None
                if existing is not None:
                    if (existing.event_type, existing.order_id, existing.symbol, existing.producer) != (
                        event.event_type, event.order_id, event.symbol, event.producer
                    ):
                        raise RuntimeStateError("conflicting identity for a recorded venue event")
                    # Repeated polling may change snapshot metadata, never trade economics.
                    fields = ("quantity", "price", "fee", "fee_asset", "liquidity")
                    if event.event_type == "execution.fill.recorded":
                        if any(getattr(existing.payload, key) != getattr(event.payload, key)
                               for key in fields):
                            raise RuntimeStateError("conflicting economics for a recorded venue trade")
                    elif existing.payload != event.payload:
                        raise RuntimeStateError("conflicting payload for a recorded venue state")
                    event = existing
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
