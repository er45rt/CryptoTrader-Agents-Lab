"""Journal every Hard Risk decision (Phase 7).

Two records are written per evaluation:
* the existing Phase 3 ``risk.decision.recorded`` contract (semantic allow/reject outcome), and
* the additive ``risk.evaluation.recorded`` contract (full deterministic rule audit).

Both are idempotent by event id, so re-delivering the same evaluation adds nothing.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from crypto_trader.events import RiskDecisionPayload, RiskDecisionRecorded
from crypto_trader.persistence import EventJournal

from .engine import HardRiskEngine, RiskDecision

__all__ = ["record_risk_decision"]


def record_risk_decision(
    journal: EventJournal,
    decision: RiskDecision,
    *,
    occurred_at: datetime,
    producer: str = "application.risk",
    event_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    """Append the semantic risk decision and its detailed audit to the journal."""
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
    evaluation = HardRiskEngine.to_phase3_evaluation(decision)
    evaluation = evaluation.model_copy(update={"occurred_at": occurred_at})
    journal.append(semantic)
    journal.append(evaluation)
    return semantic.event_id, evaluation.event_id
