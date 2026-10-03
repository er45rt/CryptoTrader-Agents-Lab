"""Bounded, serialized ingress for production trade intents.

Producer threads may enqueue only. Exactly one owner thread -- the thread that constructed the
ProductionRuntime and its SQLite journal -- drains the queue with run_once(). Queued intents are
not economic reservations; Hard Risk creates those only after an intent leaves this queue.

The replay/high-water ledger here is process-local. For durable venue-writing runtimes, an exact
intent_id is also protected across restart by the deterministic client_order_id and durable
submission journal. Re-emitting the same decision under a new intent_id needs durable decision
identity before Mainnet promotion; this controller does not pretend otherwise.

Capacity-rejected brand-new scopes are guarded by bounded strategy/global rejection floors. That
is deliberately conservative: a global overflow floor may temporarily block unrelated new scopes,
and a capacity-rejected decision must be replaced by newer evidence rather than resubmitted.
"""

from __future__ import annotations

import heapq
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from crypto_trader.execution import ExecutionRuntimeState
from crypto_trader.validity import BoundedPendingIntents

from .errors import ProductionStateError
from .runtime import (
    ProductionExecutionOutcome,
    ProductionIntent,
    ProductionRuntime,
    intent_deadline,
    validity_request_for,
)

__all__ = ["ProductionAdmissionController"]


@dataclass(frozen=True)
class _RetiredIntent:
    intent_id: UUID
    strategy_id: str
    decision_scope: str
    order_key: tuple[datetime, datetime]
    expires_at: datetime


@dataclass(frozen=True)
class _RejectionFloor:
    order_key: tuple[datetime, datetime]
    expires_at: datetime


class ProductionAdmissionController:
    """Thread-safe bounded ingress with one serialized runtime consumer.

    offer is deliberately cheap and side-effect free with respect to the journal, venue and
    Hard Risk. run_once must run on the ProductionRuntime owner thread and processes at most one
    queued intent.
    """

    def __init__(
        self,
        *,
        runtime: ProductionRuntime,
        max_size: int,
        max_per_strategy_queued: int,
        max_retired: int,
        max_per_strategy_retired: int,
        max_rejection_strategies: int,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(runtime, ProductionRuntime):
            raise ProductionStateError("production admission requires a ProductionRuntime.")
        runtime._require_owner_thread()
        if (
            not isinstance(max_size, int)
            or isinstance(max_size, bool)
            or max_size < 2
        ):
            raise ProductionStateError(
                "admission max_size must be an integer >= 2 for multi-strategy ingress."
            )
        if (
            not isinstance(max_per_strategy_queued, int)
            or isinstance(max_per_strategy_queued, bool)
            or max_per_strategy_queued <= 0
            or max_per_strategy_queued >= max_size
        ):
            raise ProductionStateError(
                "max_per_strategy_queued must be a positive integer smaller than max_size."
            )
        if (
            not isinstance(max_retired, int)
            or isinstance(max_retired, bool)
            or max_retired <= max_size
        ):
            raise ProductionStateError(
                "max_retired must be an integer larger than max_size."
            )
        if (
            not isinstance(max_per_strategy_retired, int)
            or isinstance(max_per_strategy_retired, bool)
            or max_per_strategy_retired <= max_per_strategy_queued
            or max_per_strategy_retired >= max_retired
        ):
            raise ProductionStateError(
                "max_per_strategy_retired must be larger than the per-strategy queue cap "
                "and smaller than max_retired."
            )
        if (
            not isinstance(max_rejection_strategies, int)
            or isinstance(max_rejection_strategies, bool)
            or max_rejection_strategies <= 0
            or max_rejection_strategies > max_retired
        ):
            raise ProductionStateError(
                "max_rejection_strategies must be a positive integer no larger than max_retired."
            )
        if not callable(clock):
            raise ProductionStateError("production admission requires an explicit clock.")

        self._runtime = runtime
        self._policy = runtime.validity_policy
        self._queue = BoundedPendingIntents(max_size=max_size)
        self._max_per_strategy_queued = max_per_strategy_queued
        self._max_retired = max_retired
        self._max_per_strategy_retired = max_per_strategy_retired
        self._max_rejection_strategies = max_rejection_strategies
        self._clock = clock
        self._intents: dict[UUID, ProductionIntent] = {}
        self._retired: dict[UUID, _RetiredIntent] = {}
        self._retired_heap: list[tuple[datetime, str, UUID]] = []
        self._scope_high_water: dict[tuple[str, str], UUID] = {}
        self._strategy_rejection_floor: dict[str, _RejectionFloor] = {}
        self._global_rejection_floor: _RejectionFloor | None = None
        self._strategy_queue_counts: dict[str, int] = {}
        self._strategy_replay_counts: dict[str, int] = {}
        self._active_intent_id: UUID | None = None
        self._last_now: datetime | None = None
        self._lock = threading.Lock()
        self._runtime_token = runtime._bind_admission()

    @property
    def max_size(self) -> int:
        return self._queue.max_size

    @property
    def max_per_strategy_queued(self) -> int:
        return self._max_per_strategy_queued

    @property
    def max_retired(self) -> int:
        return self._max_retired

    @property
    def max_per_strategy_retired(self) -> int:
        return self._max_per_strategy_retired

    @property
    def max_rejection_strategies(self) -> int:
        return self._max_rejection_strategies

    def offer(self, intent: ProductionIntent) -> tuple[bool, str]:
        """Offer one intent from any producer thread without touching runtime I/O."""
        if not isinstance(intent, ProductionIntent):
            raise ProductionStateError("production admission offer expects a ProductionIntent.")
        request = validity_request_for(intent)

        with self._lock:
            now = self._now()
            self._evict_locked(now)
            time_rejection = self._time_rejection_reason(intent, now)
            if time_rejection is not None:
                return False, time_rejection
            if (
                intent.intent_id in self._intents
                or intent.intent_id == self._active_intent_id
                or intent.intent_id in self._retired
            ):
                return False, "duplicate_intent_id"

            incoming_key = self._decision_order_key(intent)
            scope_key = self._scope_key(intent)
            high_water_id = self._scope_high_water.get(scope_key)
            if high_water_id is not None:
                high_water = self._retired.get(high_water_id)
                if high_water is None:
                    raise ProductionStateError(
                        "admission scope high-water points to a missing replay record."
                    )
                if incoming_key < high_water.order_key:
                    return False, "superseded_by_scope_high_water"
                if incoming_key == high_water.order_key:
                    return False, "ambiguous_same_scope_decision"
            # At most one queued decision exists per strategy+scope. If two different IDs carry
            # identical decision evidence, refuse the newcomer instead of using UUID ordering.
            same_scope = [
                queued
                for queued in self._intents.values()
                if self._scope_key(queued) == scope_key
            ]
            if len(same_scope) > 1:
                raise ProductionStateError(
                    "admission queue contains multiple intents for one strategy decision scope."
                )
            replacements: list[ProductionIntent] = []
            if same_scope:
                queued = same_scope[0]
                queued_key = self._decision_order_key(queued)
                if queued_key > incoming_key:
                    return False, "superseded_by_newer_queued_intent"
                if queued_key == incoming_key:
                    self._convert_queued_to_retired_locked(queued)
                    return False, "ambiguous_same_scope_decision"
                replacements.append(queued)

            # Coarse capacity-rejection floors are only a fallback for scopes that have neither
            # an exact retired high-water nor a queued predecessor. Existing scopes keep their
            # precise same-scope ordering semantics and must not be masked by an unrelated floor.
            if high_water_id is None and not same_scope:
                strategy_floor = self._strategy_rejection_floor.get(intent.strategy_id)
                if strategy_floor is not None and incoming_key <= strategy_floor.order_key:
                    return False, "superseded_by_capacity_rejection_floor"
                global_floor = self._global_rejection_floor
                if global_floor is not None and incoming_key <= global_floor.order_key:
                    return False, "superseded_by_global_capacity_rejection_floor"

            # Every accepted unique intent reserves one replay-ledger slot now. Replacing an older
            # queued intent converts that older slot to retired and still needs one additional slot
            # for the new unique intent, so all capacity checks happen before any mutation.
            replay_depth = len(self._retired) + len(self._queue)
            if replay_depth >= self._max_retired:
                return self._capacity_rejection_locked(
                    replacements, intent, "replay_capacity_exhausted"
                )

            strategy_replay_depth = self._strategy_replay_counts.get(
                intent.strategy_id, 0
            )
            if strategy_replay_depth >= self._max_per_strategy_retired:
                return self._capacity_rejection_locked(
                    replacements, intent, "strategy_replay_capacity_exhausted"
                )

            effective_queue_depth = len(self._queue) - len(replacements)
            if effective_queue_depth >= self._queue.max_size:
                return self._capacity_rejection_locked(
                    replacements, intent, "bounded_capacity_exhausted"
                )

            strategy_depth = self._strategy_queue_counts.get(
                intent.strategy_id, 0
            ) - len(replacements)
            if strategy_depth >= self._max_per_strategy_queued:
                return self._capacity_rejection_locked(
                    replacements, intent, "strategy_queue_capacity_exhausted"
                )

            for queued in replacements:
                self._convert_queued_to_retired_locked(queued)

            accepted, reason = self._queue.offer(request, enqueued_at=now)
            if not accepted:
                raise ProductionStateError(
                    f"admission queue rejected a pre-authorized insertion ({reason})."
                )
            self._intents[intent.intent_id] = intent
            self._increment_strategy_queue_locked(intent.strategy_id)
            self._increment_strategy_replay_locked(intent.strategy_id)
            self._check_capacity_bounds_locked(intent.strategy_id)
            return True, "accepted"

    def run_once(self) -> ProductionExecutionOutcome | None:
        """Process at most one queued intent on the runtime owner thread."""
        self._runtime._require_owner_thread()
        if self._runtime.state() is not ExecutionRuntimeState.RUNNING:
            raise ProductionStateError(
                "production admission cannot dequeue while the runtime is not running."
            )

        with self._lock:
            now = self._now()
            if self._active_intent_id is not None:
                raise ProductionStateError(
                    "production admission already has an active intent; recursive run_once refused."
                )
            self._evict_locked(now)
            snapshot = self._queue.snapshot()
            if not snapshot:
                return None

            head = snapshot[0].request
            intent = self._intents.get(head.intent_id)
            if intent is None:
                raise ProductionStateError(
                    "admission queue lost its ProductionIntent payload; refusing to continue."
                )
            self._convert_queued_to_retired_locked(intent)
            self._active_intent_id = intent.intent_id

        try:
            return self._runtime.execute_intent(
                intent,
                capacity_available=True,
                latest_scope_intent_id=intent.intent_id,
                _admission_token=self._runtime_token,
            )
        except Exception as exc:
            exc.add_note(
                f"production admission retired intent_id={intent.intent_id}; "
                "the intent was not automatically re-queued"
            )
            raise
        finally:
            with self._lock:
                self._active_intent_id = None

    def depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def queued_intent_ids(self) -> tuple[UUID, ...]:
        with self._lock:
            return self._queue.intent_ids()

    def active_intent_id(self) -> UUID | None:
        with self._lock:
            return self._active_intent_id

    def _capacity_rejection_locked(
        self,
        replacements: list[ProductionIntent],
        incoming: ProductionIntent,
        reason: str,
    ) -> tuple[bool, str]:
        if replacements:
            if len(replacements) != 1:
                raise ProductionStateError(
                    "admission capacity rejection expected at most one same-scope replacement."
                )
            self._replace_queued_with_rejected_tombstone_locked(
                replacements[0],
                incoming,
            )
            return False, f"superseded_but_{reason}"

        # Once a scope already owns a retired high-water slot, a strictly newer decision that is
        # rejected only by capacity must still advance that evidence monotonically. Reuse the
        # same bounded slot so backpressure cannot later allow an intermediate stale decision.
        # Brand-new scopes have no replay slot to advance; record a hard-bounded strategy/global
        # rejection floor so newer observed evidence cannot disappear under backpressure.
        if self._scope_key(incoming) in self._scope_high_water:
            self._advance_scope_high_water_locked(incoming)
        else:
            self._record_capacity_rejection_floor_locked(incoming)
        return False, reason

    def _record_capacity_rejection_floor_locked(
        self,
        incoming: ProductionIntent,
    ) -> None:
        """Record capacity-rejected evidence when a scope has no precise replay slot.

        Strategy floors are the bounded primary fallback. If the strategy-floor map is already
        full, one global overflow floor conservatively protects every unrecorded strategy until
        the newest rejected evidence becomes stale.
        """
        if self._scope_key(incoming) in self._scope_high_water:
            raise ProductionStateError(
                "admission rejection floor cannot shadow an exact scope high-water."
            )
        if any(
            self._scope_key(queued) == self._scope_key(incoming)
            for queued in self._intents.values()
        ):
            raise ProductionStateError(
                "admission rejection floor cannot shadow a queued same-scope intent."
            )
        if (
            incoming.intent_id in self._intents
            or incoming.intent_id in self._retired
            or incoming.intent_id == self._active_intent_id
        ):
            raise ProductionStateError(
                "admission rejection floor incoming intent id is already known."
            )

        self._assert_replay_invariants_locked()
        incoming_floor = _RejectionFloor(
            order_key=self._decision_order_key(incoming),
            expires_at=self._replay_expiry(incoming),
        )
        strategy_id = incoming.strategy_id
        existing = self._strategy_rejection_floor.get(strategy_id)
        if existing is not None:
            if incoming_floor.order_key <= existing.order_key:
                raise ProductionStateError(
                    "admission strategy rejection floor did not advance monotonically."
                )
            self._strategy_rejection_floor[strategy_id] = incoming_floor
            self._assert_rejection_floor_invariants_locked()
            return

        if len(self._strategy_rejection_floor) < self._max_rejection_strategies:
            self._strategy_rejection_floor[strategy_id] = incoming_floor
            self._assert_rejection_floor_invariants_locked()
            return

        existing_global = self._global_rejection_floor
        if existing_global is None:
            self._global_rejection_floor = incoming_floor
        else:
            order_key = max(existing_global.order_key, incoming_floor.order_key)
            expires_at = max(existing_global.expires_at, incoming_floor.expires_at)
            self._global_rejection_floor = _RejectionFloor(
                order_key=order_key,
                expires_at=expires_at,
            )
        self._assert_rejection_floor_invariants_locked()

    def _advance_scope_high_water_locked(self, incoming: ProductionIntent) -> None:
        """Advance an existing scope guard in place without consuming replay capacity.

        The retired record keeps its identity-owning intent_id (including when that intent is
        active), while order_key/expires_at advance to the newest capacity-rejected evidence.
        """
        scope_key = self._scope_key(incoming)
        high_water_id = self._scope_high_water.get(scope_key)
        if high_water_id is None:
            return

        previous = self._retired.get(high_water_id)
        if previous is None:
            raise ProductionStateError(
                "admission scope high-water points to a missing replay record."
            )
        if (previous.strategy_id, previous.decision_scope) != scope_key:
            raise ProductionStateError(
                "admission scope high-water record does not match its scope key."
            )

        if (
            incoming.intent_id in self._intents
            or incoming.intent_id in self._retired
            or incoming.intent_id == self._active_intent_id
        ):
            raise ProductionStateError(
                "admission high-water advance incoming intent id is already known."
            )
        if any(
            self._scope_key(queued) == scope_key
            for queued in self._intents.values()
        ):
            raise ProductionStateError(
                "admission high-water advance requires no queued intent in the same scope."
            )

        incoming_key = self._decision_order_key(incoming)
        if incoming_key <= previous.order_key:
            return

        self._assert_replay_invariants_locked()

        heap_indexes = [
            index
            for index, entry in enumerate(self._retired_heap)
            if entry[2] == high_water_id
        ]
        if len(heap_indexes) != 1:
            raise ProductionStateError(
                "admission replay heap does not contain exactly one high-water record."
            )
        previous_heap_entry = self._retired_heap[heap_indexes[0]]
        if (
            previous_heap_entry[0] != previous.expires_at
            or previous_heap_entry[1] != str(high_water_id)
        ):
            raise ProductionStateError(
                "admission replay heap entry diverged from the high-water record."
            )

        new_expiry = max(previous.expires_at, self._replay_expiry(incoming))
        advanced = _RetiredIntent(
            intent_id=previous.intent_id,
            strategy_id=previous.strategy_id,
            decision_scope=previous.decision_scope,
            order_key=incoming_key,
            expires_at=new_expiry,
        )

        index = heap_indexes[0]
        previous_heap_entry = self._retired_heap[index]
        self._retired[high_water_id] = advanced
        self._retired_heap[index] = (
            new_expiry,
            previous_heap_entry[1],
            high_water_id,
        )
        heapq.heapify(self._retired_heap)
        self._assert_replay_invariants_locked()

    def _replace_queued_with_rejected_tombstone_locked(
        self,
        queued: ProductionIntent,
        incoming: ProductionIntent,
    ) -> None:
        """Advance scope high-water to a newer intent rejected only by capacity.

        The queued older intent already owns one replay slot. Replacing that slot with a
        tombstone for the newer evidence keeps global/per-strategy replay usage constant while
        ensuring no intermediate older decision can execute later.
        """
        if self._scope_key(queued) != self._scope_key(incoming):
            raise ProductionStateError(
                "admission capacity tombstone replacement crossed decision scopes."
            )
        if self._decision_order_key(incoming) <= self._decision_order_key(queued):
            raise ProductionStateError(
                "admission capacity tombstone requires strictly newer incoming evidence."
            )
        if queued.intent_id not in self._intents:
            raise ProductionStateError(
                "admission capacity tombstone lost the queued payload."
            )
        if queued.intent_id not in self._queue.intent_ids():
            raise ProductionStateError(
                "admission capacity tombstone lost the queued entry."
            )
        if (
            incoming.intent_id in self._intents
            or incoming.intent_id in self._retired
            or incoming.intent_id == self._active_intent_id
        ):
            raise ProductionStateError(
                "admission capacity tombstone incoming intent id is already known."
            )

        scope_key = self._scope_key(incoming)
        previous_id = self._scope_high_water.get(scope_key)
        previous = None
        if previous_id is not None:
            previous = self._retired.get(previous_id)
            if previous is None:
                raise ProductionStateError(
                    "admission scope high-water points to a missing replay record."
                )
            if self._decision_order_key(incoming) <= previous.order_key:
                raise ProductionStateError(
                    "admission capacity tombstone would not advance scope high-water."
                )

        if not self._queue.remove(queued.intent_id):
            raise ProductionStateError(
                "admission capacity tombstone could not remove the queued predecessor."
            )
        popped = self._intents.pop(queued.intent_id, None)
        if popped is None:
            raise ProductionStateError(
                "admission capacity tombstone payload disappeared during replacement."
            )
        self._decrement_strategy_queue_locked(queued.strategy_id)

        record = _RetiredIntent(
            intent_id=incoming.intent_id,
            strategy_id=incoming.strategy_id,
            decision_scope=incoming.decision_scope,
            order_key=self._decision_order_key(incoming),
            expires_at=self._replay_expiry(incoming),
        )
        self._retired[record.intent_id] = record
        heapq.heappush(
            self._retired_heap,
            (record.expires_at, str(record.intent_id), record.intent_id),
        )
        self._scope_high_water[scope_key] = record.intent_id
        self._check_capacity_bounds_locked(incoming.strategy_id)

    def _evict_locked(self, now: datetime) -> tuple[UUID, ...]:
        self._prune_rejection_floors_locked(now)
        self._prune_replay_locked(now)
        evicted_ids: list[UUID] = []

        # Inspect first, then convert queued -> retired one intent at a time under the same lock.
        # This preserves the accepted intent's replay reservation and high-water evidence.
        for item in tuple(self._queue.snapshot()):
            intent = self._intents.get(item.request.intent_id)
            if intent is None:
                raise ProductionStateError(
                    "admission queue/payload map diverged during expiry."
                )
            market_age = (
                now - item.request.market_event_ts.astimezone(timezone.utc)
            ).total_seconds()
            expired = (
                market_age < 0
                or market_age > self._policy.max_age_seconds
                or now > intent_deadline(intent, self._policy)
            )
            if expired:
                self._convert_queued_to_retired_locked(intent)
                evicted_ids.append(intent.intent_id)

        self._check_global_capacity_bounds_locked()
        return tuple(evicted_ids)

    def _prune_rejection_floors_locked(self, now: datetime) -> None:
        expired_strategies = [
            strategy_id
            for strategy_id, floor in self._strategy_rejection_floor.items()
            if now > floor.expires_at
        ]
        for strategy_id in expired_strategies:
            self._strategy_rejection_floor.pop(strategy_id, None)

        global_floor = self._global_rejection_floor
        if global_floor is not None and now > global_floor.expires_at:
            self._global_rejection_floor = None

        self._assert_rejection_floor_invariants_locked()

    def _prune_replay_locked(self, now: datetime) -> None:
        deferred_active: list[tuple[datetime, str, UUID]] = []
        try:
            while self._retired_heap and now > self._retired_heap[0][0]:
                expires_at, intent_key, intent_id = heapq.heappop(self._retired_heap)
                if intent_id == self._active_intent_id:
                    deferred_active.append((expires_at, intent_key, intent_id))
                    continue
                record = self._retired.get(intent_id)
                if record is None:
                    raise ProductionStateError(
                        "admission replay heap points to a missing retired record."
                    )
                if record.expires_at != expires_at:
                    raise ProductionStateError(
                        "admission replay heap deadline diverged from the retired record."
                    )
                self._retired.pop(intent_id)
                self._decrement_strategy_replay_locked(record.strategy_id)
                scope_key = (record.strategy_id, record.decision_scope)
                if self._scope_high_water.get(scope_key) == intent_id:
                    self._scope_high_water.pop(scope_key, None)
        finally:
            for entry in deferred_active:
                heapq.heappush(self._retired_heap, entry)
        self._check_global_capacity_bounds_locked()

    def _convert_queued_to_retired_locked(self, intent: ProductionIntent) -> None:
        if intent.intent_id in self._retired:
            raise ProductionStateError("admission attempted to retire the same intent twice.")
        if intent.intent_id not in self._intents:
            raise ProductionStateError(
                "admission cannot retire an intent missing from the queue payload map."
            )
        if intent.intent_id not in self._queue.intent_ids():
            raise ProductionStateError(
                "admission cannot retire an intent missing from the bounded queue."
            )

        scope_key = self._scope_key(intent)
        previous_id = self._scope_high_water.get(scope_key)
        previous = None
        if previous_id is not None:
            previous = self._retired.get(previous_id)
            if previous is None:
                raise ProductionStateError(
                    "admission scope high-water points to a missing replay record."
                )

        record = _RetiredIntent(
            intent_id=intent.intent_id,
            strategy_id=intent.strategy_id,
            decision_scope=intent.decision_scope,
            order_key=self._decision_order_key(intent),
            expires_at=self._replay_expiry(intent),
        )

        if not self._queue.remove(intent.intent_id):
            raise ProductionStateError(
                "admission queue could not remove the intent being retired."
            )
        popped = self._intents.pop(intent.intent_id, None)
        if popped is None:
            raise ProductionStateError(
                "admission payload map lost the intent being retired."
            )
        self._decrement_strategy_queue_locked(intent.strategy_id)

        self._retired[record.intent_id] = record
        heapq.heappush(
            self._retired_heap,
            (record.expires_at, str(record.intent_id), record.intent_id),
        )
        if previous is None or record.order_key > previous.order_key:
            self._scope_high_water[scope_key] = record.intent_id
        self._check_capacity_bounds_locked(intent.strategy_id)

    def _increment_strategy_queue_locked(self, strategy_id: str) -> None:
        self._strategy_queue_counts[strategy_id] = (
            self._strategy_queue_counts.get(strategy_id, 0) + 1
        )

    def _decrement_strategy_queue_locked(self, strategy_id: str) -> None:
        current = self._strategy_queue_counts.get(strategy_id, 0)
        if current <= 0:
            raise ProductionStateError(
                "admission per-strategy queue counter would become negative."
            )
        if current == 1:
            self._strategy_queue_counts.pop(strategy_id, None)
        else:
            self._strategy_queue_counts[strategy_id] = current - 1

    def _increment_strategy_replay_locked(self, strategy_id: str) -> None:
        self._strategy_replay_counts[strategy_id] = (
            self._strategy_replay_counts.get(strategy_id, 0) + 1
        )

    def _decrement_strategy_replay_locked(self, strategy_id: str) -> None:
        current = self._strategy_replay_counts.get(strategy_id, 0)
        if current <= 0:
            raise ProductionStateError(
                "admission per-strategy replay counter would become negative."
            )
        if current == 1:
            self._strategy_replay_counts.pop(strategy_id, None)
        else:
            self._strategy_replay_counts[strategy_id] = current - 1

    def _check_global_capacity_bounds_locked(self) -> None:
        if len(self._queue) > self._queue.max_size:
            raise ProductionStateError("admission queue capacity invariant was exceeded.")
        if len(self._retired) + len(self._queue) > self._max_retired:
            raise ProductionStateError("admission replay capacity invariant was exceeded.")

    def _check_capacity_bounds_locked(self, strategy_id: str) -> None:
        self._check_global_capacity_bounds_locked()
        if self._strategy_queue_counts.get(strategy_id, 0) > self._max_per_strategy_queued:
            raise ProductionStateError(
                "admission per-strategy queue capacity invariant was exceeded."
            )
        if self._strategy_replay_counts.get(strategy_id, 0) > self._max_per_strategy_retired:
            raise ProductionStateError(
                "admission per-strategy replay capacity invariant was exceeded."
            )

    def _assert_rejection_floor_invariants_locked(self) -> None:
        if len(self._strategy_rejection_floor) > self._max_rejection_strategies:
            raise ProductionStateError(
                "admission rejection-floor strategy capacity invariant was exceeded."
            )

        delta = timedelta(seconds=self._policy.max_age_seconds)
        for floor in self._strategy_rejection_floor.values():
            if floor.expires_at != floor.order_key[0] + delta:
                raise ProductionStateError(
                    "admission strategy rejection floor expiry diverged from its evidence."
                )

        global_floor = self._global_rejection_floor
        if (
            global_floor is not None
            and global_floor.expires_at != global_floor.order_key[0] + delta
        ):
            raise ProductionStateError(
                "admission global rejection floor expiry diverged from its evidence."
            )

    def _assert_replay_invariants_locked(self) -> None:
        queue_ids = self._queue.intent_ids()
        queue_id_set = set(queue_ids)
        retired_ids = set(self._retired)

        if len(queue_ids) != len(queue_id_set):
            raise ProductionStateError("admission queue contains duplicate intent ids.")
        if queue_id_set != set(self._intents):
            raise ProductionStateError(
                "admission queue and payload map contain different intent ids."
            )
        if queue_id_set & retired_ids:
            raise ProductionStateError(
                "admission intent cannot be both queued and retired."
            )
        if len(self._retired) + len(self._queue) > self._max_retired:
            raise ProductionStateError("admission replay capacity invariant was exceeded.")

        expected_queue_counts: dict[str, int] = {}
        expected_replay_counts: dict[str, int] = {}
        for queued in self._intents.values():
            expected_queue_counts[queued.strategy_id] = (
                expected_queue_counts.get(queued.strategy_id, 0) + 1
            )
            expected_replay_counts[queued.strategy_id] = (
                expected_replay_counts.get(queued.strategy_id, 0) + 1
            )
        for record in self._retired.values():
            expected_replay_counts[record.strategy_id] = (
                expected_replay_counts.get(record.strategy_id, 0) + 1
            )
        if expected_queue_counts != self._strategy_queue_counts:
            raise ProductionStateError(
                "admission per-strategy queue counters diverged from queued state."
            )
        if expected_replay_counts != self._strategy_replay_counts:
            raise ProductionStateError(
                "admission per-strategy replay counters diverged from replay state."
            )
        if any(
            count > self._max_per_strategy_retired
            for count in expected_replay_counts.values()
        ):
            raise ProductionStateError(
                "admission per-strategy replay capacity invariant was exceeded."
            )

        heap_ids = [entry[2] for entry in self._retired_heap]
        if len(heap_ids) != len(retired_ids) or set(heap_ids) != retired_ids:
            raise ProductionStateError(
                "admission replay heap and retired ledger diverged."
            )
        if not set(self._scope_high_water.values()).issubset(retired_ids):
            raise ProductionStateError(
                "admission scope high-water points outside the retired ledger."
            )

        newest_by_scope: dict[tuple[str, str], _RetiredIntent] = {}
        for retired in self._retired.values():
            scope_key = (retired.strategy_id, retired.decision_scope)
            current = newest_by_scope.get(scope_key)
            if current is None or retired.order_key > current.order_key:
                newest_by_scope[scope_key] = retired

        for scope_key, intent_id in self._scope_high_water.items():
            record = self._retired[intent_id]
            if (record.strategy_id, record.decision_scope) != scope_key:
                raise ProductionStateError(
                    "admission scope high-water key does not match its retired record."
                )
            newest = newest_by_scope.get(scope_key)
            if newest is None or record.order_key != newest.order_key:
                raise ProductionStateError(
                    "admission scope high-water is not the newest retired evidence."
                )
        if self._active_intent_id is not None and self._active_intent_id not in retired_ids:
            raise ProductionStateError(
                "admission active intent is not protected by the replay ledger."
            )
        self._assert_rejection_floor_invariants_locked()

    def _replay_expiry(self, intent: ProductionIntent) -> datetime:
        return intent.decision_completed_at.astimezone(timezone.utc) + timedelta(
            seconds=self._policy.max_age_seconds
        )

    @staticmethod
    def _scope_key(intent: ProductionIntent) -> tuple[str, str]:
        return (intent.strategy_id, intent.decision_scope)

    @staticmethod
    def _decision_order_key(intent: ProductionIntent) -> tuple[datetime, datetime]:
        return (
            intent.decision_completed_at.astimezone(timezone.utc),
            intent.market_event_ts.astimezone(timezone.utc),
        )

    def _time_rejection_reason(
        self, intent: ProductionIntent, now: datetime
    ) -> str | None:
        completed_utc = intent.decision_completed_at.astimezone(timezone.utc)
        started_utc = intent.decision_started_at.astimezone(timezone.utc)
        market_event_utc = intent.market_event_ts.astimezone(timezone.utc)
        if market_event_utc > now:
            return "market_event_ts_in_future"
        if started_utc > completed_utc or market_event_utc > completed_utc:
            return "decision_time_order_invalid"
        if completed_utc > now:
            return "decision_completed_in_future"

        explicit_deadlines: list[datetime] = []
        if intent.valid_until is not None:
            explicit_deadlines.append(intent.valid_until.astimezone(timezone.utc))
        if self._policy.valid_until_seconds is not None:
            explicit_deadlines.append(
                intent.decision_completed_at.astimezone(timezone.utc)
                + timedelta(seconds=self._policy.valid_until_seconds)
            )
        if explicit_deadlines and now > min(explicit_deadlines):
            return "valid_until_exceeded"
        age_seconds = (
            now - intent.market_event_ts.astimezone(timezone.utc)
        ).total_seconds()
        if age_seconds < 0:
            return "market_event_ts_in_future"
        if age_seconds > self._policy.max_age_seconds:
            return "max_age_exceeded"
        return None

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ProductionStateError(
                "production admission clock must return a timezone-aware datetime."
            )
        now_utc = now.astimezone(timezone.utc)
        if self._last_now is not None and now_utc < self._last_now:
            raise ProductionStateError(
                "production admission clock moved backwards; refusing to continue."
            )
        self._last_now = now_utc
        return now_utc
