"""R-03a-1b RED candidate: origin identity is not equivalent to an intent UUID.

OFFLINE ONLY: scripted startup transport, no POST invocation, no real venue client.
This test is a candidate design; it is not counted as failing until pytest runs.
"""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from crypto_trader.events import (
    TradeIntentCreated, TradeIntentPayload,
    StrategyDecisionRecorded, StrategyDecisionPayload,
)
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    BASE, intent, seed_canonical_equity_root, bind_test_admission,
    startup_responses,
)
from crypto_trader.tests.test_production_startup_evidence import wired
from crypto_trader.validity import MarketObservation, ReplayMarketStateProvider


def test_r03a1b_reissued_origin_with_fresh_uuid_cannot_enter_live_admission(tmp_path):
    old = replace(intent("r03a1b-reissued-same-origin"),
                  origin_decision_event_id=uuid4())
    path = tmp_path / "source-provenance.db"
    with SQLiteEventJournal(path, venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        journal.append(StrategyDecisionRecorded(
            event_id=old.origin_decision_event_id, occurred_at=BASE,
            correlation_id=old.correlation_id, producer="offline.r03",
            symbol=old.symbol, strategy_id=old.strategy_id,
            payload=StrategyDecisionPayload(
                action=old.side.value, confidence="high", rationale="source decision",
            ),
        ))
        journal.append(TradeIntentCreated(
            event_id=old.intent_id, occurred_at=BASE,
            correlation_id=old.correlation_id,
            causation_id=old.origin_decision_event_id,
            producer="offline.r03", symbol=old.symbol, strategy_id=old.strategy_id,
            payload=TradeIntentPayload(
                side=old.side.value, quantity=old.quantity,
                order_type=old.order_type.value, limit_price=old.limit_price,
            ),
        ))

    now = BASE + timedelta(seconds=2)
    # The source decision is NOT new. The hostile redelivery changes its UUID and
    # timestamps while claiming the same original market_reference and economics.
    redelivery = replace(
        old, intent_id=uuid4(), decision_started_at=now,
        decision_completed_at=now, market_event_ts=now,
    )
    market = ReplayMarketStateProvider([MarketObservation(
        symbol=old.symbol, price=old.reference_price,
        market_event_ts=now, local_ingest_ts=now,
        market_reference=old.market_reference, sequence=2,
    )])
    with SQLiteEventJournal(path, venue_writes=True) as journal:
        production, _, _, transport = wired(
            journal, tmp_path, startup_responses(),
            when=now, market=market, bind_admission=False,
            require_origin_binding=True, trusted_origin_producer="offline.r03",
        )
        admission = bind_test_admission(production, when=now)
        production.start()
        accepted, reason = admission.offer(redelivery)
        assert accepted is False, (
            "R-03a-1b: a reused decision origin must not become a new "
            f"venue-writable intent by replacing UUID4; observed {reason!r}"
        )
        assert not [request for request in transport.requests if request.method == "POST"]
