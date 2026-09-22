"""Journal every validity decision (Phase 6).

Every validity verdict is appended to the persistent journal as the additive
``validity.disposition.recorded`` contract, so disposition history is auditable and replayable.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from crypto_trader.persistence import EventJournal

from .gate import ValidityDecision, to_phase3_event

__all__ = ["record_validity_decision"]


def record_validity_decision(
    journal: EventJournal,
    decision: ValidityDecision,
    *,
    producer: str = "application.validity",
    notes: str | None = None,
    recorded_at: datetime | None = None,
    event_id: UUID | None = None,
) -> UUID:
    """Append the decision to the journal and return the resulting event id.

    Pass the same ``event_id`` when re-delivering the *same logical* decision so the journal
    stays idempotent and the consumer sees exactly one effect.
    """
    event = to_phase3_event(
        decision,
        producer=producer,
        notes=notes,
        occurred_at=recorded_at,
        event_id=event_id or uuid4(),
    )
    journal.append(event)
    return event.event_id
