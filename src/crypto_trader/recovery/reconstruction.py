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

Durable pre-send attempt (Phase 15 durable ambiguity)
-----------------------------------------------------
``order.submission.attempted`` is the durable fact that a submission was attempted. Replay treats
it as a FACT MERGE, never as a lifecycle regression: if a resolution was already applied (for
example ``FILLED``) and the attempt event sorts later, the attempt's durable facts are merged in
(``submission_attempted``, the venue-facing reference, intent/correlation identity and the
immutable economic anchors) while the lifecycle state and the resolved quantities stay untouched.

Every lifecycle change is then resolved by explicit rules over:

* explicit provenance - ``local_attempt`` / ``local_lifecycle`` /
  ``authoritative_venue_lifecycle`` / ``authoritative_venue_fill`` / ``no_attempt_recorded`` -
  taken from the durable ``venue_producer`` anchor, never inferred from time;
* the immutable economic anchors (order identity, symbol, side, order type, original quantity,
  limit price, time in force);
* quantity monotonicity (``executedQty`` never decreases and never exceeds the original quantity);
* terminal finality (``filled`` is absorbing; a terminal order never silently becomes open again).

A contradiction is never dropped: it is recorded as a lifecycle conflict for that reference, and
the recovery report keeps the order unresolved so new risk stays fenced until authoritative
evidence resolves it.

Journals written before this contract keep their original semantics: the reference falls back to
``str(order_id)`` and ``submission_attempted`` is derived from the lifecycle rank, exactly as
before.

Our venue order identity (review G-4)
-------------------------------------
``order.venue_identity.recorded`` binds the venue order id of our own accepted write to the attempt
it answered (``LocalOrderState.venue_order_id``). Only the first identity from the attempt's venue
producer, with the same reference, intent, order id and symbol, is bound. Every contradiction is a
conflict, and an order whose identity was never journaled keeps ``venue_order_id`` unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from crypto_trader.events import (
    EXECUTION_FILL_EVENT_TYPE,
    ORDER_STATE_EVENT_TYPE,
    ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE,
    ORDER_VENUE_IDENTITY_EVENT_TYPE,
    EventEnvelope,
)
from crypto_trader.persistence import EventJournal, IdempotentConsumer, RebuildSummary, rebuild_state
from crypto_trader.persistence import SnapshotRecord

from .errors import RecoveryError, ReconciliationError
from .models import LocalOrderState, OrderStateName, STATE_RANK

__all__ = [
    "RECONSTRUCTION_SNAPSHOT_SCHEMA",
    "AttemptAnchors",
    "OrderStateReconstructor",
    "Provenance",
    "validate_venue_evidence",
    "reconstruct_order_states",
]

#: Schema version for the reconstructor's serialized snapshot state.
RECONSTRUCTION_SNAPSHOT_SCHEMA = 2

#: Durable venue identities allowed to confer authoritative replay provenance.
_RECOGNIZED_VENUE_PRODUCERS = frozenset({"infrastructure.binance_testnet"})

#: The lifecycle rank at/after which a submission attempt must have been recorded.
_SUBMISSION_ATTEMPT_RANK = STATE_RANK["submitted"]

_ZERO = Decimal("0")

#: States after which the order is no longer open. Terminal finality is enforced explicitly; no
#: total "terminal strength" ordering is invented.
_TERMINAL_STATES = frozenset(
    {
        OrderStateName.FILLED,
        OrderStateName.CANCELED,
        OrderStateName.REJECTED,
        OrderStateName.EXPIRED,
    }
)

_FILLED = OrderStateName.FILLED
_PARTIALLY_FILLED = OrderStateName.PARTIALLY_FILLED
_SUBMITTED = OrderStateName.SUBMITTED


class Provenance(str, Enum):
    """Explicit provenance of one reconstruction fact.

    Authority is read from durable facts (the attempt's ``venue_producer`` anchor and the emitting
    event's producer), never inferred from ``occurred_at``.
    """

    #: The durable pre-send attempt fact itself.
    LOCAL_ATTEMPT = "local_attempt"
    #: A journal written before the attempt contract existed: provenance cannot be established.
    NO_ATTEMPT_RECORDED = "no_attempt_recorded"
    #: A lifecycle/fill fact emitted by a producer that is not the recorded venue producer.
    LOCAL_LIFECYCLE = "local_lifecycle"
    #: An authoritative venue lifecycle report for an order with a durable attempt record.
    AUTHORITATIVE_VENUE_LIFECYCLE = "authoritative_venue_lifecycle"
    #: An authoritative venue fill report for an order with a durable attempt record.
    AUTHORITATIVE_VENUE_FILL = "authoritative_venue_fill"

    @property
    def is_authoritative_venue(self) -> bool:
        return self in {
            Provenance.AUTHORITATIVE_VENUE_LIFECYCLE,
            Provenance.AUTHORITATIVE_VENUE_FILL,
        }


class _Decision(str, Enum):
    APPLY = "apply"
    IGNORE_AS_STALE = "ignore_as_stale"
    IDEMPOTENT_NO_OP = "idempotent_no_op"
    CONFLICT_KEEP_FENCED = "conflict_keep_fenced"


@dataclass(frozen=True)
class AttemptAnchors:
    """The immutable durable facts carried by one ``order.submission.attempted`` event."""

    order_reference: str
    order_id: UUID
    intent_id: UUID
    correlation_id: UUID | None
    symbol: str
    strategy_id: str | None
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal | None
    time_in_force: str
    venue_producer: str
    occurred_at_ns: int

    def identity_key(self) -> tuple:
        """The immutable economic identity: any difference is a conflict, never a new order."""
        return (
            str(self.order_id),
            str(self.intent_id),
            self.symbol,
            self.strategy_id,
            self.side,
            self.order_type,
            str(self.quantity),
            None if self.limit_price is None else str(self.limit_price),
            self.time_in_force,
        )


@dataclass(frozen=True)
class _CompletionFact:
    """Source evidence for a FILLED fact that arrived before its durable attempt anchor."""

    producer: str
    kind: str
    filled_quantity: Decimal | None = None
    remaining_quantity: Decimal | None = None


@dataclass(frozen=True)
class _IdentityFact:
    """Our journaled venue order identity (review G-4), held until its attempt is replayed."""

    order_id: UUID | None
    intent_id: UUID
    symbol: str | None
    producer: str
    venue_order_id: str


def _to_ns(value: Any) -> int:
    return int(value.timestamp() * 1_000_000_000)


class OrderStateReconstructor(IdempotentConsumer):
    """Deterministic rebuild of our application's belief about each order."""

    def __init__(self) -> None:
        super().__init__()
        self._states: dict[str, LocalOrderState] = {}
        self._attempts: dict[str, AttemptAnchors] = {}
        self._reference_by_order_id: dict[UUID, str] = {}
        self._reference_by_venue_order: dict[tuple[str, str], str] = {}
        self._identity_facts: dict[str, list[_IdentityFact]] = {}
        self._completion_facts: dict[str, _CompletionFact] = {}
        self._last_applied_ns: dict[str, int] = {}
        self._conflicts: dict[str, list[str]] = {}
        self._out_of_order_events_skipped = 0
        self._late_fill_refinements = 0
        self._submission_attempts_merged = 0

    # --- application ---

    def apply(self, event: EventEnvelope) -> None:
        if event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE:
            self._apply_attempt(event)
        elif event.event_type == ORDER_STATE_EVENT_TYPE:
            self._apply_state_change(event)
        elif event.event_type == EXECUTION_FILL_EVENT_TYPE:
            self._apply_fill(event)
        elif event.event_type == ORDER_VENUE_IDENTITY_EVENT_TYPE:
            self._apply_venue_identity(event)

    # --- the durable pre-send attempt (fact merge, never a lifecycle regression) ---

    def _apply_attempt(self, event: EventEnvelope) -> None:
        """Merge the durable attempt facts; never regress a lifecycle state already resolved."""
        if event.order_id is None:
            raise RecoveryError(
                "order.submission.attempted without an order_id cannot be reconstructed."
            )
        if not event.symbol:
            raise RecoveryError(
                "order.submission.attempted without a symbol cannot be reconstructed."
            )
        payload = event.payload
        reference = str(payload.venue_reference)
        anchors = AttemptAnchors(
            order_reference=reference,
            order_id=event.order_id,
            intent_id=payload.intent_id,
            correlation_id=event.correlation_id,
            symbol=event.symbol,
            strategy_id=event.strategy_id,
            side=payload.side,
            order_type=payload.order_type,
            quantity=payload.quantity,
            limit_price=payload.limit_price,
            time_in_force=payload.time_in_force,
            venue_producer=str(payload.venue_producer),
            occurred_at_ns=_to_ns(event.occurred_at),
        )
        known_reference = self._reference_by_order_id.get(event.order_id)
        if known_reference is not None and known_reference != reference:
            self._record_conflict(reference, "order_identity_reused_by_a_different_reference")
            return
        recorded = self._attempts.get(reference)
        if recorded is not None and recorded.identity_key() != anchors.identity_key():
            self._record_conflict(reference, "immutable_economic_identity_mismatch")
            return
        if recorded is None:
            self._attempts[reference] = anchors
            self._reference_by_order_id[event.order_id] = reference
            self._submission_attempts_merged += 1
            recorded = anchors
        if anchors.venue_producer not in _RECOGNIZED_VENUE_PRODUCERS:
            self._record_conflict(reference, "unrecognized_venue_producer")

        state = self._states.get(reference)
        if state is None:
            legacy_key = str(event.order_id)
            candidate = self._states.pop(legacy_key, None)
            if candidate is not None and candidate.order_id != event.order_id:
                self._states[legacy_key] = candidate
                self._record_conflict(reference, "attempt_reference_does_not_match_the_order")
                return
            state = candidate
            completion = self._completion_facts.pop(legacy_key, None)
            if completion is not None:
                self._completion_facts[reference] = completion
            if legacy_key in self._last_applied_ns:
                self._last_applied_ns[reference] = self._last_applied_ns.pop(legacy_key)

        if state is None:
            self._states[reference] = LocalOrderState(
                order_id=event.order_id,
                order_reference=reference,
                intent_id=payload.intent_id,
                correlation_id=event.correlation_id,
                symbol=event.symbol,
                state=_SUBMITTED,
                filled_quantity=_ZERO,
                remaining_quantity=payload.quantity,
                admitted_locally=True,
                submission_attempted=True,
            )
            self._last_applied_ns[reference] = anchors.occurred_at_ns
            self._bind_held_identities(reference)
            return

        self._states[reference] = replace(
            state,
            order_reference=reference,
            intent_id=state.intent_id or payload.intent_id,
            correlation_id=state.correlation_id or event.correlation_id,
            symbol=state.symbol or event.symbol,
            submission_attempted=True,
        )
        completion = self._completion_facts.get(reference)
        if state.state is _FILLED and completion is not None:
            self._validate_completion_fact(reference, completion, recorded)
        self._bind_held_identities(reference)

    # --- our venue order identity (review G-4) ---

    def _apply_venue_identity(self, event: EventEnvelope) -> None:
        payload = event.payload
        reference = str(payload.venue_reference)
        fact = _IdentityFact(
            order_id=event.order_id,
            intent_id=payload.intent_id,
            symbol=event.symbol,
            producer=event.producer,
            venue_order_id=str(payload.venue_order_id),
        )
        if reference not in self._attempts:
            # Replay runs in time order and the venue stamps its answer, so the attempt may follow.
            self._identity_facts.setdefault(reference, []).append(fact)
            return
        self._bind_venue_identity(reference, fact)

    def _bind_held_identities(self, reference: str) -> None:
        for fact in self._identity_facts.pop(reference, []):
            self._bind_venue_identity(reference, fact)

    def _bind_venue_identity(self, reference: str, fact: _IdentityFact) -> None:
        """Bind our venue order id to the attempt it answered; a contradiction is a conflict."""
        anchors = self._attempts[reference]
        state = self._states.get(reference)
        if state is None:
            self._record_conflict(reference, "venue_identity_without_a_matching_attempt")
            return
        if (anchors.order_id, anchors.intent_id, anchors.symbol) != (
            fact.order_id, fact.intent_id, fact.symbol
        ):
            self._record_conflict(reference, "venue_identity_bound_to_a_different_attempt")
            return
        if (
            anchors.venue_producer not in _RECOGNIZED_VENUE_PRODUCERS
            or fact.producer != anchors.venue_producer
        ):
            self._record_conflict(reference, "venue_identity_requires_the_attempt_venue_producer")
            return
        venue_order_id = fact.venue_order_id
        if state.venue_order_id is not None:
            if state.venue_order_id != venue_order_id:
                self._record_conflict(reference, "conflicting_venue_order_identity")
            return
        key = (anchors.symbol, venue_order_id)
        claimed_by = self._reference_by_venue_order.get(key)
        if claimed_by is not None:
            # One venue order cannot be two of our attempts: neither binding can be trusted.
            self._record_conflict(claimed_by, "venue_order_identity_claimed_by_another_attempt")
            self._record_conflict(reference, "venue_order_identity_claimed_by_another_attempt")
            return
        self._reference_by_venue_order[key] = reference
        self._states[reference] = replace(state, venue_order_id=venue_order_id)

    def _validate_completion_fact(
        self, reference: str, completion: _CompletionFact, anchors: AttemptAnchors
    ) -> None:
        if (
            anchors.venue_producer not in _RECOGNIZED_VENUE_PRODUCERS
            or completion.producer != anchors.venue_producer
        ):
            self._record_conflict(
                reference, "filled_completion_requires_authoritative_venue_evidence"
            )
            return
        if (
            completion.kind == "fill"
            and completion.filled_quantity is not None
            and completion.filled_quantity != anchors.quantity
        ):
            self._record_conflict(
                reference, "filled_completion_quantity_contradicts_original_quantity"
            )

    def _apply_state_change(self, event: EventEnvelope) -> None:
        reference = self._reference_for(event, "order.state.changed")
        incoming = OrderStateName(event.payload.state)
        anchors = self._attempts.get(reference)
        provenance = self._provenance(event, anchors)
        occurred_at_ns = _to_ns(event.occurred_at)
        current = self._states.get(reference)

        if current is None:
            self._states[reference] = LocalOrderState(
                order_id=event.order_id,
                order_reference=reference,
                intent_id=None,
                correlation_id=event.correlation_id,
                symbol=event.symbol or "",
                state=incoming,
                filled_quantity=_ZERO,
                remaining_quantity=_ZERO,
                admitted_locally=True,
                submission_attempted=incoming.rank >= _SUBMISSION_ATTEMPT_RANK,
            )
            if incoming is _FILLED and anchors is None:
                self._completion_facts[reference] = _CompletionFact(
                    producer=event.producer, kind="lifecycle"
                )
            self._last_applied_ns[reference] = occurred_at_ns
            return

        if anchors is not None and event.symbol and anchors.symbol != event.symbol:
            self._record_conflict(reference, "immutable_economic_identity_mismatch")
            return

        if (
            incoming is _FILLED
            and current.state not in _TERMINAL_STATES
            and anchors is not None
            and provenance is not Provenance.AUTHORITATIVE_VENUE_LIFECYCLE
        ):
            self._record_conflict(
                reference, "filled_completion_requires_authoritative_venue_evidence"
            )
            return

        decision = self._decide_state_transition(
            current,
            incoming,
            occurred_at_ns,
            self._last_applied_ns.get(reference, occurred_at_ns),
        )
        if decision is _Decision.CONFLICT_KEEP_FENCED:
            self._record_conflict(
                reference, _state_conflict_reason(current.state, incoming, provenance)
            )
            return
        if decision is _Decision.IGNORE_AS_STALE:
            self._out_of_order_events_skipped += 1
            return
        if decision is _Decision.IDEMPOTENT_NO_OP:
            return

        self._states[reference] = replace(
            current,
            state=incoming,
            correlation_id=current.correlation_id or event.correlation_id,
            symbol=current.symbol or (event.symbol or ""),
            submission_attempted=(
                current.submission_attempted or incoming.rank >= _SUBMISSION_ATTEMPT_RANK
            ),
        )
        if incoming is _FILLED and anchors is None:
            self._completion_facts[reference] = _CompletionFact(
                producer=event.producer, kind="lifecycle"
            )
        self._last_applied_ns[reference] = max(
            self._last_applied_ns.get(reference, 0), occurred_at_ns
        )

    def _apply_fill(self, event: EventEnvelope) -> None:
        reference = self._reference_for(event, "execution.fill.recorded")
        payload = event.payload
        anchors = self._attempts.get(reference)
        provenance = self._provenance(event, anchors)
        occurred_at_ns = _to_ns(event.occurred_at)
        current = self._states.get(reference)
        if current is None:
            raise ReconciliationError(
                f"fill recorded for order {reference} with no lifecycle record; refusing to guess."
            )
        original = None if anchors is None else anchors.quantity
        filled = current.filled_quantity + payload.quantity
        if original is not None and filled > original:
            self._record_conflict(reference, "executed_quantity_exceeds_original_quantity")
            return
        if (
            payload.remaining_quantity is not None
            and current.remaining_quantity > 0
            and payload.remaining_quantity > current.remaining_quantity
        ):
            self._record_conflict(reference, "executed_quantity_decreased")
            return
        remaining = payload.remaining_quantity
        explicit_remaining = remaining is not None
        if remaining is None:
            remaining = max(_ZERO, current.remaining_quantity - payload.quantity)

        if current.state in _TERMINAL_STATES:
            self._apply_fill_to_terminal(
                reference,
                current,
                payload,
                provenance,
                original,
                filled,
                remaining,
                occurred_at_ns,
            )
            return

        state = current.state
        if remaining == 0:
            if anchors is None:
                if explicit_remaining:
                    state = _FILLED
                elif state.rank < STATE_RANK["partially_filled"]:
                    state = _PARTIALLY_FILLED
            elif (
                explicit_remaining
                and provenance is Provenance.AUTHORITATIVE_VENUE_FILL
                and original is not None
                and filled == original
            ):
                state = _FILLED
            elif state.rank < STATE_RANK["partially_filled"]:
                state = _PARTIALLY_FILLED
        elif state.rank < STATE_RANK["partially_filled"]:
            state = _PARTIALLY_FILLED

        self._states[reference] = replace(
            current,
            state=state,
            filled_quantity=filled,
            remaining_quantity=remaining,
        )
        if state is _FILLED and anchors is None:
            self._completion_facts[reference] = _CompletionFact(
                producer=event.producer,
                kind="fill",
                filled_quantity=filled,
                remaining_quantity=remaining,
            )
        self._last_applied_ns[reference] = max(
            self._last_applied_ns.get(reference, 0), occurred_at_ns
        )

    def _apply_fill_to_terminal(
        self,
        reference: str,
        current: LocalOrderState,
        payload: Any,
        provenance: Provenance,
        original: Decimal | None,
        filled: Decimal,
        remaining: Decimal,
        occurred_at_ns: int,
    ) -> None:
        """Terminal-finality rules for fills; a contradiction is never silently dropped.

        A historical fill is quantity evidence, not proof of the CURRENT venue lifecycle state.
        Therefore a fill never performs the terminal -> FILLED lifecycle refinement: that
        refinement requires an authoritative current venue state report (an authoritative
        ``order.state.changed{filled}`` event here, or the recovery/reconciliation engine adopting
        a venue report for the order).
        """
        if not provenance.is_authoritative_venue:
            self._record_conflict(reference, "non_authoritative_fill_on_terminal_order")
            return
        if current.state is OrderStateName.REJECTED:
            # A rejected order was never executed; a historical fill cannot prove the current
            # venue state, so the contradiction stays fenced.
            self._record_conflict(reference, "rejected_fill_requires_authoritative_current_state")
            return
        if original is None:
            # A terminal refinement must stay auditable from durable facts alone.
            self._record_conflict(reference, "terminal_refinement_without_original_quantity")
            return
        if payload.remaining_quantity is None:
            # Without the reported remaining quantity the previous executed quantity could not be
            # derived from durable facts alone, so the late fill would not be auditable.
            self._record_conflict(reference, "late_fill_without_remaining_quantity_evidence")
            return
        if filled + remaining != original:
            self._record_conflict(reference, "inconsistent_terminal_fill_quantities")
            return
        # FILLED / CANCELED / EXPIRED may all receive authoritative historical fill details after
        # the lifecycle fact itself. Keep the authoritative terminal lifecycle unchanged while the
        # quantity detail advances monotonically. In particular, a FILLED state may temporarily
        # carry a non-zero remaining quantity while an atomic backfill batch is replayed; the final
        # replay proof must still reach the venue's executed quantity exactly.
        self._states[reference] = replace(
            current, filled_quantity=filled, remaining_quantity=remaining
        )
        self._late_fill_refinements += 1
        self._last_applied_ns[reference] = max(
            self._last_applied_ns.get(reference, 0), occurred_at_ns
        )

    # --- resolution rules ---

    def _reference_for(self, event: EventEnvelope, label: str) -> str:
        if event.order_id is None:
            raise RecoveryError(f"{label} without an order_id cannot be reconstructed.")
        return self._reference_by_order_id.get(event.order_id, str(event.order_id))

    @staticmethod
    def _provenance(event: EventEnvelope, anchors: AttemptAnchors | None) -> Provenance:
        """Explicit provenance: the durable producer anchor decides, never the timestamp."""
        if anchors is None:
            return Provenance.NO_ATTEMPT_RECORDED
        if (
            anchors.venue_producer in _RECOGNIZED_VENUE_PRODUCERS
            and event.producer == anchors.venue_producer
        ):
            if event.event_type == EXECUTION_FILL_EVENT_TYPE:
                return Provenance.AUTHORITATIVE_VENUE_FILL
            return Provenance.AUTHORITATIVE_VENUE_LIFECYCLE
        return Provenance.LOCAL_LIFECYCLE

    @staticmethod
    def _decide_state_transition(
        current: LocalOrderState,
        incoming: OrderStateName,
        occurred_at_ns: int,
        last_applied_ns: int,
    ) -> _Decision:
        if current.state is incoming:
            # An older redundant fact is stale; an equal/newer identical duplicate is a pure
            # idempotent no-op and must not be counted as out-of-order.
            if occurred_at_ns < last_applied_ns:
                return _Decision.IGNORE_AS_STALE
            return _Decision.IDEMPOTENT_NO_OP
        stale = occurred_at_ns < last_applied_ns
        if current.state in _TERMINAL_STATES:
            if incoming not in _TERMINAL_STATES:
                # A terminal order never silently becomes open again.
                return _Decision.CONFLICT_KEEP_FENCED
            # Terminal -> terminal (INCLUDING terminal -> FILLED): a historical lifecycle fact is
            # never proof of CURRENT venue state, so it can never refine a terminal order. The only
            # terminal -> FILLED release path is current reconciliation through VenueOrderReport.
            return _Decision.IGNORE_AS_STALE if stale else _Decision.CONFLICT_KEEP_FENCED
        if incoming in _TERMINAL_STATES:
            return _Decision.APPLY
        if incoming.rank < current.state.rank:
            # A lower lifecycle rank arriving after a higher one is order/clock skew when it is
            # older, and a genuine contradiction when it is newer.
            return _Decision.IGNORE_AS_STALE if stale else _Decision.CONFLICT_KEEP_FENCED
        return _Decision.APPLY

    def _record_conflict(self, reference: str, reason_code: str) -> None:
        reasons = self._conflicts.setdefault(reference, [])
        if reason_code not in reasons:
            reasons.append(reason_code)

    # --- reads ---

    def states(self) -> tuple[LocalOrderState, ...]:
        return tuple(self._states[reference] for reference in sorted(self._states))

    def state_for(self, order_reference: str) -> LocalOrderState | None:
        return self._states.get(order_reference)

    def open_states(self) -> tuple[LocalOrderState, ...]:
        return tuple(state for state in self.states() if state.is_open)

    def attempt_anchors(self, order_reference: str) -> AttemptAnchors | None:
        """The durable attempt anchors for one reference, when an attempt was journaled."""
        return self._attempts.get(order_reference)

    def attempt_anchor_map(self) -> dict[str, AttemptAnchors]:
        """Every durable attempt anchor, keyed by the venue-facing reference."""
        return dict(self._attempts)

    def conflicts(self) -> dict[str, tuple[str, ...]]:
        """Lifecycle conflicts by reference: authoritative contradictions, never dropped."""
        conflicts = {reference: list(reasons) for reference, reasons in self._conflicts.items()}
        for reference in self._identity_facts:
            # A venue identity whose attempt never arrived names no attempt of ours.
            reasons = conflicts.setdefault(reference, [])
            if "venue_identity_without_a_matching_attempt" not in reasons:
                reasons.append("venue_identity_without_a_matching_attempt")
        return {reference: tuple(reasons) for reference, reasons in sorted(conflicts.items())}

    def conflicted_references(self) -> tuple[str, ...]:
        return tuple(self.conflicts())

    @property
    def out_of_order_events_skipped(self) -> int:
        return self._out_of_order_events_skipped

    @property
    def late_fill_refinements(self) -> int:
        return self._late_fill_refinements

    @property
    def submission_attempts_merged(self) -> int:
        return self._submission_attempts_merged

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
        # Additive keys: legacy snapshots simply do not carry them.
        self._attempts = {
            entry["order_reference"]: _attempt_from_dict(entry)
            for entry in state.get("attempts", [])
        }
        self._completion_facts = {
            entry["order_reference"]: _completion_from_dict(entry)
            for entry in state.get("completion_facts", [])
        }
        self._reference_by_order_id = {
            anchors.order_id: reference for reference, anchors in self._attempts.items()
        }
        self._reference_by_venue_order = {
            (self._attempts[reference].symbol, order.venue_order_id): reference
            for reference, order in self._states.items()
            if order.venue_order_id is not None and reference in self._attempts
        }
        self._identity_facts = {}
        for entry in state.get("venue_identity_facts", []):
            self._identity_facts.setdefault(str(entry["order_reference"]), []).append(
                _identity_fact_from_dict(entry)
            )
        bookkeeping = state.get("bookkeeping", {})
        self._last_applied_ns = {
            str(reference): int(value)
            for reference, value in dict(bookkeeping.get("last_applied_ns", {})).items()
        }
        self._conflicts = {
            str(reference): [str(reason) for reason in reasons]
            for reference, reasons in dict(bookkeeping.get("conflicts", {})).items()
        }
        for reference, anchors in self._attempts.items():
            if anchors.venue_producer not in _RECOGNIZED_VENUE_PRODUCERS:
                self._record_conflict(reference, "unrecognized_venue_producer")
            completion = self._completion_facts.get(reference)
            if completion is not None:
                self._validate_completion_fact(reference, completion, anchors)
        self._out_of_order_events_skipped = int(bookkeeping.get("out_of_order_events_skipped", 0))
        self._late_fill_refinements = int(bookkeeping.get("late_fill_refinements", 0))
        self._submission_attempts_merged = int(
            bookkeeping.get("submission_attempts_merged", len(self._attempts))
        )
        self.seed_applied({UUID(value) for value in state.get("applied_event_ids", [])})

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "reconstruction_schema": RECONSTRUCTION_SNAPSHOT_SCHEMA,
            "orders": [_local_state_to_dict(state) for state in self.states()],
            "attempts": [
                _attempt_to_dict(self._attempts[reference]) for reference in sorted(self._attempts)
            ],
            "completion_facts": [
                _completion_to_dict(reference, self._completion_facts[reference])
                for reference in sorted(self._completion_facts)
            ],
            "venue_identity_facts": [
                _identity_fact_to_dict(reference, fact)
                for reference in sorted(self._identity_facts)
                for fact in self._identity_facts[reference]
            ],
            "bookkeeping": {
                "last_applied_ns": {
                    reference: self._last_applied_ns[reference]
                    for reference in sorted(self._last_applied_ns)
                },
                "conflicts": {
                    reference: list(self._conflicts[reference])
                    for reference in sorted(self._conflicts)
                },
                "out_of_order_events_skipped": self._out_of_order_events_skipped,
                "late_fill_refinements": self._late_fill_refinements,
                "submission_attempts_merged": self._submission_attempts_merged,
            },
            "applied_event_ids": [str(event_id) for event_id in self.applied_events],
        }


def reconstruct_order_states(
    journal: EventJournal,
    *,
    snapshot: SnapshotRecord | None = None,
) -> tuple[OrderStateReconstructor, RebuildSummary]:
    """Restore a snapshot (if any), replay the journal tail, and return the reconstructed state."""
    reconstructor = OrderStateReconstructor()
    effective_snapshot = snapshot
    legacy_state: dict[str, Any] | None = None
    if snapshot is not None:
        raw_schema = snapshot.state.get("reconstruction_schema")
        if type(raw_schema) is not int or raw_schema not in {1, RECONSTRUCTION_SNAPSHOT_SCHEMA}:
            raise RecoveryError(
                f"reconstruction snapshot schema {raw_schema!r} is not supported "
                f"(expected 1 or {RECONSTRUCTION_SNAPSHOT_SCHEMA})."
            )
        has_derived_state = bool(snapshot.state.get("orders") or snapshot.state.get("attempts"))
        if snapshot.last_event_id is None and has_derived_state:
            raise RecoveryError(
                "a reconstruction snapshot with derived order state must carry a journal watermark."
            )
        if raw_schema == 1:
            legacy_state = dict(snapshot.state)
            effective_snapshot = None

    summary = rebuild_state(journal, reconstructor, snapshot=effective_snapshot)

    if legacy_state is not None:
        revision_ids = set(journal.revision().event_ids)
        if snapshot is None or snapshot.last_event_id is None or snapshot.last_event_id not in revision_ids:
            raise RecoveryError(
                "schema-1 reconstruction snapshot cannot be migrated because its journal "
                "watermark is missing from the current journal."
            )
        try:
            snapshot_applied = {
                UUID(str(event_id)) for event_id in legacy_state.get("applied_event_ids", [])
            }
            snapshot_order_ids = {
                UUID(str(entry["order_id"]))
                for section in ("orders", "attempts")
                for entry in legacy_state.get(section, [])
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise RecoveryError("schema-1 reconstruction snapshot metadata is malformed.") from exc
        if not snapshot_applied.issubset(revision_ids):
            raise RecoveryError(
                "schema-1 reconstruction snapshot references events absent from the current journal."
            )
        rebuilt_order_ids = {state.order_id for state in reconstructor.states()}
        if not snapshot_order_ids.issubset(rebuilt_order_ids):
            raise RecoveryError(
                "schema-1 reconstruction snapshot references orders absent from the rebuilt journal."
            )
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
        "venue_order_id": state.venue_order_id,
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
        # Additive key (review G-4): a legacy snapshot leaves our venue identity unknown.
        venue_order_id=(
            None if entry.get("venue_order_id") is None else str(entry["venue_order_id"])
        ),
    )


def _state_conflict_reason(
    current: OrderStateName, incoming: OrderStateName, provenance: Provenance
) -> str:
    """A precise, deterministic reason code for one refused lifecycle transition."""
    if current in _TERMINAL_STATES and incoming not in _TERMINAL_STATES:
        return "terminal_to_non_terminal_lifecycle_conflict"
    if current is _FILLED:
        return "filled_is_absorbing"
    if incoming is _FILLED:
        return (
            "filled_refinement_requires_authoritative_venue_evidence"
            if provenance is not Provenance.AUTHORITATIVE_VENUE_LIFECYCLE
            else "historical_filled_event_cannot_refine_a_terminal_order"
        )
    if current in _TERMINAL_STATES and incoming in _TERMINAL_STATES:
        return "mutually_exclusive_terminal_states"
    return "lifecycle_state_regressed_without_stale_evidence"


def _completion_to_dict(reference: str, completion: _CompletionFact) -> dict[str, Any]:
    return {
        "order_reference": reference,
        "producer": completion.producer,
        "kind": completion.kind,
        "filled_quantity": (
            None if completion.filled_quantity is None else str(completion.filled_quantity)
        ),
        "remaining_quantity": (
            None if completion.remaining_quantity is None else str(completion.remaining_quantity)
        ),
    }


def _completion_from_dict(entry: dict[str, Any]) -> _CompletionFact:
    kind = str(entry["kind"])
    if kind not in {"lifecycle", "fill"}:
        raise RecoveryError(f"unsupported completion fact kind {kind!r}.")
    return _CompletionFact(
        producer=str(entry["producer"]),
        kind=kind,
        filled_quantity=(
            None if entry.get("filled_quantity") is None else Decimal(str(entry["filled_quantity"]))
        ),
        remaining_quantity=(
            None
            if entry.get("remaining_quantity") is None
            else Decimal(str(entry["remaining_quantity"]))
        ),
    )


def _identity_fact_to_dict(reference: str, fact: _IdentityFact) -> dict[str, Any]:
    return {
        "order_reference": reference,
        "order_id": None if fact.order_id is None else str(fact.order_id),
        "intent_id": str(fact.intent_id),
        "symbol": fact.symbol,
        "producer": fact.producer,
        "venue_order_id": fact.venue_order_id,
    }


def _identity_fact_from_dict(entry: dict[str, Any]) -> _IdentityFact:
    return _IdentityFact(
        order_id=None if entry.get("order_id") is None else UUID(entry["order_id"]),
        intent_id=UUID(entry["intent_id"]),
        symbol=None if entry.get("symbol") is None else str(entry["symbol"]),
        producer=str(entry["producer"]),
        venue_order_id=str(entry["venue_order_id"]),
    )


def _attempt_to_dict(anchors: AttemptAnchors) -> dict[str, Any]:
    return {
        "order_reference": anchors.order_reference,
        "order_id": str(anchors.order_id),
        "intent_id": str(anchors.intent_id),
        "correlation_id": None if anchors.correlation_id is None else str(anchors.correlation_id),
        "symbol": anchors.symbol,
        "strategy_id": anchors.strategy_id,
        "side": anchors.side,
        "order_type": anchors.order_type,
        "quantity": str(anchors.quantity),
        "limit_price": None if anchors.limit_price is None else str(anchors.limit_price),
        "time_in_force": anchors.time_in_force,
        "venue_producer": anchors.venue_producer,
        "occurred_at_ns": anchors.occurred_at_ns,
    }


def _attempt_from_dict(entry: dict[str, Any]) -> AttemptAnchors:
    return AttemptAnchors(
        order_reference=str(entry["order_reference"]),
        order_id=UUID(entry["order_id"]),
        intent_id=UUID(entry["intent_id"]),
        correlation_id=(
            None if entry.get("correlation_id") is None else UUID(entry["correlation_id"])
        ),
        symbol=str(entry["symbol"]),
        strategy_id=None if entry.get("strategy_id") is None else str(entry["strategy_id"]),
        side=str(entry["side"]),
        order_type=str(entry["order_type"]),
        quantity=Decimal(str(entry["quantity"])),
        limit_price=None if entry.get("limit_price") is None else Decimal(str(entry["limit_price"])),
        time_in_force=str(entry["time_in_force"]),
        venue_producer=str(entry["venue_producer"]),
        occurred_at_ns=int(entry["occurred_at_ns"]),
    )


def validate_venue_evidence(
    anchors: AttemptAnchors | None, report: Any
) -> str | None:
    """Validate one CURRENT venue report against the durable attempt anchors.

    Returns ``None`` when the report is usable as reconciliation evidence, or a deterministic
    reason code when it must not be handed to the reconciliation engine.

    The only terminal -> FILLED release path is a CURRENT authoritative venue report, and the
    approved rule requires that report to prove, against the durable original-quantity anchor:

    * ``state == filled``;
    * ``executedQty == Q``;
    * ``origQty == Q`` (i.e. executed + remaining == Q, and nothing remains open).

    A report that claims FILLED while contradicting those quantities is refused, so the order
    stays unresolved and new risk stays fenced. Orders without a durable attempt anchor keep the
    pre-existing reconciliation behaviour (there is no durable Q to contradict).
    """
    if anchors is None or report.state is not _FILLED:
        return None
    if report.filled_quantity != anchors.quantity:
        return "venue_filled_report_executed_quantity_contradicts_original_quantity"
    if report.remaining_quantity != 0:
        return "venue_filled_report_inconsistent_remaining_quantity"
    if report.filled_quantity + report.remaining_quantity != anchors.quantity:
        return "venue_filled_report_original_quantity_mismatch"
    return None
