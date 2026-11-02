"""Isolated NO-SUBMIT Crew -> persisted origin -> admission/validity/risk proof.

No credentials, venue network, or real POST. The host mocks all market facts.
This is an opt-in composition test, NOT proof that default main.run() is wired.
"""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from crypto_trader import main as cli
from crypto_trader.crew_decision_gateway import CrewDecisionGateway
from crypto_trader.crew_intent_policy import (
    ApprovedIntentConfig, HostDecisionTimes, InstrumentFilters,
    VerifiedMarketQuote, make_policy,
)
from crypto_trader.events import StrategyDecisionRecorded, TradeIntentCreated
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production.runtime import CrewOriginAuthority
from crypto_trader.risk import RiskLimits
from crypto_trader.tests.production_fixtures import (
    bind_test_admission, canonical_risk_refresh_responses,
    seed_canonical_equity_root, startup_responses,
)
from crypto_trader.tests.test_production_startup_evidence import wired
from crypto_trader.tests.test_r03a1b_crew_gateway import plan
from crypto_trader.tests.test_r03a1b_origin_binding import NOW


@pytest.fixture
def host(tmp_path):
    with SQLiteEventJournal(tmp_path / "e2e-no-submit.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        authority = CrewOriginAuthority()
        runtime, _, _, transport = wired(
            journal, tmp_path,
            [*startup_responses(), *canonical_risk_refresh_responses()],
            when=NOW, bind_admission=False, require_origin_binding=True,
            trusted_origin_producer="gateway.crew_strategist",
            origin_authority=authority,
        )
        admission = bind_test_admission(runtime, when=NOW)
        runtime.start()
        yield journal, authority, runtime, admission, transport, tmp_path


def _policy(*, stale=False, excessive=False):
    limits = RiskLimits(
        limits_version="e2e-offline", max_order_notional_quote=Decimal("500"),
        max_symbol_exposure_quote=Decimal("600"), max_gross_exposure_quote=Decimal("700"),
        max_net_exposure_quote=Decimal("700"), max_strategy_exposure_quote=Decimal("500"),
        loss_limit_quote=Decimal("300"), max_drawdown_pct=Decimal("10"),
        concentration_limit_pct=Decimal("50"), max_open_orders=2, max_open_positions=2,
    )
    return make_policy(
        config=ApprovedIntentConfig(
            venue="BINANCE", symbol="BTCUSDT", strategy_id="offline.e2e",
            approved_notional_quote=Decimal("600" if excessive else "270"),
            max_snapshot_age=timedelta(seconds=8),
            intent_valid_for=timedelta(seconds=5),
        ),
        market_quote=VerifiedMarketQuote(
            venue="BINANCE", symbol="BTCUSDT", reference_id="host-offline-quote-19",
            event_ts=NOW - timedelta(seconds=12) if stale else NOW,
            received_at=NOW,
            best_bid=Decimal("26999"), best_ask=Decimal("27000"),
        ),
        host_times=HostDecisionTimes(
            started_at=NOW, completed_at=NOW,
        ),
        instrument_filters=InstrumentFilters(
            venue="BINANCE", symbol="BTCUSDT", tick_size=Decimal("0.01"),
            quantity_step=Decimal("0.0001"), minimum_quantity=Decimal("0.0001"),
            minimum_notional_quote=Decimal("5"),
        ),
        risk_limits=limits,
    )


def _run_mocked_crew(monkeypatch, output_dir, gateway):
    class OfflineCrew:
        def crew(self):
            return self

        def kickoff(self, *, inputs):
            assert inputs["target_symbol"] == "BTCUSDT"
            return plan()

    monkeypatch.setattr(cli, "CryptoTraderCrew", OfflineCrew)
    return cli.run_crew(
        {"target_symbol": "BTCUSDT", "filename_datetime": "unused"},
        output_dir, advisory_gateway=gateway,
    )


def test_offline_crew_host_policy_admission_and_risk_prepare_never_submit(host, monkeypatch):
    journal, authority, runtime, admission, transport, output_dir = host
    baseline = {event.event_id for event in journal.read_range()}
    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist", policy=_policy(),
    )
    assert _run_mocked_crew(monkeypatch, output_dir, gateway) == plan()
    additions = [event for event in journal.read_range() if event.event_id not in baseline]
    assert len(additions) == 2
    assert {type(event) for event in additions} == {
        StrategyDecisionRecorded, TradeIntentCreated,
    }
    assert len(authority._tickets) == 1  # Test-only observation, not public bootstrap API.
    issued = next(iter(authority._tickets.values()))[0]
    assert issued.quantity == Decimal("0.0100")
    assert issued.limit_price == Decimal("27000.00")
    assert admission.offer(issued) == (True, "accepted")
    prepared = runtime.prepare_intent(
        issued, _admission_token=admission._runtime_token,
    )
    assert prepared.approved
    assert not any(request.method == "POST" for request in transport.requests)


@pytest.mark.parametrize("case", ["stale_quote", "over_budget"])
def test_invalid_host_facts_cannot_issue_intent_or_reach_venue(host, monkeypatch, case):
    journal, authority, runtime, admission, transport, output_dir = host
    before = tuple(journal.read_range())
    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist",
        policy=_policy(stale=case == "stale_quote",
                       excessive=case == "over_budget"),
    )
    assert _run_mocked_crew(monkeypatch, output_dir, gateway) is None
    assert tuple(journal.read_range()) == before
    assert not authority._tickets
    assert admission.depth() == 0
    assert not any(request.method == "POST" for request in transport.requests)

def _cli_context(monkeypatch, output_dir):
    from types import SimpleNamespace

    class OfflineCrew:
        def crew(self):
            return self

        def kickoff(self, *, inputs):
            assert inputs["target_symbol"] == "BTCUSDT"
            return plan()

    monkeypatch.setattr(cli, "CryptoTraderCrew", OfflineCrew)
    monkeypatch.setattr(cli, "parse_arguments", lambda: SimpleNamespace(
        symbol="BTCUSDT", interval="4h", limit=72, output="offline", verbose=False,
    ))
    monkeypatch.setattr(cli, "setup_environment", lambda: True)
    monkeypatch.setattr(cli, "prepare_output_directory", lambda _config: output_dir)
    monkeypatch.setattr(cli, "create_inputs", lambda _config: {
        "target_symbol": "BTCUSDT", "filename_datetime": "offline",
    })


def test_main_optin_uses_trusted_gateway_without_any_order_post(host, monkeypatch):
    journal, authority, runtime, _admission, transport, output_dir = host
    before = {event.event_id for event in journal.read_range()}
    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist", policy=_policy(),
    )
    _cli_context(monkeypatch, output_dir)
    cli.run(advisory_gateway=gateway)
    events = [event for event in journal.read_range() if event.event_id not in before]
    assert len(events) == 2
    assert {type(event) for event in events} == {
        StrategyDecisionRecorded, TradeIntentCreated,
    }
    assert len(authority._tickets) == 1
    assert not any(request.method == "POST" for request in transport.requests)


def test_main_default_stays_advisory_only_without_gateway(host, monkeypatch):
    journal, authority, _runtime, _admission, transport, output_dir = host
    before = tuple(journal.read_range())
    _cli_context(monkeypatch, output_dir)
    cli.run()
    assert tuple(journal.read_range()) == before
    assert not authority._tickets
    assert (output_dir / "trading_plan_BTCUSDT_offline.md").is_file()
    assert not any(request.method == "POST" for request in transport.requests)

def test_main_explicit_receipt_exposes_trusted_intent_without_private_ticket_access(
    host, monkeypatch,
):
    journal, authority, runtime, admission, transport, output_dir = host
    baseline = {event.event_id for event in journal.read_range()}
    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist", policy=_policy(),
    )
    _cli_context(monkeypatch, output_dir)
    receipt = cli.run(advisory_gateway=gateway, return_origin_receipt=True)
    from crypto_trader.crew_decision_gateway import CrewAdvisoryReceipt

    assert isinstance(receipt, CrewAdvisoryReceipt)
    assert receipt.raw_plan == plan()
    assert receipt.source_delivery_id.version == 4
    assert receipt.intent.quantity == Decimal("0.0100")
    additions = [event for event in journal.read_range() if event.event_id not in baseline]
    created = next(event for event in additions if isinstance(event, TradeIntentCreated))
    assert receipt.intent.intent_id == created.event_id
    assert admission.offer(receipt.intent) == (True, "accepted")
    prepared = runtime.prepare_intent(
        receipt.intent, _admission_token=admission._runtime_token,
    )
    assert prepared.approved
    assert not any(request.method == "POST" for request in transport.requests)

def test_main_refuses_receipt_request_without_trusted_gateway(host, monkeypatch):
    from crypto_trader.production.errors import ProductionStateError

    journal, _authority, _runtime, _admission, transport, output_dir = host
    before = tuple(journal.read_range())
    _cli_context(monkeypatch, output_dir)
    with pytest.raises(ProductionStateError, match="trusted.*gateway"):
        cli.run(return_origin_receipt=True)
    assert tuple(journal.read_range()) == before
    assert not (output_dir / "trading_plan_BTCUSDT_offline.md").exists()
    assert not any(request.method == "POST" for request in transport.requests)


def test_main_receipt_fails_closed_for_stale_host_market_quote(host, monkeypatch):
    journal, authority, runtime, _admission, transport, output_dir = host
    before = tuple(journal.read_range())
    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist", policy=_policy(stale=True),
    )
    _cli_context(monkeypatch, output_dir)
    with pytest.raises(SystemExit) as exc:
        cli.run(advisory_gateway=gateway, return_origin_receipt=True)
    assert exc.value.code == 1
    assert tuple(journal.read_range()) == before
    assert not any(request.method == "POST" for request in transport.requests)


# Item 6: read-only real-code quote lineage E2E
from datetime import datetime, timezone
import json
from crypto_trader.execution.binance_testnet.market_data import BinanceTestnetTickerQuoteSource
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.validity import ReplayMarketStateProvider


def test_public_testnet_quote_through_crew_validity_and_canonical_risk_never_posts(
    tmp_path, monkeypatch,
):
    """One REAL-code testnet market boundary, with OFFLINE fake sockets/account; NO POST."""
    class OneFrameSocket:
        def __init__(self, frame):
            self.frame = frame
            self.read_count = 0
            self.closed = False

        def recv(self):
            self.read_count += 1
            return self.frame

        def close(self):
            self.closed = True

    venue_now = datetime.fromtimestamp(fx.SERVER_TIME_MS / 1000, tz=timezone.utc)
    frame = json.dumps({
        "e": "24hrTicker", "E": int(venue_now.timestamp() * 1000), "s": "BTCUSDT",
        "b": "26999.00", "B": "10", "a": "27000.00", "A": "11",
    })
    decision_socket, risk_socket = OneFrameSocket(frame), OneFrameSocket(frame)

    def ticker_source(socket):
        return BinanceTestnetTickerQuoteSource(
            symbol="BTCUSDT", clock=lambda: venue_now,
            max_event_age=timedelta(seconds=5), max_spread_bps=Decimal("100"),
            connect=lambda _url, _timeout: socket,
        )

    quote, market_observation = ticker_source(decision_socket).read_once_bound()
    responses = canonical_risk_refresh_responses()
    responses[0] = fx.time_response(fx.SERVER_TIME_MS + 1)
    responses[2].body["updateTime"] = fx.SERVER_TIME_MS
    del responses[3]  # No untimed REST mark permitted in the strict risk path.
    with SQLiteEventJournal(tmp_path / "bound-live-facts.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        authority = CrewOriginAuthority()
        runtime, _port, _client, transport = wired(
            journal, tmp_path, [*startup_responses(), *responses],
            when=venue_now, bind_admission=False, require_origin_binding=True,
            trusted_origin_producer="gateway.crew_strategist",
            origin_authority=authority,
        )
        runtime._market_state = ReplayMarketStateProvider([market_observation])
        runtime._risk_state_provider._mark_source = ticker_source(risk_socket)
        admission = bind_test_admission(runtime, when=venue_now)
        runtime.start()
        policy = make_policy(
            config=ApprovedIntentConfig(
                venue="binance-spot-testnet", symbol="BTCUSDT",
                strategy_id="offline.e2e", approved_notional_quote=Decimal("270"),
                max_snapshot_age=timedelta(seconds=5),
                intent_valid_for=timedelta(seconds=3),
            ),
            market_quote=quote,
            host_times=HostDecisionTimes(started_at=venue_now, completed_at=venue_now),
            instrument_filters=InstrumentFilters(
                venue="binance-spot-testnet", symbol="BTCUSDT",
                tick_size=Decimal("0.01"), quantity_step=Decimal("0.0001"),
                minimum_quantity=Decimal("0.0001"),
                minimum_notional_quote=Decimal("5"),
            ),
            risk_limits=runtime._limits,
        )
        gateway = CrewDecisionGateway(
            journal=journal, runtime=runtime, authority=authority,
            producer="gateway.crew_strategist", policy=policy,
        )
        assert _run_mocked_crew(monkeypatch, tmp_path, gateway) == plan()
        intent = next(iter(authority._tickets.values()))[0]
        assert intent.market_reference == quote.reference_id
        assert runtime._market_state.latest("BTCUSDT").market_reference == quote.reference_id
        assert admission.offer(intent) == (True, "accepted")
        prepared = runtime.prepare_intent(
            intent, _admission_token=admission._runtime_token,
        )
        assert prepared.approved
        assert (decision_socket.read_count, risk_socket.read_count) == (1, 1)
        assert decision_socket.closed and risk_socket.closed
        assert not any(request.method == "POST" for request in transport.requests)
