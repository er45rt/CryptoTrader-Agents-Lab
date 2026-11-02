"""Offline end-to-end Testnet market/bootstrap evidence; no POST and no credentials."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from crypto_trader.crew_testnet_host_market import (
    TestnetHostMarketBindingError, TestnetNoSubmitMarketBinding,
)
from crypto_trader.crew_host_session import CrewNoSubmitHostSession, HostNoSubmitEvidence
from crypto_trader.crew_intent_policy import ApprovedIntentConfig, HostDecisionTimes
from crypto_trader.execution.binance_testnet.market_data import BinanceTestnetTickerQuoteSource
from crypto_trader.execution.binance_testnet.models import TestnetSymbolRules
from crypto_trader.execution.binance_testnet import TestnetOrderRuntime
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStateError
from crypto_trader.production.risk_state import RiskDecisionMark
from crypto_trader.production.runtime import CrewOriginAuthority
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    INSTRUMENT, build_runtime, bind_test_admission, control_for,
    canonical_risk_refresh_responses, seed_canonical_equity_root, startup_responses,
)
from crypto_trader.tests.test_r03a1b_no_submit_e2e import _cli_context, host
from crypto_trader.tests.test_r03a1b_testnet_ticker_quote import FakeSocket, ticker

NOW = datetime.fromtimestamp(fx.SERVER_TIME_MS / 1000, tz=timezone.utc)


def rules(*, symbol="BTCUSDT", base="BTC"):
    return TestnetSymbolRules(
        symbol=symbol, status="TRADING", base_asset=base, quote_asset="USDT",
        tick_size=Decimal("0.01"), step_size=Decimal("0.0001"),
        min_quantity=Decimal("0.0001"), max_quantity=Decimal("100"),
        min_notional=Decimal("5"),
    )


def readers(*, corrupt_at=None):
    sockets = []
    def factory():
        ordinal = len(sockets)
        frame = ticker(
            E=int(NOW.timestamp() * 1000) - 500 + ordinal * 100,
            a="27001.00",
        )
        socket = FakeSocket(frame, fails=(ordinal == corrupt_at))
        sockets.append(socket)
        return BinanceTestnetTickerQuoteSource(
            symbol="BTCUSDT", clock=lambda: NOW,
            max_event_age=timedelta(seconds=5),
            max_spread_bps=Decimal("100"), connect=lambda *_: socket,
        )
    return factory, sockets


def capture(factory):
    return TestnetNoSubmitMarketBinding.capture(
        instrument=INSTRUMENT, venue_rules=rules(), ticker_factory=factory,
    )


def test_market_bootstrap_returns_single_provenance_lineage_with_future_refresh_capability():
    factory, sockets = readers()
    binding = capture(factory)
    assert len(sockets) == 1 and sockets[0].closed
    assert binding.quote.reference_id == binding.observation.market_reference
    assert binding.quote.event_ts == binding.observation.market_event_ts
    assert binding.market_state.latest("BTCUSDT") is binding.observation
    assert binding.instrument_filters.symbol == binding.quote.symbol
    assert binding.new_risk_reader().symbol == "BTCUSDT"
    assert len(sockets) == 2


@pytest.mark.parametrize("bad", [
    rules(symbol="ETHUSDT"), rules(base="ETH"), object(),
])
def test_bad_rules_refuse_before_any_network_read(bad):
    factory, sockets = readers()
    with pytest.raises(TestnetHostMarketBindingError):
        TestnetNoSubmitMarketBinding.capture(
            instrument=INSTRUMENT, venue_rules=bad, ticker_factory=factory,
        )
    assert not sockets


@pytest.mark.parametrize("bad_factory", [
    lambda: object(),
    lambda: BinanceTestnetTickerQuoteSource(
        symbol="ETHUSDT", clock=lambda: NOW,
        max_event_age=timedelta(seconds=5), max_spread_bps=Decimal("100"),
    ),
])
def test_untrusted_initial_reader_refused_without_network(bad_factory):
    with pytest.raises(TestnetHostMarketBindingError, match="pinned"):
        capture(bad_factory)


def test_host_session_uses_prestart_bound_market_and_refreshes_canonical_risk_twice(
    tmp_path, monkeypatch,
):
    factory, sockets = readers()
    binding = capture(factory)
    responses = [
        *startup_responses(),
        *canonical_risk_refresh_responses(),
        *canonical_risk_refresh_responses(),
    ]
    # Strict mark factory consumes zero untimed REST prices; remove those scripted responses.
    del responses[-2]
    del responses[-6]
    with SQLiteEventJournal(tmp_path / "session.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        transport = fx.ScriptedTransport(responses)
        client = fx.client(transport, retries=1)
        port = TestnetOrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
        control = control_for(journal, tmp_path / "state", when=NOW)
        risk_source = binding.canonical_risk_source(
            journal=journal, control=control, client=client,
            venue_account_id="123456789",
            max_decision_mark_move_bps=Decimal("50"),
        )
        authority = CrewOriginAuthority()
        runtime = build_runtime(
            journal=journal, state_dir=tmp_path / "state",
            execution=port, control=control, risk_state_source=risk_source,
            market=binding.market_state, when=NOW, bind_admission=False,
            require_origin_binding=True,
            trusted_origin_producer="gateway.crew_strategist",
            origin_authority=authority,
        )
        admission = bind_test_admission(runtime, when=NOW)
        runtime.start()
        _cli_context(monkeypatch, tmp_path)
        host = CrewNoSubmitHostSession(
            journal=journal, runtime=runtime, admission=admission,
            authority=authority, producer="gateway.crew_strategist",
            config=ApprovedIntentConfig(
                venue="binance-spot-testnet", symbol="BTCUSDT",
                strategy_id="offline.e2e", approved_notional_quote=Decimal("270"),
                max_snapshot_age=timedelta(seconds=5),
                intent_valid_for=timedelta(seconds=3),
            ),
            market_quote=binding.quote,
            host_times=HostDecisionTimes(started_at=NOW, completed_at=NOW),
            instrument_filters=binding.instrument_filters,
            risk_limits=runtime._limits,
            testnet_market_binding=binding,
        )
        evidence = host.run()
        assert type(evidence) is HostNoSubmitEvidence
        assert evidence.receipt.intent.market_reference == binding.quote.reference_id
        assert admission.depth() == 0
        decision_mark = RiskDecisionMark(
            symbol="BTCUSDT", reference_price=binding.quote.best_ask,
            event_ts=binding.quote.event_ts, reference_id=binding.quote.reference_id,
        )
        one, _ = risk_source(NOW, guard=runtime._guard, decision_mark=decision_mark)
        two, _ = risk_source(NOW, guard=runtime._guard, decision_mark=decision_mark)
        assert one.equity_quote == two.equity_quote
        assert len(sockets) == 3 and len({id(s) for s in sockets}) == 3
        assert all(s.closed and s.read_count == 1 for s in sockets)
        assert not any(req.method == "POST" for req in transport.requests)


def test_strict_host_refuses_runtime_with_unrelated_market_or_rest_risk_source(host):
    journal, authority, runtime, admission, transport, _ = host
    factory, sockets = readers()
    binding = capture(factory)
    with pytest.raises(ProductionStateError, match="pre-start bound market"):
        CrewNoSubmitHostSession(
            journal=journal, runtime=runtime, admission=admission,
            authority=authority, producer="gateway.crew_strategist",
            config=ApprovedIntentConfig(
                venue="binance-spot-testnet", symbol="BTCUSDT",
                strategy_id="offline.e2e", approved_notional_quote=Decimal("270"),
                max_snapshot_age=timedelta(seconds=5),
                intent_valid_for=timedelta(seconds=3),
            ),
            market_quote=binding.quote,
            host_times=HostDecisionTimes(started_at=NOW, completed_at=NOW),
            instrument_filters=binding.instrument_filters,
            risk_limits=runtime._limits,
            testnet_market_binding=binding,
        )
    assert len(sockets) == 1 and sockets[0].closed
    assert not any(req.method == "POST" for req in transport.requests)


def test_factory_failure_on_second_refresh_never_retries_or_uses_rest(tmp_path):
    from crypto_trader.tests.test_production_risk_state_source import seed_baseline, source, refresh_without_untimed_price
    factory, sockets = readers(corrupt_at=2)
    binding = capture(factory)
    (tmp_path / "state").mkdir()
    with SQLiteEventJournal(tmp_path / "risk.db") as journal:
        control, _ = seed_baseline(journal, tmp_path / "state")
        guard = control.claim_normal_writer(owner_id="normal:runtime", reason_code="test_runtime")
        risk_source, transport = source(
            journal, control, refresh_without_untimed_price() * 2,
        )
        # Use the factory through the canonical public constructor; never mutate private providers.
        risk_source = binding.canonical_risk_source(
            journal=journal, control=control, client=risk_source._client,
            venue_account_id="123456789",
        )
        first, _ = risk_source(NOW, guard=guard)
        with pytest.raises(ProductionStateError, match="venue risk evidence refresh failed"):
            risk_source(NOW, guard=guard)
        assert first.equity_quote > 0
        assert len(sockets) == 3 and sockets[-1].closed
        assert all(req.method == "GET" for req in transport.requests)
        assert len(transport.requests) == 7
