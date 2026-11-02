"""Offline R-03a-1b opt-in causal binding tests; no submission or venue POST."""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from crypto_trader.events import (
    StrategyDecisionRecorded, StrategyDecisionPayload,
    TradeIntentCreated, TradeIntentPayload,
)
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStateError
from crypto_trader.tests.production_fixtures import (
    BASE, bind_test_admission, intent, seed_canonical_equity_root,
    startup_responses,
)
from crypto_trader.tests.test_production_startup_evidence import wired


NOW = BASE + timedelta(seconds=2)


@pytest.fixture
def strict(tmp_path):
    with SQLiteEventJournal(tmp_path / "strict-origin.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        production, _, _, transport = wired(
            journal, tmp_path, startup_responses(),
            when=NOW, bind_admission=False,
            require_origin_binding=True,
            trusted_origin_producer="trusted.r03",
        )
        admission = bind_test_admission(production, when=NOW)
        production.start()
        yield journal, production, admission, transport


def fresh(name="r03a1b-current"):
    return replace(
        intent(name), origin_decision_event_id=uuid4(),
        decision_started_at=NOW, decision_completed_at=NOW, market_event_ts=NOW,
    )


def record_pair(journal, candidate, *, producer="trusted.r03", root_id=None):
    root_id = candidate.origin_decision_event_id if root_id is None else root_id
    journal.append(StrategyDecisionRecorded(
        event_id=root_id, occurred_at=NOW, correlation_id=candidate.correlation_id,
        producer=producer, symbol=candidate.symbol, strategy_id=candidate.strategy_id,
        payload=StrategyDecisionPayload(
            action=candidate.side.value, confidence="high", rationale="offline decision",
        ),
    ))
    journal.append(TradeIntentCreated(
        event_id=candidate.intent_id, occurred_at=NOW,
        correlation_id=candidate.correlation_id, causation_id=root_id,
        producer=producer, symbol=candidate.symbol, strategy_id=candidate.strategy_id,
        payload=TradeIntentPayload(
            side=candidate.side.value, quantity=candidate.quantity,
            order_type=candidate.order_type.value, limit_price=candidate.limit_price,
        ),
    ))


def test_strict_origin_rejects_missing_id_at_ingress(strict):
    journal, production, admission, transport = strict
    candidate = replace(fresh(), origin_decision_event_id=None)
    assert admission.offer(candidate) == (False, "prior_session_intent")
    assert not [request for request in transport.requests if request.method == "POST"]


def test_strict_origin_rejects_missing_journal_lineage_in_real_prepare(strict):
    journal, production, admission, transport = strict
    candidate = fresh()
    assert admission.offer(candidate) == (True, "accepted")
    with pytest.raises(ProductionStateError, match="persisted decision and intent"):
        production.prepare_intent(candidate, _admission_token=admission._runtime_token)
    assert not [request for request in transport.requests if request.method == "POST"]


def test_strict_origin_accepts_verified_new_lineage_without_submission(strict):
    journal, production, admission, transport = strict
    candidate = fresh()
    record_pair(journal, candidate)
    assert admission.offer(candidate) == (True, "accepted")
    production._verify_durable_origin(candidate)
    assert not [request for request in transport.requests if request.method == "POST"]


@pytest.mark.parametrize("mutation", ["wrong_quantity", "wrong_source", "duplicate_source"])
def test_strict_origin_refuses_conflicting_or_reused_lineage(strict, mutation):
    journal, production, admission, transport = strict
    candidate = fresh(mutation)
    record_pair(journal, candidate, producer="untrusted" if mutation == "wrong_source" else "trusted.r03")
    if mutation == "wrong_quantity":
        candidate = replace(candidate, quantity=candidate.quantity * 2)
    if mutation == "duplicate_source":
        journal.append(TradeIntentCreated(
            event_id=uuid4(), occurred_at=NOW,
            correlation_id=candidate.correlation_id,
            causation_id=candidate.origin_decision_event_id,
            producer="trusted.r03", symbol=candidate.symbol,
            strategy_id=candidate.strategy_id,
            payload=TradeIntentPayload(
                side=candidate.side.value, quantity=candidate.quantity,
                order_type=candidate.order_type.value, limit_price=candidate.limit_price,
            ),
        ))
    with pytest.raises(ProductionStateError, match="conflicting|competing"):
        production._verify_durable_origin(candidate)
    assert not [request for request in transport.requests if request.method == "POST"]
