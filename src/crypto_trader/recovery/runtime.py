"""Restart recovery + reconciliation runtime (Phase 8).

Restart sequence (deterministic, no wall-clock reads):

1. restore a snapshot if one is available (acceleration only),
2. replay the journal tail and rebuild pending application state,
3. identify ambiguous orders,
4. reconcile against the execution/venue view,
5. resolve ONLY with evidence,
6. journal the reconciliation outcome.

Reconciliation sources are engine-independent: the application journal, a Nautilus-runtime/cache
view and a venue/execution-adapter view all present the same ``VenueOrderReport`` shape, so the
same deterministic engine reconciles them. Two sources that agree are deduplicated; two sources
that disagree are ``UNKNOWN_PENDING_RECONCILIATION`` and block new risk.

This runtime NEVER submits, cancels or retries an order. An ambiguous submission stays ambiguous
until external evidence appears.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from crypto_trader.persistence import EventJournal, RebuildSummary, SnapshotRecord

from .engine import ReconciliationEngine
from .journaling import record_reconciliation
from .models import LocalOrderState, ReconciliationReport, VenueOrderReport
from .reconstruction import (
    AttemptAnchors,
    OrderStateReconstructor,
    reconstruct_order_states,
    validate_venue_evidence,
)

__all__ = [
    "RecoveryRun",
    "RestartRecovery",
    "StaticVenueStateSource",
    "VenueStateSource",
    "ReconciliationSources",
]


@runtime_checkable
class VenueStateSource(Protocol):
    """An engine-independent view of order state we can reconcile against."""

    def reports(self) -> Sequence[VenueOrderReport]: ...


@dataclass(frozen=True)
class StaticVenueStateSource:
    """A fixed venue view (used for offline reconciliation and tests)."""

    entries: tuple[VenueOrderReport, ...] = ()
    name: str = "venue"

    def reports(self) -> Sequence[VenueOrderReport]:
        return self.entries


@dataclass(frozen=True)
class ReconciliationSources:
    """Combine several engine-independent state views into one reconciliation input."""

    sources: tuple[VenueStateSource, ...] = ()

    def reports(self) -> Sequence[VenueOrderReport]:
        combined: list[VenueOrderReport] = []
        for source in self.sources:
            combined.extend(source.reports())
        return tuple(combined)


@dataclass(frozen=True)
class RecoveryRun:
    """The complete, journaled result of one restart recovery pass."""

    report: ReconciliationReport
    rebuilt_states: tuple[LocalOrderState, ...] = ()
    rebuild: RebuildSummary | None = None
    journaled_event_ids: list[UUID] = field(default_factory=list)
    recovery_state: Literal["resolved", "failed"] = "resolved"
    #: References whose reconstructed lifecycle carried an authoritative contradiction. They stay
    #: unresolved (new risk fenced) until external evidence resolves them; the reason codes live in
    #: ``report.reason_codes``.
    conflicted_orders: tuple[str, ...] = ()
    #: Terminal orders whose executed quantity advanced from a late authoritative fill.
    late_fill_refinements: int = 0

    @property
    def blocked_orders(self) -> list[str]:
        """Orders that MUST NOT be resubmitted; they need external evidence/operator action."""
        return self.report.unresolved_orders

    @property
    def requires_halt_new_risk(self) -> bool:
        return self.report.requires_halt_new_risk

    @property
    def may_resume_trading(self) -> bool:
        """New risk may only resume once every order is reconciled with evidence."""
        return self.recovery_state == "resolved"


class RestartRecovery:
    """Deterministic restart recovery orchestrator."""

    def __init__(
        self,
        *,
        engine: ReconciliationEngine,
        journal: EventJournal,
        venue_sources: Sequence[VenueStateSource],
        producer: str = "application.recovery",
    ) -> None:
        if not isinstance(engine, ReconciliationEngine):
            raise TypeError("engine must be a ReconciliationEngine.")
        if not venue_sources:
            raise ValueError("at least one venue state source is required.")
        self._engine = engine
        self._journal = journal
        self._sources = tuple(venue_sources)
        self._producer = producer

    @property
    def engine(self) -> ReconciliationEngine:
        return self._engine

    def reconcile_now(
        self,
        *,
        local_states: Sequence[LocalOrderState],
    ) -> ReconciliationReport:
        """Reconcile a supplied local view without reading the journal (offline use)."""
        return self._engine.reconcile(local_states, self._collect_venue_reports())

    def run(
        self,
        *,
        now: datetime,
        snapshot: SnapshotRecord | None = None,
        local_states: Sequence[LocalOrderState] | None = None,
        correlation_id: UUID | None = None,
        event_ids: tuple[UUID, UUID, UUID] | None = None,
    ) -> RecoveryRun:
        """Restore, rebuild, reconcile and journal one restart recovery pass."""
        reconstructor, rebuild = reconstruct_order_states(self._journal, snapshot=snapshot)
        states = reconstructor.states() if local_states is None else tuple(local_states)
        conflicts: dict[str, list[str]] = {
            reference: list(reasons) for reference, reasons in reconstructor.conflicts().items()
        }
        late_fill_refinements = reconstructor.late_fill_refinements
        anchors: dict[str, AttemptAnchors] = reconstructor.attempt_anchor_map()

        # Current venue evidence is validated against the durable attempt anchors before it can
        # reach the engine: a FILLED report must prove executedQty == Q and origQty == Q, otherwise
        # it is refused and the order stays unresolved (fenced).
        accepted_reports: list[VenueOrderReport] = []
        for venue_report in self._collect_venue_reports():
            reason = validate_venue_evidence(
                anchors.get(venue_report.order_reference), venue_report
            )
            if reason is None:
                accepted_reports.append(venue_report)
                continue
            existing = conflicts.setdefault(venue_report.order_reference, [])
            if reason not in existing:
                existing.append(reason)

        report = self._engine.reconcile(states, tuple(accepted_reports))
        if conflicts:
            # A lifecycle contradiction is never dropped and never auto-resolved: it keeps the
            # order unresolved so the fence stays engaged until authoritative evidence exists.
            report = replace(
                report,
                ambiguous_orders=sorted({*report.ambiguous_orders, *conflicts}),
                reason_codes={
                    **report.reason_codes,
                    **{reference: ",".join(reasons) for reference, reasons in conflicts.items()},
                },
                requires_halt_new_risk=True,
            )
        journaled = record_reconciliation(
            self._journal,
            report,
            occurred_at=now,
            producer=self._producer,
            correlation_id=correlation_id,
            event_ids=event_ids,
        )
        return RecoveryRun(
            report=report,
            rebuilt_states=states,
            rebuild=rebuild,
            journaled_event_ids=journaled,
            recovery_state="resolved" if report.is_fully_resolved else "failed",
            conflicted_orders=tuple(sorted(conflicts)),
            late_fill_refinements=late_fill_refinements,
        )

    def _collect_venue_reports(self) -> tuple[VenueOrderReport, ...]:
        combined: list[VenueOrderReport] = []
        for source in self._sources:
            combined.extend(source.reports())
        return tuple(combined)

    @staticmethod
    def reconstructor() -> OrderStateReconstructor:
        """Expose a fresh reconstructor for callers that want to inspect rebuilt state."""
        return OrderStateReconstructor()
