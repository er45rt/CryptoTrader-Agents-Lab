"""Host-owned one-shot Crew session; isolated/no credentials/no venue POST."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from crypto_trader import main as cli
from crypto_trader.crew_decision_gateway import CrewAdvisoryReceipt
from crypto_trader.crew_host_session import CrewNoSubmitHostSession, HostNoSubmitEvidence
from crypto_trader.crew_intent_policy import (
    ApprovedIntentConfig, HostDecisionTimes, InstrumentFilters, VerifiedMarketQuote,
)
from crypto_trader.production.errors import ProductionStateError
from crypto_trader.tests.test_r03a1b_crew_gateway import plan
from crypto_trader.tests.test_r03a1b_no_submit_e2e import _cli_context, host
from crypto_trader.tests.test_r03a1b_origin_binding import NOW, fresh


def session(host, *, stale=False, excess=False, halted=False):
    journal, authority, runtime, admission, _transport, _output_dir = host
    if halted:
        runtime._limits = replace(runtime._limits, trading_halted=True)
    limits = runtime._limits
    budget = min(
        limits.max_order_notional_quote,
        limits.max_symbol_exposure_quote,
        limits.max_gross_exposure_quote,
        limits.max_net_exposure_quote,
        limits.max_strategy_exposure_quote,
    ) + Decimal("1") if excess else Decimal("270")
    return CrewNoSubmitHostSession(
        journal=journal, runtime=runtime, admission=admission, authority=authority,
        producer="gateway.crew_strategist",
        config=ApprovedIntentConfig(
            venue="BINANCE", symbol="BTCUSDT", strategy_id="offline.e2e",
            approved_notional_quote=budget, max_snapshot_age=timedelta(seconds=8),
            intent_valid_for=timedelta(seconds=5),
        ),
        market_quote=VerifiedMarketQuote(
            venue="BINANCE", symbol="BTCUSDT",
            reference_id="host-session-offline-quote-1",
            event_ts=NOW - timedelta(seconds=12) if stale else NOW,
            received_at=NOW, best_bid=Decimal("26999"), best_ask=Decimal("27000"),
        ),
        host_times=HostDecisionTimes(started_at=NOW, completed_at=NOW),
        instrument_filters=InstrumentFilters(
            venue="BINANCE", symbol="BTCUSDT", tick_size=Decimal("0.01"),
            quantity_step=Decimal("0.0001"), minimum_quantity=Decimal("0.0001"),
            minimum_notional_quote=Decimal("5"),
        ),
        risk_limits=limits,
    )


def _no_post(host):
    return not any(req.method == "POST" for req in host[4].requests)


def test_host_session_validates_main_receipt_and_leaves_inert_evidence(host, monkeypatch):
    _cli_context(monkeypatch, host[5])
    before = {e.event_id for e in host[0].read_range()}
    result = session(host).run()
    assert type(result) is HostNoSubmitEvidence
    assert result.receipt.raw_plan == plan()
    assert result.notional_quote == Decimal("270")
    assert result.receipt.intent.intent_id == result.intent_event_id
    assert result.receipt.intent.origin_decision_event_id == result.decision_event_id
    assert {e.event_id for e in host[0].read_range()} - before == {
        result.intent_event_id, result.decision_event_id,
    }
    assert host[3].depth() == 0
    assert host[3].active_intent_id() is None
    assert _no_post(host)


def test_host_session_is_one_shot_and_fails_before_second_kickoff(host, monkeypatch):
    _cli_context(monkeypatch, host[5])
    session_object = session(host)
    session_object.run()
    state = tuple(host[0].read_range())
    with pytest.raises(ProductionStateError, match="replayed"):
        session_object.run()
    assert tuple(host[0].read_range()) == state
    assert host[3].depth() == 0 and _no_post(host)


@pytest.mark.parametrize("invalid_facts", ["stale", "budget", "halted"])
def test_host_fails_closed_for_untrusted_or_excessive_facts(host, monkeypatch, invalid_facts):
    _cli_context(monkeypatch, host[5])
    state = tuple(host[0].read_range())
    candidate = session(
        host, stale=invalid_facts == "stale",
        excess=invalid_facts == "budget",
        halted=invalid_facts == "halted",
    )
    with pytest.raises(ProductionStateError, match="Crew advisory failed"):
        candidate.run()
    assert tuple(host[0].read_range()) == state
    assert host[3].depth() == 0 and _no_post(host)


def test_host_refuses_synthetic_receipt_without_host_delivery_ledger(host, monkeypatch):
    fake = CrewAdvisoryReceipt(
        raw_plan=plan(), intent=fresh("synthetic-origin"),
        source_delivery_id=__import__("uuid").uuid4(),
    )
    monkeypatch.setattr(cli, "run", lambda **_kwargs: fake)
    before = tuple(host[0].read_range())
    with pytest.raises(ProductionStateError, match="trusted|finalized"):
        session(host).run()
    assert tuple(host[0].read_range()) == before
    assert host[3].depth() == 0 and _no_post(host)


def test_host_rejects_non_receipt_without_accepting_as_evidence(host, monkeypatch):
    monkeypatch.setattr(cli, "run", lambda **_kwargs: plan())
    before = tuple(host[0].read_range())
    with pytest.raises(ProductionStateError, match="typed"):
        session(host).run()
    assert tuple(host[0].read_range()) == before
    assert host[3].depth() == 0 and _no_post(host)


def test_host_rejects_unbound_admission_before_gateway_binding(host):
    journal, authority, runtime, _admission, _transport, _output_dir = host
    with pytest.raises(ProductionStateError, match="bound"):
        CrewNoSubmitHostSession(
            journal=journal, runtime=runtime, admission=object(),
            authority=authority, producer="gateway.crew_strategist",
            config=object(), market_quote=object(), host_times=object(),
            instrument_filters=object(), risk_limits=runtime._limits,
        )
    assert _no_post(host)


def test_host_rejects_nonempty_ingress_before_crewai_kickoff(host, monkeypatch):
    _cli_context(monkeypatch, host[5])
    obj = session(host)
    assert host[3].offer(fresh("queued-host-preexisting"))[0]
    prior = tuple(host[0].read_range())
    with pytest.raises(ProductionStateError, match="nonempty"):
        obj.run()
    assert tuple(host[0].read_range()) == prior
    assert host[3].depth() == 1 and _no_post(host)
