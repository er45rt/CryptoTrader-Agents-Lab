"""Crew host cross-session origin fencing; all venue evidence is scripted offline.

This proves no-submit host receipts and their durable origin cannot be adopted by
a newly started production runtime after its SQLite journal is closed/reopened.
It does not restore a Nautilus engine or authorize execution.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import socket
from uuid import uuid4

import pytest

from crypto_trader.crew_host_session import CrewNoSubmitHostSession
from crypto_trader.crew_intent_policy import ApprovedIntentConfig, HostDecisionTimes
from crypto_trader.crew_testnet_host_market import TestnetPostStartMarketBootstrap
from crypto_trader.execution.binance_testnet import TestnetOrderRuntime
from crypto_trader.execution.binance_testnet.market_data import BinanceTestnetTickerQuoteSource
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStateError
from crypto_trader.production.runtime import CrewOriginAuthority
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    INSTRUMENT,
    bind_test_admission,
    build_runtime,
    canonical_risk_refresh_responses,
    control_for,
    seed_canonical_equity_root,
    startup_responses,
)
from crypto_trader.tests.test_r03a1b_no_submit_e2e import _cli_context
from crypto_trader.tests.test_r03a1b_testnet_host_market_bootstrap import NOW, rules
from crypto_trader.tests.test_r03a1b_testnet_ticker_quote import FakeSocket, ticker


def _deny_network(*_args, **_kwargs):
    pytest.fail("item8 attempted real network access")


def _artifact_bytes(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def _scripted_responses(when):
    """Construct fresh fixture responses using the clock of this runtime."""
    stamp = int(when.timestamp() * 1000)
    responses = [*startup_responses(), *canonical_risk_refresh_responses()]
    del responses[-2]  # Strict ticker reader replaces the untimed REST mark.
    for response in responses:
        if isinstance(response.body, dict):
            for name in ("serverTime", "updateTime"):
                if name in response.body:
                    response.body[name] = stamp
    return responses


@contextmanager
def _running_session(journal, root, output, monkeypatch, *, when, owner):
    """Build a wholly new production owner and strict post-start market binding."""
    sockets = []

    def ticker_reader():
        fake_socket = FakeSocket(ticker(
            E=int(when.timestamp() * 1000), b="26999.00", a="27001.00",
        ))
        sockets.append(fake_socket)
        return BinanceTestnetTickerQuoteSource(
            symbol=INSTRUMENT.symbol,
            clock=lambda: when,
            max_event_age=timedelta(seconds=5),
            max_spread_bps=Decimal("100"),
            connect=lambda *_args: fake_socket,
        )

    bootstrap = TestnetPostStartMarketBootstrap(
        instrument=INSTRUMENT, venue_rules=rules(), ticker_factory=ticker_reader,
    )
    transport = fx.ScriptedTransport(_scripted_responses(when))
    client = fx.client(
        transport,
        retries=1,
        clock_instance=fx.SpotClock(time_source=lambda: when.timestamp()),
    )
    execution = TestnetOrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    control = control_for(journal, root / "state", when=when)
    risk_source = bootstrap.canonical_risk_source(
        journal=journal,
        control=control,
        client=client,
        venue_account_id="123456789",
        max_decision_mark_move_bps=Decimal("50"),
    )
    authority = CrewOriginAuthority()
    runtime = build_runtime(
        journal=journal,
        state_dir=root / "state",
        execution=execution,
        control=control,
        risk_state_source=risk_source,
        market=bootstrap.market_state,
        when=when,
        owner_id=owner,
        bind_admission=False,
        require_origin_binding=True,
        trusted_origin_producer="gateway.crew_strategist",
        origin_authority=authority,
    )
    admission = bind_test_admission(runtime, when=when)
    started = False
    try:
        report = runtime.start()
        started = True
        binding = bootstrap.capture_after_start(runtime)
        _cli_context(monkeypatch, output)
        host = CrewNoSubmitHostSession(
            journal=journal,
            runtime=runtime,
            admission=admission,
            authority=authority,
            producer="gateway.crew_strategist",
            config=ApprovedIntentConfig(
                venue="binance-spot-testnet",
                symbol="BTCUSDT",
                strategy_id="offline.item8",
                approved_notional_quote=Decimal("270"),
                max_snapshot_age=timedelta(seconds=5),
                intent_valid_for=timedelta(seconds=3),
            ),
            market_quote=binding.quote,
            host_times=HostDecisionTimes(started_at=when, completed_at=when),
            instrument_filters=binding.instrument_filters,
            risk_limits=runtime._limits,
            testnet_market_binding=binding,
        )
        yield runtime, admission, host, authority, transport, sockets, report
    finally:
        if started:
            runtime.stop()


def test_host_receipt_cannot_cross_reopened_runtime_session(tmp_path, monkeypatch):
    """A durable A receipt never becomes B authority; B's own receipt still works."""
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)

    db_path = tmp_path / "restart.sqlite"
    output = tmp_path / "deliveries"
    output.mkdir()

    with SQLiteEventJournal(db_path, venue_writes=True) as journal_a:
        seed_canonical_equity_root(journal_a)
        with _running_session(
            journal_a, tmp_path, output, monkeypatch,
            when=NOW, owner="normal:item8-a",
        ) as session_a:
            runtime_a, admission_a, host_a, authority_a, transport_a, sockets_a, report_a = session_a
            before_a = {event.event_id for event in journal_a.read_range()}
            evidence_a = host_a.run()
            old_intent = evidence_a.receipt.intent
            assert {event.event_id for event in journal_a.read_range()} - before_a == {
                evidence_a.decision_event_id,
                evidence_a.intent_event_id,
            }
            runtime_a._verify_durable_origin(old_intent)
            assert admission_a.depth() == 0
            assert admission_a.active_intent_id() is None
            assert len(sockets_a) == 1
            assert all(s.closed and s.read_count == 1 for s in sockets_a)
            assert transport_a.requests
            assert all(request.method == "GET" for request in transport_a.requests)
        persisted_a = tuple(journal_a.read_range())

    artifacts_a = _artifact_bytes(output)
    assert len(artifacts_a) == 3  # Pending manifest, completed manifest, plan.
    later = NOW + timedelta(seconds=1)
    output_b = tmp_path / "deliveries-b"
    output_b.mkdir()

    with SQLiteEventJournal(db_path, venue_writes=True) as journal_b:
        assert tuple(journal_b.read_range()) == persisted_a
        with _running_session(
            journal_b, tmp_path, output_b, monkeypatch,
            when=later, owner="normal:item8-b",
        ) as session_b:
            runtime_b, admission_b, host_b, authority_b, transport_b, sockets_b, report_b = session_b
            assert runtime_b is not runtime_a
            assert authority_b is not authority_a
            assert report_a.owner_id == "normal:item8-a"
            assert report_b.owner_id == "normal:item8-b"
            assert report_b.started_at > report_a.started_at
            assert report_b.fence_term > report_a.fence_term

            # Baseline AFTER startup: recovery can legitimately journal new events.
            baseline = tuple(journal_b.read_range())
            assert admission_b.offer(old_intent) == (False, "prior_session_intent")
            old_preparation = runtime_b.prepare_intent(
                old_intent, _admission_token=admission_b._runtime_token,
            )
            assert not old_preparation.approved
            assert old_preparation.blocked_reason_code == "prior_session_intent"
            with pytest.raises(ProductionStateError, match="prior.session"):
                runtime_b._verify_durable_origin(old_intent)

            freshened = replace(
                old_intent,
                intent_id=uuid4(),
                decision_started_at=later,
                decision_completed_at=later,
                market_event_ts=later,
            )
            assert admission_b._time_rejection_reason(freshened, later) is None
            assert admission_b.offer(freshened) == (False, "prior_session_intent")
            forged_preparation = runtime_b.prepare_intent(
                freshened, _admission_token=admission_b._runtime_token,
            )
            assert not forged_preparation.approved
            assert forged_preparation.blocked_reason_code == "prior_session_intent"
            with pytest.raises(ProductionStateError, match="prior.session"):
                runtime_b._verify_durable_origin(freshened)

            # Old on-disk delivery artifacts cannot reconstruct B's issuance ledger.
            with pytest.raises(ProductionStateError, match="not finalized"):
                host_b._gateway.issue(
                    evidence_a.receipt.raw_plan,
                    source_delivery_id=evidence_a.receipt.source_delivery_id,
                )
            assert tuple(journal_b.read_range()) == baseline
            assert _artifact_bytes(output) == artifacts_a
            assert admission_b.depth() == 0
            assert admission_b.active_intent_id() is None
            assert authority_b._tickets == {}

            # Positive control: the new owner may issue its OWN provenance, not A's.
            assert admission_b.depth() == 0
            assert _artifact_bytes(output_b) == {}
            before_b = {event.event_id for event in journal_b.read_range()}
            evidence_b = host_b.run()
            current = evidence_b.receipt.intent
            assert current.origin_decision_event_id != old_intent.origin_decision_event_id
            assert current.intent_id != old_intent.intent_id
            assert current.market_reference != old_intent.market_reference
            assert evidence_b.receipt.source_delivery_id != evidence_a.receipt.source_delivery_id
            assert {event.event_id for event in journal_b.read_range()} - before_b == {
                evidence_b.decision_event_id,
                evidence_b.intent_event_id,
            }
            assert admission_b.depth() == 0
            runtime_b._verify_durable_origin(current)
            assert admission_b.offer(current) == (True, "accepted")
            assert admission_b.depth() == 1
            prepared = runtime_b.prepare_intent(
                current, _admission_token=admission_b._runtime_token,
            )
            assert prepared.approved
            assert admission_b.active_intent_id() is None
            assert len(sockets_b) == 2
            assert all(s.closed and s.read_count == 1 for s in sockets_b)
            assert all(request.method == "GET" for request in transport_b.requests)
            assert all(request.method == "GET" for request in transport_a.requests)
            assert _artifact_bytes(output) == artifacts_a
            assert len(_artifact_bytes(output_b)) == 3
