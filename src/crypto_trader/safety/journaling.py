"""Journal operational safety transitions (Phase 9).

Every realized transition is appended through the ADDITIVE Phase 9 ``safety.state.changed``
contract. An idempotent no-op transition (``changed is False``) produces NO event, so
re-delivering the same kill-switch/halt request never creates a second effect.

The existing Phase 3 ``recovery.state.changed`` contract keeps its own meaning and is not
overloaded with safety modes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from crypto_trader.events import SafetyStateChangePayload, SafetyStateChanged
from crypto_trader.persistence import EventJournal

from .controls import SafetyTransition
from .errors import SafetyConfigError

__all__ = ["record_safety_state", "record_safety_transitions", "safety_state_events"]


def safety_state_events(
    transition: SafetyTransition,
    *,
    occurred_at: datetime,
    producer: str = "application.safety",
    correlation_id: UUID | None = None,
    event_id: UUID | None = None,
) -> list[SafetyStateChanged]:
    """Build the event for one realized transition (empty list for an idempotent no-op)."""
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise SafetyConfigError("safety transition occurred_at must be timezone-aware.")
    if not transition.changed:
        return []
    return [
        SafetyStateChanged(
            event_id=event_id or uuid4(),
            occurred_at=occurred_at.astimezone(timezone.utc),
            correlation_id=correlation_id or transition.correlation_id,
            producer=producer,
            payload=SafetyStateChangePayload(
                mode=transition.mode.value,
                previous_mode=transition.previous_mode.value,
                reason_code=transition.reason_code,
                reason=transition.reason,
                trigger=transition.trigger,
                kill_switch_engaged=transition.kill_switch_engaged,
            ),
        )
    ]


def record_safety_state(
    journal: EventJournal,
    transition: SafetyTransition,
    *,
    occurred_at: datetime,
    producer: str = "application.safety",
    correlation_id: UUID | None = None,
    event_id: UUID | None = None,
) -> list[UUID]:
    """Journal one realized transition; returns the appended event ids (empty for a no-op)."""
    events = safety_state_events(
        transition,
        occurred_at=occurred_at,
        producer=producer,
        correlation_id=correlation_id,
        event_id=event_id,
    )
    if events:
        journal.append_batch(events)
    return [event.event_id for event in events]


def record_safety_transitions(
    journal: EventJournal,
    transitions: tuple[SafetyTransition, ...],
    *,
    occurred_at: datetime,
    producer: str = "application.safety",
    correlation_id: UUID | None = None,
    event_ids: tuple[UUID, ...] | None = None,
) -> list[UUID]:
    """Journal several transitions (e.g. a break-glass posture change) in ONE transaction."""
    events: list[SafetyStateChanged] = []
    ids = list(event_ids) if event_ids is not None else []
    for index, transition in enumerate(transitions):
        if not transition.changed:
            continue
        event_ids_for_transition = (ids[index],) if index < len(ids) else (None,)
        events.extend(
            safety_state_events(
                transition,
                occurred_at=occurred_at,
                producer=producer,
                correlation_id=correlation_id,
                event_id=event_ids_for_transition[0],
            )
        )
    if events:
        journal.append_batch(events)
    return [event.event_id for event in events]
