"""Deterministic replay and idempotent consumption (Phase 5).

Delivery remains at-least-once: a consumer may see the same ``event_id`` more than once and
must produce the same single effect. Replay order is deterministic, so the same journal (or
snapshot + tail) always rebuilds the same state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from crypto_trader.events import EventEnvelope

from .journal import EventJournal
from .snapshots import SnapshotRecord

__all__ = ["IdempotentConsumer", "RebuildSummary", "rebuild_state"]


class IdempotentConsumer(ABC):
    """Applies each logical event at most once, keyed on ``event_id``."""

    def __init__(self) -> None:
        self._applied: set[UUID] = set()
        self.applied_events: list[UUID] = []

    @abstractmethod
    def apply(self, event: EventEnvelope) -> None:
        """Perform the domain effect for one event (must be safe to skip when duplicate)."""

    def handle(self, event: EventEnvelope) -> bool:
        """Apply the event unless it was already applied. Returns True when applied."""
        if event.event_id in self._applied:
            return False
        self.apply(event)
        self._applied.add(event.event_id)
        self.applied_events.append(event.event_id)
        return True

    def seed_applied(self, event_ids: set[UUID]) -> None:
        """Seed the dedup set (e.g. from a snapshot's recorded position)."""
        self._applied |= set(event_ids)

    def restore_state(self, state: dict[str, Any]) -> None:
        """Restore derived state from a snapshot (acceleration only)."""
        self._state = state

    @property
    def applied_count(self) -> int:
        return len(self._applied)


@dataclass
class RebuildSummary:
    """Outcome of replaying a journal (optionally from a snapshot)."""

    events_seen: int = 0
    events_applied: int = 0
    duplicates_skipped: int = 0
    last_event_id: UUID | None = None
    last_occurred_at_ns: int | None = None
    last_sequence_number: int | None = None
    applied_event_ids: list[UUID] = field(default_factory=list)


def rebuild_state(
    journal: EventJournal,
    consumer: IdempotentConsumer,
    *,
    snapshot: SnapshotRecord | None = None,
) -> RebuildSummary:
    """Rebuild consumer state by replaying the journal (snapshot + tail when available).

    The snapshot is an acceleration only: if it is present, replay starts after the snapshot's
    recorded position and the snapshot state is restored first.
    """
    summary = RebuildSummary()

    if snapshot is not None and snapshot.last_event_id is not None:
        consumer.restore_state(dict(snapshot.state))
        consumer.seed_applied({snapshot.last_event_id})
        summary.last_event_id = snapshot.last_event_id
        summary.last_occurred_at_ns = snapshot.last_occurred_at_ns
        summary.last_sequence_number = snapshot.last_sequence_number

    for event in journal.replay(
        after_occurred_at_ns=None if snapshot is None else snapshot.last_occurred_at_ns,
        after_event_id=None if snapshot is None else snapshot.last_event_id,
    ):
        summary.events_seen += 1
        if consumer.handle(event):
            summary.events_applied += 1
            summary.applied_event_ids.append(event.event_id)
        else:
            summary.duplicates_skipped += 1
        summary.last_event_id = event.event_id
        summary.last_occurred_at_ns = int(event.occurred_at.timestamp() * 1_000_000_000)
        if event.sequence_number is not None:
            summary.last_sequence_number = event.sequence_number

    return summary
