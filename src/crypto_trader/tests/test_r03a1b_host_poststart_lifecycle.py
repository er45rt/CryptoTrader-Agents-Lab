"""Post-start Testnet host market lifecycle — offline, no order submission."""
from datetime import timedelta
from decimal import Decimal

import pytest

from crypto_trader.crew_testnet_host_market import (
    TestnetHostMarketBindingError, TestnetPostStartMarketBootstrap,
)
from crypto_trader.crew_host_session import CrewNoSubmitHostSession
from crypto_trader.crew_intent_policy import ApprovedIntentConfig, HostDecisionTimes
from crypto_trader.execution.binance_testnet import TestnetOrderRuntime
from crypto_trader.execution.binance_testnet.market_data import BinanceTestnetTickerQuoteSource, TestnetTickerQuoteError
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production.runtime import CrewOriginAuthority
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    INSTRUMENT, bind_test_admission, build_runtime, control_for,
    canonical_risk_refresh_responses, seed_canonical_equity_root, startup_responses,
)
from crypto_trader.tests.test_r03a1b_no_submit_e2e import _cli_context
from crypto_trader.tests.test_r03a1b_testnet_host_market_bootstrap import NOW, rules
from crypto_trader.tests.test_r03a1b_testnet_ticker_quote import FakeSocket, ticker


def new_bootstrap(*, moved=False, event_offset_ms=0):
    sockets = []
    def factory():
        is_risk = bool(sockets)
        bid = "27999.00" if moved and is_risk else "26999.00"
        ask = "28001.00" if moved and is_risk else "27001.00"
        socket = FakeSocket(ticker(
            E=int(NOW.timestamp()*1000)+event_offset_ms,
            b=bid, a=ask,
        ))
        sockets.append(socket)
        return BinanceTestnetTickerQuoteSource(
            symbol=INSTRUMENT.symbol, clock=lambda: NOW,
            max_event_age=timedelta(seconds=5), max_spread_bps=Decimal("100"),
            connect=lambda *_args: socket,
        )
    return TestnetPostStartMarketBootstrap(
        instrument=INSTRUMENT, venue_rules=rules(), ticker_factory=factory,
    ), sockets


def prepare(tmp_path, monkeypatch, *, moved=False, event_offset_ms=0):
    bootstrap, sockets = new_bootstrap(moved=moved, event_offset_ms=event_offset_ms)
    assert bootstrap.market_state.latest("BTCUSDT") is None
    responses = [*startup_responses(), *canonical_risk_refresh_responses()]
    del responses[-2]  # No untimed REST mark when strict risk binding is configured.
    journal = SQLiteEventJournal(tmp_path / "poststart.db", venue_writes=True)
    seed_canonical_equity_root(journal)
    transport = fx.ScriptedTransport(responses)
    client = fx.client(transport, retries=1)
    port = TestnetOrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    control = control_for(journal, tmp_path / "state", when=NOW)
    risk_source = bootstrap.canonical_risk_source(
        journal=journal, control=control, client=client,
        venue_account_id="123456789", max_decision_mark_move_bps=Decimal("50"),
    )
    authority = CrewOriginAuthority()
    runtime = build_runtime(
        journal=journal, state_dir=tmp_path / "state", execution=port,
        control=control, risk_state_source=risk_source,
        market=bootstrap.market_state, when=NOW, bind_admission=False,
        require_origin_binding=True,
        trusted_origin_producer="gateway.crew_strategist",
        origin_authority=authority,
    )
    admission = bind_test_admission(runtime, when=NOW)
    report = runtime.start()
    assert report.started_at <= NOW
    assert bootstrap.market_state.latest("BTCUSDT") is None
    binding = bootstrap.capture_after_start(runtime)
    assert binding.observation.market_event_ts >= report.started_at
    assert binding.market_state.latest("BTCUSDT") is binding.observation
    _cli_context(monkeypatch, tmp_path)
    host = CrewNoSubmitHostSession(
        journal=journal, runtime=runtime, admission=admission,
        authority=authority, producer="gateway.crew_strategist",
        config=ApprovedIntentConfig(
            venue="binance-spot-testnet", symbol="BTCUSDT", strategy_id="offline.e2e",
            approved_notional_quote=Decimal("270"),
            max_snapshot_age=timedelta(seconds=5), intent_valid_for=timedelta(seconds=3),
        ),
        market_quote=binding.quote,
        host_times=HostDecisionTimes(started_at=NOW, completed_at=NOW),
        instrument_filters=binding.instrument_filters,
        risk_limits=runtime._limits, testnet_market_binding=binding,
    )
    return journal, runtime, admission, host, transport, sockets, bootstrap


def test_authenticated_quote_capture_occurs_after_real_runtime_start_and_passes_admission(
    tmp_path, monkeypatch,
):
    journal, runtime, admission, host, transport, sockets, bootstrap = prepare(tmp_path, monkeypatch)
    try:
        evidence = host.run()
        intent = evidence.receipt.intent
        assert admission.offer(intent) == (True, "accepted")
        prepared = runtime.prepare_intent(intent, _admission_token=admission._runtime_token)
        assert prepared.approved
        assert len(sockets) == 2 and all(x.closed and x.read_count == 1 for x in sockets)
        assert not any(x.method == "POST" for x in transport.requests)
        with pytest.raises(TestnetHostMarketBindingError, match="single-use"):
            bootstrap.capture_after_start(runtime)
    finally:
        journal.close()


def test_authenticated_poststart_session_refuses_late_risk_price_jump(tmp_path, monkeypatch):
    journal, runtime, admission, host, transport, sockets, bootstrap = prepare(
        tmp_path, monkeypatch, moved=True,
    )
    try:
        evidence = host.run()
        intent = evidence.receipt.intent
        assert admission.offer(intent) == (True, "accepted")
        with pytest.raises(Exception, match="moved beyond approved"):
            runtime.prepare_intent(intent, _admission_token=admission._runtime_token)
        assert len(sockets) == 2
        assert not any(x.method == "POST" for x in transport.requests)
    finally:
        journal.close()


def test_capture_refuses_before_runtime_start_without_consuming_socket(tmp_path):
    bootstrap, sockets = new_bootstrap()
    with pytest.raises(TestnetHostMarketBindingError, match="runtime must be started"):
        bootstrap.capture_after_start(object())
    assert not sockets
    assert bootstrap.market_state.latest("BTCUSDT") is None


@pytest.mark.parametrize(("event_offset_ms", "error_type", "pattern"), [
    pytest.param(-1000, TestnetHostMarketBindingError, "predates", id="pre_session"),
    pytest.param(1000, TestnetTickerQuoteError, "future-dated or stale", id="future"),
])
def test_capture_does_not_retry_future_or_pre_session_market_message(
    tmp_path, monkeypatch, event_offset_ms, error_type, pattern,
):
    # Risk bootstrap can be installed before a live session; a bad first frame never
    # becomes a newly minted price just by consuming the clock at a later point.
    bootstrap, sockets = new_bootstrap(event_offset_ms=event_offset_ms)
    with SQLiteEventJournal(tmp_path / "bad-start.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        transport = fx.ScriptedTransport(startup_responses())
        client = fx.client(transport, retries=1)
        port = TestnetOrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
        control = control_for(journal, tmp_path / "state", when=NOW)
        risk_source = bootstrap.canonical_risk_source(
            journal=journal, control=control, client=client,
            venue_account_id="123456789", max_decision_mark_move_bps=Decimal("50"),
        )
        runtime = build_runtime(
            journal=journal, state_dir=tmp_path / "state", execution=port,
            control=control, risk_state_source=risk_source, market=bootstrap.market_state,
            when=NOW, bind_admission=False,
        )
        bind_test_admission(runtime, when=NOW)
        runtime.start()
        with pytest.raises(error_type, match=pattern):
            bootstrap.capture_after_start(runtime)
        with pytest.raises(TestnetHostMarketBindingError, match="single-use"):
            bootstrap.capture_after_start(runtime)
        assert len(sockets) == 1 and sockets[0].closed
        assert bootstrap.market_state.latest("BTCUSDT") is None
        assert all(x.method == "GET" for x in transport.requests)



@pytest.mark.parametrize("tamper", ("disable_continuity", "same_owner_wrong_factory"))
def test_strict_host_revalidates_market_provenance_on_run(tmp_path, monkeypatch, tamper):
    """Fail closed if strict market provenance is altered after host construction."""
    from crypto_trader.production import ProductionStateError

    journal, runtime, admission, host, transport, sockets, bootstrap = prepare(
        tmp_path, monkeypatch,
    )
    try:
        risk = runtime._risk_state_provider
        if tamper == "disable_continuity":
            risk._continuity_max_move_bps = None
        else:
            # This has the SAME __self__ but the WRONG __func__.
            risk._mark_source_factory = bootstrap.canonical_risk_source
        with pytest.raises(ProductionStateError, match="strict Testnet host requires"):
            host.run()
        assert admission.depth() == 0
        assert not any(request.method == "POST" for request in transport.requests)
    finally:
        journal.close()