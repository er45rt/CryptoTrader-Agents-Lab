"""Late risk-mark continuity tests: deterministic, offline, NO venue POST."""
from datetime import timedelta
from decimal import Decimal

import pytest

from crypto_trader.crew_host_session import CrewNoSubmitHostSession
from crypto_trader.crew_intent_policy import ApprovedIntentConfig, HostDecisionTimes
from crypto_trader.crew_testnet_host_market import TestnetNoSubmitMarketBinding
from crypto_trader.execution.binance_testnet import TestnetOrderRuntime
from crypto_trader.execution.binance_testnet.market_data import BinanceTestnetTickerQuoteSource
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStateError
from crypto_trader.production.risk_state import RiskDecisionMark
from crypto_trader.production.runtime import CrewOriginAuthority
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    INSTRUMENT, bind_test_admission, build_runtime, canonical_risk_refresh_responses,
    control_for, seed_canonical_equity_root, startup_responses,
)
from crypto_trader.tests.test_r03a1b_no_submit_e2e import _cli_context
from crypto_trader.tests.test_r03a1b_testnet_host_market_bootstrap import NOW, rules
from crypto_trader.tests.test_r03a1b_testnet_ticker_quote import FakeSocket, ticker


def session_with_late_mark(tmp_path, monkeypatch, *, late_bid="26999.00",
                           late_ask="27001.00", late_ms=100, limit=Decimal("50")):
    sockets = []
    def factory():
        index = len(sockets)
        if index == 0:
            event_ms, bid, ask = int(NOW.timestamp()*1000)-500, "26999.00", "27001.00"
        else:
            event_ms, bid, ask = int(NOW.timestamp()*1000)-500+late_ms, late_bid, late_ask
        sock = FakeSocket(ticker(E=event_ms, b=bid, a=ask))
        sockets.append(sock)
        return BinanceTestnetTickerQuoteSource(
            symbol="BTCUSDT", clock=lambda: NOW, max_event_age=timedelta(seconds=5),
            max_spread_bps=Decimal("100"), connect=lambda *_: sock,
        )
    binding = TestnetNoSubmitMarketBinding.capture(
        instrument=INSTRUMENT, venue_rules=rules(), ticker_factory=factory,
    )
    all_responses = [*startup_responses(), *canonical_risk_refresh_responses()]
    del all_responses[-2]  # No untimed REST mark in strict mode.
    journal = SQLiteEventJournal(tmp_path / "continuity.db", venue_writes=True)
    seed_canonical_equity_root(journal)
    transport = fx.ScriptedTransport(all_responses)
    client = fx.client(transport, retries=1)
    port = TestnetOrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    control = control_for(journal, tmp_path / "state", when=NOW)
    risk_source = binding.canonical_risk_source(
        journal=journal, control=control, client=client,
        venue_account_id="123456789", max_decision_mark_move_bps=limit,
    )
    authority = CrewOriginAuthority()
    runtime = build_runtime(
        journal=journal, state_dir=tmp_path / "state", execution=port, control=control,
        risk_state_source=risk_source, market=binding.market_state, when=NOW - timedelta(seconds=1),
        bind_admission=False, require_origin_binding=True,
        trusted_origin_producer="gateway.crew_strategist", origin_authority=authority,
    )
    admission = bind_test_admission(runtime, when=NOW)
    runtime.start()
    # Offline clock transition models capture AFTER startup; no host production wiring is claimed.
    runtime._clock = lambda: NOW
    _cli_context(monkeypatch, tmp_path)
    host = CrewNoSubmitHostSession(
        journal=journal, runtime=runtime, admission=admission,
        authority=authority, producer="gateway.crew_strategist",
        config=ApprovedIntentConfig(
            venue="binance-spot-testnet", symbol="BTCUSDT", strategy_id="offline.e2e",
            approved_notional_quote=Decimal("270"), max_snapshot_age=timedelta(seconds=5),
            intent_valid_for=timedelta(seconds=3),
        ),
        market_quote=binding.quote,
        host_times=HostDecisionTimes(started_at=NOW, completed_at=NOW),
        instrument_filters=binding.instrument_filters, risk_limits=runtime._limits,
        testnet_market_binding=binding,
    )
    evidence = host.run()
    assert admission.offer(evidence.receipt.intent) == (True, "accepted")
    return journal, runtime, admission, transport, sockets, evidence.receipt.intent


def test_actual_preparation_accepts_second_mark_within_approved_movement(tmp_path, monkeypatch):
    journal, runtime, admission, transport, sockets, intent = session_with_late_mark(tmp_path, monkeypatch)
    try:
        prepared = runtime.prepare_intent(intent, _admission_token=admission._runtime_token)
        assert prepared.approved
        assert len(sockets) == 2
        assert all(s.closed and s.read_count == 1 for s in sockets)
        assert not any(request.method == "POST" for request in transport.requests)
    finally:
        journal.close()


def test_actual_preparation_refuses_late_mark_jump_after_validity_pass(tmp_path, monkeypatch):
    journal, runtime, admission, transport, sockets, intent = session_with_late_mark(
        tmp_path, monkeypatch, late_bid="27999.00", late_ask="28001.00",
    )
    try:
        before = journal.count()
        with pytest.raises(ProductionStateError, match="moved beyond approved"):
            runtime.prepare_intent(intent, _admission_token=admission._runtime_token)
        assert journal.count() == before  # No durable peak or admission result after jump.
        assert len(sockets) == 2 and sockets[-1].closed
        assert all(request.method == "GET" for request in transport.requests)
        assert admission.depth() == 1  # Still queued, but never dequeued or submitted.
    finally:
        journal.close()


def test_actual_preparation_refuses_nonmonotonic_exchange_timestamp(tmp_path, monkeypatch):
    journal, runtime, admission, transport, sockets, intent = session_with_late_mark(
        tmp_path, monkeypatch, late_ms=-100,
    )
    try:
        with pytest.raises(ProductionStateError, match="predates"):
            runtime.prepare_intent(intent, _admission_token=admission._runtime_token)
        assert all(request.method == "GET" for request in transport.requests)
        assert len(sockets) == 2
    finally:
        journal.close()


@pytest.mark.parametrize("bad", [None, Decimal("NaN"), Decimal("-1"), Decimal("10001")])
def test_owner_cannot_claim_invalid_strict_movement_limit(tmp_path, bad):
    from crypto_trader.tests.test_production_risk_state_source import seed_baseline
    (tmp_path / "state").mkdir()
    with SQLiteEventJournal(tmp_path / "bad-limit.db") as journal:
        control, _ = seed_baseline(journal, tmp_path / "state")
        # None is legacy opt-out and must remain allowed, but strict host refuses it.
        def reader():
            return BinanceTestnetTickerQuoteSource(
                symbol="BTCUSDT", clock=lambda: NOW,
                max_event_age=timedelta(seconds=5), max_spread_bps=Decimal("100"),
                connect=lambda *_: FakeSocket(ticker()),
            )
        transport = fx.ScriptedTransport([])
        client = fx.client(transport)
        from crypto_trader.production.risk_state import ProductionRiskStateSource
        args=dict(
            journal=journal, control=control, client=client, instrument=INSTRUMENT,
            environment="binance-spot-testnet", venue_account_id="123456789",
            mark_source_factory=reader, max_decision_mark_move_bps=bad,
        )
        if bad is None:
            assert ProductionRiskStateSource(**args)._continuity_max_move_bps is None
        else:
            with pytest.raises(ProductionStateError, match="strict decision-to-mark"):
                ProductionRiskStateSource(**args)
        assert not transport.requests


def test_strict_risk_requires_explicit_intent_reference_before_network(tmp_path):
    from crypto_trader.tests.test_production_risk_state_source import seed_baseline
    from crypto_trader.production.risk_state import ProductionRiskStateSource
    (tmp_path / "state").mkdir()
    with SQLiteEventJournal(tmp_path / "missing-reference.db") as journal:
        control, _ = seed_baseline(journal, tmp_path / "state")
        guard = control.claim_normal_writer(owner_id="normal:runtime", reason_code="test_runtime")
        transport = fx.ScriptedTransport([])
        client = fx.client(transport)
        source = ProductionRiskStateSource(
            journal=journal, control=control, client=client, instrument=INSTRUMENT,
            environment="binance-spot-testnet", venue_account_id="123456789",
            mark_source_factory=lambda: object(), max_decision_mark_move_bps=Decimal("50"),
        )
        with pytest.raises(ProductionStateError, match="authenticated intent market reference"):
            source(NOW, guard=guard)
        assert not transport.requests
