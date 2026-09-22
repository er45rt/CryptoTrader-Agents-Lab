"""Phase 7/10 regression: the deterministic risk decision must actually journal.

`HardRiskEngine.to_phase3_evaluation` previously built the additive audit event without the
mandatory envelope `occurred_at`, so `record_risk_decision` raised a pydantic validation error and
was never exercised. Phase 10 became the first caller. These tests pin the fixed behaviour.
"""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.risk import (
    HardRiskEngine,
    RiskConfigError,
    RiskRequest,
    RiskStateSnapshot,
    Side,
    load_limits,
    record_risk_decision,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

LIMIT_CONFIG = {
    "limits_version": "risk-v1",
    "max_order_notional_quote": "5000",
    "max_symbol_exposure_quote": "20000",
    "max_gross_exposure_quote": "50000",
    "max_net_exposure_quote": "30000",
    "max_strategy_exposure_quote": "40000",
    "loss_limit_quote": "1000",
    "max_drawdown_pct": "10",
    "concentration_limit_pct": "50",
    "max_open_orders": 5,
    "max_open_positions": 3,
}


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as instance:
        yield instance


def decision(*, quantity: str = "1") -> object:
    engine = HardRiskEngine(load_limits(LIMIT_CONFIG))
    request = RiskRequest(
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        side=Side.BUY,
        quantity=Decimal(quantity),
        price=Decimal("2700"),
        strategy_id="s1",
        intent_id=uuid4(),
    )
    state = RiskStateSnapshot(
        equity_quote=Decimal("10000"),
        peak_equity_quote=Decimal("10000"),
        realized_pnl_quote=Decimal("0"),
        gross_exposure_quote=Decimal("0"),
        net_exposure_quote=Decimal("0"),
        open_orders=0,
        open_positions=0,
        snapshot_ts=BASE,
    )
    return engine.evaluate(request, state, now=BASE)


def test_to_phase3_evaluation_requires_a_timestamp():
    with pytest.raises(RiskConfigError):
        HardRiskEngine.to_phase3_evaluation(decision(), occurred_at=datetime(2026, 1, 1))


def test_to_phase3_evaluation_carries_the_supplied_timestamp():
    event = HardRiskEngine.to_phase3_evaluation(decision(), occurred_at=BASE)

    assert event.occurred_at == BASE
    assert event.event_type == "risk.evaluation.recorded"
    assert event.payload.decision == "allowed"


def test_record_risk_decision_appends_both_events(journal):
    result = decision()

    semantic_id, evaluation_id = record_risk_decision(
        journal, result, occurred_at=BASE
    )

    assert semantic_id != evaluation_id
    assert journal.count() == 2
    semantic = journal.read_by_id(semantic_id)
    evaluation = journal.read_by_id(evaluation_id)
    assert semantic.event_type == "risk.decision.recorded"
    assert semantic.payload.approved is True
    assert semantic.occurred_at == BASE
    assert evaluation.event_type == "risk.evaluation.recorded"
    assert evaluation.occurred_at == BASE
    assert evaluation.payload.limits_version == "risk-v1"
    assert evaluation.payload.rule_results


def test_record_risk_decision_is_idempotent_by_event_id(journal):
    result = decision()
    semantic_id = uuid4()
    evaluation_id = uuid4()

    record_risk_decision(
        journal,
        result,
        occurred_at=BASE,
        event_id=semantic_id,
        evaluation_event_id=evaluation_id,
    )
    record_risk_decision(
        journal,
        result,
        occurred_at=BASE,
        event_id=semantic_id,
        evaluation_event_id=evaluation_id,
    )

    assert journal.count() == 2


def test_rejected_decision_is_journaled_with_its_reason(journal):
    result = decision(quantity="10")
    assert result.allowed is False

    semantic_id, _ = record_risk_decision(journal, result, occurred_at=BASE)
    semantic = journal.read_by_id(semantic_id)

    assert semantic.payload.approved is False
    assert "max_order_notional_exceeded" in semantic.payload.reason
    assert semantic.payload.approved_quantity is None
