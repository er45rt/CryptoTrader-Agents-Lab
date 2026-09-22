"""Journal reconciliation outcomes (Phase 8).

Every reconciliation run is recorded through the existing Phase 3 ``recovery.state.changed``
contract (states: ``detected`` -> ``reconciling`` -> ``resolved`` | ``failed``). That schema is
reused unchanged - it is not rewritten, and no new event contract is added for Phase 8.

The three events form one deterministic record per run. Pass the same ``event_ids`` when
re-running the *same* logical reconciliation so the journal stays idempotent by event id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from crypto_trader.events import RecoveryStateChangePayload, RecoveryStateChanged
from crypto_trader.persistence import EventJournal

from .errors import RecoveryConfigError
from .models import ReconciliationReport

__all__ = ["reconciliation_events", "record_reconciliation"]


def reconciliation_events(
    report: ReconciliationReport,
    *,
    occurred_at: datetime,
    producer: str = "application.recovery",
    correlation_id: UUID | None = None,
    event_ids: tuple[UUID, UUID, UUID] | None = None,
) -> list[RecoveryStateChanged]:
    """Build the ``detected`` / ``reconciling`` / terminal recovery events for one report."""
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise RecoveryConfigError("reconciliation occurred_at must be timezone-aware.")
    stamp = occurred_at.astimezone(timezone.utc)
    ids = event_ids if event_ids is not None else (uuid4(), uuid4(), uuid4())
    if len(ids) != 3:
        raise RecoveryConfigError("exactly three recovery event ids are required per run.")

    correlation = correlation_id if correlation_id is not None else _correlation_id(report)
    affected = report.affected_order_ids()
    unresolved = report.unresolved_orders

    detected = RecoveryStateChanged(
        event_id=ids[0],
        occurred_at=stamp,
        correlation_id=correlation,
        producer=producer,
        payload=RecoveryStateChangePayload(
            state="detected",
            reason=f"reconciliation_started:{len(report.outcomes)}_orders",
            affected_order_ids=affected,
        ),
    )
    reconciling = RecoveryStateChanged(
        event_id=ids[1],
        occurred_at=stamp,
        correlation_id=correlation,
        producer=producer,
        payload=RecoveryStateChangePayload(
            state="reconciling",
            reason=f"reconciliation_running:{_policy_summary(report)}",
            affected_order_ids=affected,
        ),
    )
    if unresolved:
        terminal = RecoveryStateChanged(
            event_id=ids[2],
            occurred_at=stamp,
            correlation_id=correlation,
            producer=producer,
            payload=RecoveryStateChangePayload(
                state="failed",
                reason="unknown_pending_reconciliation:" + ",".join(unresolved),
                affected_order_ids=_ids_for(report, unresolved),
            ),
        )
    else:
        terminal = RecoveryStateChanged(
            event_id=ids[2],
            occurred_at=stamp,
            correlation_id=correlation,
            producer=producer,
            payload=RecoveryStateChangePayload(
                state="resolved",
                reason=f"reconciliation_complete:{len(report.resolved_orders)}_orders",
                affected_order_ids=affected,
            ),
        )
    return [detected, reconciling, terminal]


def record_reconciliation(
    journal: EventJournal,
    report: ReconciliationReport,
    *,
    occurred_at: datetime,
    producer: str = "application.recovery",
    correlation_id: UUID | None = None,
    event_ids: tuple[UUID, UUID, UUID] | None = None,
) -> list[UUID]:
    """Journal one reconciliation run and return the appended event ids."""
    events = reconciliation_events(
        report,
        occurred_at=occurred_at,
        producer=producer,
        correlation_id=correlation_id,
        event_ids=event_ids,
    )
    journal.append_batch(events)
    return [event.event_id for event in events]


def _correlation_id(report: ReconciliationReport) -> UUID:
    """Derive the run correlation identity deterministically, or fail closed.

    A run touching exactly one order correlation reuses it; a run spanning several correlations
    must be given an explicit id by the caller, so the journal never invents identity.
    """
    distinct = report.distinct_correlation_ids()
    if len(distinct) == 1:
        return distinct[0]
    if not distinct:
        raise RecoveryConfigError(
            "a reconciliation run with no known order correlation requires an explicit "
            "correlation_id."
        )
    raise RecoveryConfigError(
        "a reconciliation run spanning multiple correlations requires an explicit correlation_id."
    )


def _ids_for(report: ReconciliationReport, references: list[str]) -> list[UUID]:
    return sorted(
        {report.order_ids[reference] for reference in references if reference in report.order_ids},
        key=str,
    )


def _policy_summary(report: ReconciliationReport) -> str:
    return (
        f"resolved={len(report.resolved_orders)}"
        f":ambiguous={len(report.ambiguous_orders)}"
        f":local_ahead={len(report.local_ahead_of_venue)}"
        f":unknown_external={len(report.unknown_external_orders)}"
        f":never_submitted={len(report.never_submitted)}"
    )
