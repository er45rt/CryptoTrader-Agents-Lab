"""Fenced pre-start maintenance writer for G4 Binance trade backfill.

The pure planner lives in execution.binance_testnet.backfill. This module is the only side-effect
boundary for applying one already-reviewed plan. It never contacts Binance and never starts the
production runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from crypto_trader.breakglass import BreakGlassControl, BreakGlassError
from crypto_trader.events import EXECUTION_FILL_EVENT_TYPE, VENUE_TRADE_EVENT_TYPE
from crypto_trader.execution.binance_testnet.backfill import (
    TradeBackfillPlan,
    trade_backfill_plan_fingerprint,
)
from crypto_trader.persistence import (
    EventJournal,
    JournalError,
    JournalRevision,
    JournalRevisionMismatchError,
    SnapshotError,
    SnapshotStore,
)

from .errors import ProductionStateError

__all__ = ["TradeBackfillMaintenanceWriter", "TradeBackfillWriteReport"]

_DEFAULT_OWNER_ID: Final[str] = "normal:trade-backfill-maintenance"
_SNAPSHOT_NAME: Final[str] = "orders"
_ALLOWED_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {EXECUTION_FILL_EVENT_TYPE, VENUE_TRADE_EVENT_TYPE}
)


def _event_ns(event) -> int:
    return int(event.occurred_at.timestamp() * 1_000_000_000)


@dataclass(frozen=True)
class TradeBackfillWriteReport:
    """Evidence returned after one atomic maintenance append."""

    owner_id: str
    fence_term: int | None
    source_fingerprint: str
    plan_fingerprint: str
    final_fingerprint: str
    planned_event_ids: tuple[UUID, ...]
    inserted_events: int
    snapshot_name: str = _SNAPSHOT_NAME

    @property
    def changed(self) -> bool:
        return self.inserted_events > 0


class TradeBackfillMaintenanceWriter:
    """Apply a pure backfill plan under the normal-writer fencing discipline."""

    def __init__(
        self,
        *,
        journal: EventJournal,
        control: BreakGlassControl,
        snapshot_store: SnapshotStore,
        symbol: str,
        owner_id: str = _DEFAULT_OWNER_ID,
    ) -> None:
        if not isinstance(control, BreakGlassControl):
            raise ProductionStateError("trade backfill maintenance requires BreakGlassControl.")
        if not isinstance(snapshot_store, SnapshotStore):
            raise ProductionStateError(
                "trade backfill maintenance requires an explicit SnapshotStore."
            )
        if not isinstance(symbol, str) or not symbol.strip():
            raise ProductionStateError("trade backfill maintenance requires a non-empty symbol.")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ProductionStateError("trade backfill maintenance requires a non-empty owner id.")
        self._journal = journal
        self._control = control
        self._snapshot_store = snapshot_store
        self._symbol = symbol
        self._owner_id = owner_id

    @staticmethod
    def _expected_revision(plan: TradeBackfillPlan) -> JournalRevision:
        try:
            return JournalRevision(
                entries=plan.source_entries,
                fingerprint=plan.source_fingerprint,
            )
        except (TypeError, ValueError) as exc:
            raise ProductionStateError(
                "trade backfill plan carries an invalid source revision."
            ) from exc

    def _require_source_unchanged(
        self,
        plan: TradeBackfillPlan,
        *,
        expected: JournalRevision,
    ) -> None:
        try:
            current = self._journal.revision()
        except JournalError as exc:
            raise ProductionStateError(
                "trade backfill refused: journal revision could not be verified."
            ) from exc
        if current != expected:
            raise ProductionStateError(
                "trade backfill refused: journal changed after the plan was constructed."
            )

    def _require_snapshot_safe(self, plan: TradeBackfillPlan) -> None:
        if plan.is_empty:
            return
        try:
            snapshot = self._snapshot_store.latest(_SNAPSHOT_NAME)
        except SnapshotError as exc:
            raise ProductionStateError(
                "trade backfill refused: snapshot state could not be verified."
            ) from exc
        if snapshot is None:
            return

        has_event_id = snapshot.last_event_id is not None
        has_occurred_at = snapshot.last_occurred_at_ns is not None
        if has_event_id != has_occurred_at:
            raise ProductionStateError(
                "trade backfill refused: snapshot cursor is incomplete or malformed."
            )
        if not has_event_id:
            return

        source_ids = set(plan.source_event_ids)
        if snapshot.last_event_id not in source_ids:
            raise ProductionStateError(
                "trade backfill refused: snapshot cursor is not part of the planned journal view."
            )
        source_event = self._journal.read_by_id(snapshot.last_event_id)
        if source_event is None:
            raise ProductionStateError(
                "trade backfill refused: snapshot cursor event is missing from the journal."
            )
        source_ns = _event_ns(source_event)
        if source_ns != snapshot.last_occurred_at_ns:
            raise ProductionStateError(
                "trade backfill refused: snapshot ordering cursor does not match its event."
            )

        earliest_backfill_ns = min(_event_ns(event) for event in plan.events)
        if snapshot.last_occurred_at_ns >= earliest_backfill_ns:
            raise ProductionStateError(
                "trade backfill refused: an existing snapshot would hide historical backfill events."
            )

    def _require_plan_integrity(self, plan: TradeBackfillPlan) -> JournalRevision:
        if not isinstance(plan.references, tuple):
            raise ProductionStateError("trade backfill references must be an immutable tuple.")
        if tuple(sorted(set(plan.references))) != plan.references:
            raise ProductionStateError(
                "trade backfill references must be sorted and unique."
            )

        event_ids = tuple(event.event_id for event in plan.events)
        if len(set(event_ids)) != len(event_ids):
            raise ProductionStateError("trade backfill plan contains duplicate event identities.")
        if any(event.event_type not in _ALLOWED_EVENT_TYPES for event in plan.events):
            raise ProductionStateError(
                "trade backfill plan contains an event type outside the maintenance contract."
            )
        if any(event.symbol != self._symbol for event in plan.events):
            raise ProductionStateError(
                "trade backfill plan contains an event outside the writer symbol."
            )

        expected = self._expected_revision(plan)
        if set(event_ids).intersection(expected.event_ids):
            raise ProductionStateError(
                "trade backfill plan overlaps an event identity already in its source revision."
            )

        recalculated = trade_backfill_plan_fingerprint(
            events=plan.events,
            references=plan.references,
            source_fingerprint=plan.source_fingerprint,
        )
        if plan.plan_fingerprint != recalculated:
            raise ProductionStateError(
                "trade backfill plan fingerprint does not match its reviewed contents."
            )
        return expected

    def apply(self, plan: TradeBackfillPlan) -> TradeBackfillWriteReport:
        """Apply one plan atomically.

        Before the normal-writer lease is claimed every refusal is side-effect free. After a lease
        is claimed, a later refusal leaves that newer maintenance fence persisted by design; the
        production runtime must claim a fresh normal-writer lease before resuming.
        """

        if not isinstance(plan, TradeBackfillPlan):
            raise ProductionStateError("trade backfill writer requires a TradeBackfillPlan.")

        expected = self._require_plan_integrity(plan)
        self._require_source_unchanged(plan, expected=expected)
        self._require_snapshot_safe(plan)

        if plan.is_empty:
            return TradeBackfillWriteReport(
                owner_id=self._owner_id,
                fence_term=None,
                source_fingerprint=plan.source_fingerprint,
                plan_fingerprint=plan.plan_fingerprint,
                final_fingerprint=plan.source_fingerprint,
                planned_event_ids=(),
                inserted_events=0,
            )

        try:
            guard = self._control.claim_normal_writer(
                owner_id=self._owner_id,
                reason_code="trade_backfill_maintenance",
            )
        except BreakGlassError as exc:
            raise ProductionStateError(
                "trade backfill refused: normal writer lease is unavailable."
            ) from exc

        planned_ids = tuple(event.event_id for event in plan.events)
        try:
            with self._control.ownership_store.exclusive(purpose="trade_backfill"):
                guard.require_current()
                self._require_source_unchanged(plan, expected=expected)
                self._require_snapshot_safe(plan)

                try:
                    committed = self._journal.append_batch_if_revision(
                        plan.events,
                        expected=expected,
                        require_all_new=True,
                    )
                except JournalRevisionMismatchError as exc:
                    raise ProductionStateError(
                        "trade backfill refused: journal changed before the atomic append."
                    ) from exc
                except JournalError as exc:
                    raise ProductionStateError(
                        "trade backfill atomic journal append failed; re-read the journal and re-plan "
                        "before any retry."
                    ) from exc
        except BreakGlassError as exc:
            raise ProductionStateError(
                "trade backfill refused: writer fence changed during maintenance."
            ) from exc

        return TradeBackfillWriteReport(
            owner_id=self._owner_id,
            fence_term=guard.fence.term,
            source_fingerprint=plan.source_fingerprint,
            plan_fingerprint=plan.plan_fingerprint,
            final_fingerprint=committed.after.fingerprint,
            planned_event_ids=planned_ids,
            inserted_events=committed.inserted,
        )
