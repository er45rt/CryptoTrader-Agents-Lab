"""Journal every Hard Risk decision (Phase 7).

Two records are written per evaluation:
* the existing Phase 3 ``risk.decision.recorded`` contract (semantic allow/reject outcome), and
* the additive ``risk.evaluation.recorded`` contract (full deterministic rule audit).

Both are idempotent by event id, so re-delivering the same evaluation adds nothing.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid4

from crypto_trader.events import RiskDecisionPayload, RiskDecisionRecorded
from crypto_trader.persistence import (
    ConditionalAppendResult, EventIdConflictError, EventJournal, JournalRevision, JournalSchemaError,
)

from .engine import HardRiskEngine, RiskDecision

__all__ = ["record_risk_decision"]


def record_risk_decision(
    journal: EventJournal,
    decision: RiskDecision,
    *,
    occurred_at: datetime,
    producer: str = "application.risk",
    event_id: UUID | None = None,
    evaluation_event_id: UUID | None = None,
    expected_revision: JournalRevision | None = None,
) -> tuple[UUID, UUID]:
    """Validate and atomically record one complete semantic/audit pair.

    Pass the same ``event_id`` / ``evaluation_event_id`` when re-delivering the *same logical*
    evaluation: both appends are idempotent by event id and produce exactly one effect.
    A Paper caller supplies the revision captured before risk evaluation, after
    preliminary writes. Direct callers capture it here before building events.
    Existing pairs must match exactly; a partial pair is never repaired. The
    revision check and writes use the existing conditional transaction API.
    No exception proves rollback: the session must stop on an uncertain result.
    """
    expected = journal.revision() if expected_revision is None else expected_revision
    if not isinstance(expected, JournalRevision):
        raise JournalSchemaError("risk journaling requires a JournalRevision.")
    semantic = RiskDecisionRecorded(
        event_id=event_id or uuid4(),
        occurred_at=occurred_at,
        correlation_id=decision.correlation_id,
        producer=producer,
        symbol=decision.symbol,
        payload=RiskDecisionPayload(
            approved=decision.allowed,
            reason="; ".join(decision.reason_codes),
            approved_quantity=decision.approved_quantity,
        ),
    )
    evaluation = HardRiskEngine.to_phase3_evaluation(
        decision,
        occurred_at=occurred_at,
        event_id=evaluation_event_id,
    )
    pair = (semantic, evaluation)
    if semantic.event_id == evaluation.event_id:
        raise JournalSchemaError("risk pair requires distinct event identities.")
    previous = dict(expected.entries)
    present = [event.event_id in previous for event in pair]
    if any(present) and not all(present):
        raise JournalSchemaError("incomplete risk pair; refusing automatic repair.")
    digests = {event.event_id: sha256(event.to_json().encode("utf-8")).hexdigest() for event in pair}
    if all(present):
        for event in pair:
            digest = digests[event.event_id]
            if previous[event.event_id] != digest:
                raise EventIdConflictError(event.event_id)
    result = journal.append_batch_if_revision(pair, expected=expected)
    after = {**previous, **digests}
    if (
        not isinstance(result, ConditionalAppendResult)
        or result.before != expected
        or result.inserted != (0 if all(present) else 2)
        or dict(result.after.entries) != after
    ):
        raise JournalSchemaError("uncertain risk pair append acknowledgement; refusing to proceed.")
    return semantic.event_id, evaluation.event_id
