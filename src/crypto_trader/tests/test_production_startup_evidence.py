"""G6 authoritative startup guard: all requests use offline scripted transports."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from threading import Event, Thread
from uuid import uuid4

import pytest

from crypto_trader.breakglass import StaleWriterFenceError
from crypto_trader.execution import NonDurableExecutionError
from crypto_trader.execution.binance_testnet import TestnetOrderRuntime as OrderRuntime, TransportTimeout
from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
from crypto_trader.execution.binance_testnet.startup import (
    BinanceStartupCollector, StartupEvidenceError,
)
from crypto_trader.events import VenueTradeRecorded, VenueTradeRecordPayload
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionRecoveryRequiredError
from crypto_trader.recovery import StaticVenueStateSource
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    INSTRUMENT, RecordingExecutionPort, build_runtime, canonical_test_risk_source,
    control_for, startup_responses,
)
from crypto_trader.tests.test_recovery_submission_attempt import (
    attempt_event, state_event, CLIENT_ORDER_ID,
)


@pytest.fixture
def journal(tmp_path):
    with SQLiteEventJournal(tmp_path / "journal.db") as active:
        yield active


def wired(journal, tmp_path, responses=(), **kwargs):
    transport = fx.ScriptedTransport(list(responses))
    client = fx.client(transport, retries=1)
    port = OrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    state_dir = tmp_path / "state"
    control = control_for(journal, state_dir)
    risk_source = canonical_test_risk_source(journal=journal, control=control, client=client)
    bind_admission = kwargs.pop("bind_admission", True)
    production = build_runtime(
        journal=journal,
        state_dir=state_dir,
        execution=port,
        control=control,
        risk_state_source=risk_source,
        bind_admission=bind_admission,
        **kwargs,
    )
    return production, port, client, transport


def seed(journal, *, quantity="0.001", symbol="BTCUSDT"):
    event = attempt_event(quantity=quantity).model_copy(update={"symbol": symbol})
    journal.append(event)
    return event


def test_r03_old_persisted_intent_without_attempt_is_abandoned_after_restart(tmp_path, record_property):
    from datetime import timedelta
    from crypto_trader.events import TradeIntentCreated, TradeIntentPayload
    from crypto_trader.tests.production_fixtures import (
        BASE, intent, seed_canonical_equity_root, bind_test_admission,
        canonical_risk_refresh_responses,
    )
    from crypto_trader.tests.test_testnet_durable_submission import _r03_journal
    target = intent("r03-prior-session")
    path = tmp_path / "prior.db"
    with _r03_journal(path) as journal:
        seed_canonical_equity_root(journal)
        journal.append(TradeIntentCreated(
            event_id=target.intent_id, occurred_at=BASE, correlation_id=target.correlation_id,
            producer="offline.r03", symbol=target.symbol, strategy_id=target.strategy_id,
            payload=TradeIntentPayload(side="buy", quantity=target.quantity,
                                       order_type="limit", limit_price=target.limit_price),
        ))
    with _r03_journal(path) as journal:
        runtime, port, client, transport = wired(
            journal, tmp_path,
            [*startup_responses(), *canonical_risk_refresh_responses(),
             fx.order_response(f"ct-{target.intent_id.hex[:20]}", orig_qty="0.01000")],
            when=BASE + timedelta(seconds=2), bind_admission=False,
        )
        admission = bind_test_admission(runtime, when=BASE + timedelta(seconds=2))
        runtime.start()
        accepted, reason = admission.offer(target)
        if accepted:
            admission.run_once()
        written = [r for r in transport.requests if r.method == "POST"]
        record_property("accepted_after_restart", accepted)
        record_property("post_count", len(written))
        assert accepted is False, "old persisted intent was redelivered and accepted after restart"
        assert written == []


def test_r03_lost_attempt_commit_ack_keeps_pending_economics(tmp_path, monkeypatch, record_property):
    from crypto_trader.events import OrderSubmissionAttempted
    from crypto_trader.tests.production_fixtures import (
        intent, admitted_prepare, admitted_submit, canonical_risk_refresh_responses,
        seed_canonical_equity_root,
    )
    from crypto_trader.tests.test_testnet_durable_submission import _r03_journal
    with _r03_journal(tmp_path / "ack.db") as journal:
        seed_canonical_equity_root(journal)
        runtime, port, client, transport = wired(
            journal, tmp_path, [*startup_responses(), *canonical_risk_refresh_responses()],
        )
        runtime.start()
        preparation = admitted_prepare(runtime, intent("r03-lost-ack"))
        append = journal.append
        conditional = journal.append_batch_if_revision
        def lose_append(event):
            result = append(event)
            if isinstance(event, OrderSubmissionAttempted):
                raise RuntimeError("r03 lost commit acknowledgment")
            return result
        def lose_conditional(events, **kwargs):
            result = conditional(events, **kwargs)
            if any(isinstance(e, OrderSubmissionAttempted) for e in events):
                raise RuntimeError("r03 lost commit acknowledgment")
            return result
        monkeypatch.setattr(journal, "append", lose_append)
        monkeypatch.setattr(journal, "append_batch_if_revision", lose_conditional)
        with pytest.raises(RuntimeError, match="lost commit acknowledgment"):
            admitted_submit(runtime, preparation)
        attempts = [e for e in journal.read_range() if isinstance(e, OrderSubmissionAttempted)]
        record_property("attempt_count", len(attempts))
        record_property("pending_count", len(runtime._pending))
        assert len(attempts) == 1
        assert not [r for r in transport.requests if r.method == "POST"]
        assert len(runtime._pending) == 1, "ambiguous committed attempt must keep its risk reservation"


def test_r03_attempt_recorded_after_risk_preparation_keeps_pending_on_refusal(tmp_path):
    from crypto_trader.tests.production_fixtures import (
        intent, admitted_prepare, admitted_submit, canonical_risk_refresh_responses,
        seed_canonical_equity_root,
    )
    from crypto_trader.production import ProductionStateError
    from crypto_trader.tests.test_testnet_durable_submission import _r03_journal
    with _r03_journal(tmp_path / "risk-drift.db") as journal:
        seed_canonical_equity_root(journal)
        runtime, port, _, transport = wired(
            journal, tmp_path, [*startup_responses(), *canonical_risk_refresh_responses()],
        )
        runtime.start()
        preparation = admitted_prepare(runtime, intent("r03-risk-drift"))
        from crypto_trader.execution import ExecutionRequest
        request = ExecutionRequest(
            intent_id=preparation.intent_id, correlation_id=preparation.correlation_id,
            symbol=preparation.symbol, strategy_id=preparation.strategy_id,
            side=preparation.side, order_type=preparation.order_type,
            quantity=preparation.quantity, limit_price=preparation.limit_price,
            time_in_force=preparation.time_in_force,
        )
        prepared = port._prepare_durable_write(request)
        # Durable evidence arrives after Hard Risk, as it can from another writer.
        journal.append(port._durable_attempt_event(prepared, request))
        with pytest.raises(ProductionStateError, match="journal changed"):
            admitted_submit(runtime, preparation)
        assert not [r for r in transport.requests if r.method == "POST"]
        assert len(runtime._pending) == 1
        assert preparation.intent_id not in runtime._issued_preparations


@pytest.mark.parametrize("resolution", ["not_found", "open", "partial", "filled"])
def test_r03_post_crash_restart_new_snapshot_buy_requires_reconciliation_and_exposure(
    tmp_path, resolution, record_property,
):
    from datetime import timedelta
    from crypto_trader.tests.production_fixtures import (
        BASE, intent, admitted_execute, admitted_prepare, canonical_risk_refresh_responses,
        seed_canonical_equity_root, limits,
    )
    from crypto_trader.tests.test_testnet_durable_submission import _r03_journal
    from crypto_trader.validity import MarketObservation, ReplayMarketStateProvider
    from crypto_trader.production import ProductionStateError
    class Crash(BaseException):
        pass
    class CrashTransport(fx.ScriptedTransport):
        def send(self, request):
            if request.method == "POST":
                self.requests.append(request)
                raise Crash("offline crash after POST, before response")
            return super().send(request)
    old = intent("r03-crash-old", quantity=Decimal("0.00100"))
    reference = f"ct-{old.intent_id.hex[:20]}"
    path = tmp_path / "crash.db"
    with _r03_journal(path) as journal:
        seed_canonical_equity_root(journal)
        first, port, client, _ = wired(journal, tmp_path)
        crash_transport = CrashTransport([*startup_responses(), *canonical_risk_refresh_responses()])
        client.transport = crash_transport
        port.clock = lambda: datetime.fromtimestamp((fx.SERVER_TIME_MS + 2) / 1000, tz=timezone.utc)
        first.start()
        with pytest.raises(Crash):
            admitted_execute(first, old)
        assert len([r for r in crash_transport.requests if r.method == "POST"]) == 1
        assert len(first._pending) == 1
        assert len([e for e in journal.read_range() if e.event_type == "order.submission.attempted"]) == 1
        first.stop()
    now = BASE + timedelta(seconds=2)
    new = intent("r03-crash-new", quantity=Decimal("0.00100"),
                 decision_started_at=now, decision_completed_at=now, market_event_ts=now,
                 market_reference="r03-new-snapshot")
    market = ReplayMarketStateProvider([MarketObservation(
        symbol=new.symbol, price=new.reference_price, market_event_ts=now,
        local_ingest_ts=now, market_reference=new.market_reference, sequence=2,
    )])
    executed = {"not_found": "0", "open": "0", "partial": "0.0005", "filled": "0.001"}[resolution]
    status = {"not_found": "NEW", "open": "NEW", "partial": "PARTIALLY_FILLED", "filled": "FILLED"}[resolution]
    opened = fx.open_orders_response(reference, status=status, executed_qty=executed) if resolution in {"open", "partial"} else fx.open_orders_response()
    fills = (fx.fill(qty=executed, commission="0", asset="USDT"),) if Decimal(executed) else ()
    authoritative = fx.order_response(reference, status=status, executed_qty=executed,
                                      fills=fills, order_id=100, transact_time=fx.SERVER_TIME_MS + 2000)
    lookup = [fx.rejected(-2013)] if resolution == "not_found" else ([authoritative] if resolution == "filled" else [])
    account = fx.account_response()
    for balance in account.body["balances"]:
        if balance["asset"] == "BTC":
            balance.update(free=executed, locked="0")
        elif balance["asset"] == "USDT":
            balance.update(free="99973", locked=format((Decimal("0.001")-Decimal(executed))*Decimal("27000"), "f"))
    risk_reads = [fx.time_response(fx.SERVER_TIME_MS + 4000), opened, account,
                  fx.price_response("27000.00"), opened]
    replies = [fx.time_response(), opened, *lookup, *fx.environment_responses(),
               authoritative, *risk_reads, *risk_reads]
    with _r03_journal(path) as journal:
        restarted, port, client, transport = wired(
            journal, tmp_path, replies, when=now, market=market,
            limits_value=replace(limits(), max_symbol_exposure_quote=Decimal("40")),
        )
        if resolution == "not_found":
            with pytest.raises(ProductionRecoveryRequiredError):
                restarted.start()
            with pytest.raises(ProductionStateError):
                admitted_prepare(restarted, new)
            assert not [r for r in transport.requests if r.method == "POST"]
            record_property("restart_post_count", 0)
            return
        restarted.start()
        assert not [r for r in transport.requests if r.method == "POST"]
        assert port.recover_order(old.intent_id, correlation_id=old.correlation_id) is not None
        snapshot, _ = restarted._risk_state_provider(
            now, guard=restarted._guard, correlation_id=new.correlation_id,
        )
        record_property("earlier_symbol_exposure", str(snapshot.symbol_exposure_quote[new.symbol]))
        record_property("earlier_position", str(snapshot.position_quantity[new.symbol]))
        record_property("earlier_open_orders", snapshot.open_orders)
        assert snapshot.symbol_exposure_quote[new.symbol] == Decimal("27")
        assert snapshot.position_quantity[new.symbol] == Decimal(executed)
        preparation = admitted_prepare(restarted, new)
        assert not preparation.approved
        assert "max_symbol_exposure_exceeded" in preparation.blocked_reason_code
        assert not [r for r in transport.requests if r.method == "POST"]
        record_property("restart_post_count", 0)


def assert_stopped(runtime, transport):
    assert runtime.state().value == "stopped"
    assert runtime.startup_report() is None
    assert runtime.status()["last_refusal"]
    assert not [request for request in transport.requests if request.is_write]


def test_empty_journal_still_queries_open_orders_and_exports_secret_free_evidence(journal, tmp_path, monkeypatch):
    runtime, port, client, transport = wired(journal, tmp_path, startup_responses())
    # Route responses by endpoint here: omitting openOrders must reach the request
    # assertion, rather than misalign the FIFO environment responses.
    from urllib.parse import urlsplit
    paths = ["/api/v3/time", "/api/v3/openOrders", "/api/v3/time",
             "/api/v3/exchangeInfo", "/api/v3/account"]
    replies = {}
    for path, response in zip(paths, startup_responses(), strict=True):
        replies.setdefault(path, []).append(response)
    def send(request):
        transport.requests.append(request)
        return replies[urlsplit(request.url).path].pop(0)
    monkeypatch.setattr(transport, "send", send)
    record = runtime.start().to_record()
    assert fx.request_paths(transport) == [
        "/api/v3/time", "/api/v3/openOrders", "/api/v3/time",
        "/api/v3/exchangeInfo", "/api/v3/account",
    ]
    assert record["venue_evidence"]["open_orders_queried"] is True
    assert record["recovered_orders"] == 0
    assert client._orders == port._orders == {}
    assert {event.event_type for event in journal.read_range()} == {"recovery.state.changed"}
    assert fx.FAKE_API_KEY not in str(record) and fx.FAKE_API_SECRET not in str(record)


@pytest.mark.parametrize("sources", [(StaticVenueStateSource(),), (object(),)])
def test_writing_rejects_caller_static_or_unsupported_sources_before_claim(journal, tmp_path, sources):
    runtime, _, _, transport = wired(journal, tmp_path, venue_sources=sources)
    with pytest.raises(StartupEvidenceError):
        runtime.start()
    assert runtime.control.ownership_store.read() is None
    assert transport.requests == []
    assert_stopped(runtime, transport)


@pytest.mark.parametrize("change", ["endpoint", "symbol", "instrument", "client_type", "port_type", "journal"])
def test_mismatched_execution_context_is_refused_before_claim(journal, tmp_path, change):
    runtime, port, client, transport = wired(journal, tmp_path)
    if change == "endpoint": client.base_url += "/other"
    elif change == "symbol": client.symbol = "ETHUSDT"
    elif change == "instrument": port.instrument = replace(INSTRUMENT, symbol="ETHUSDT")
    elif change == "client_type":
        class OtherClient(type(client)): pass
        client.__class__ = OtherClient
    elif change == "port_type":
        class OtherPort(OrderRuntime): pass
        port.__class__ = OtherPort
    else: port.journal = None
    with pytest.raises((StartupEvidenceError, RuntimeError)):
        runtime.start()
    assert runtime.control.ownership_store.read() is None
    assert_stopped(runtime, transport)


@pytest.mark.parametrize("body", [None, {}, False, "", 0])
def test_malformed_empty_response_invalidates_old_writer_before_recovery(journal, tmp_path, body):
    runtime, _, _, transport = wired(journal, tmp_path, [fx.time_response(), fx.response(200, body)])
    previous = runtime.control.claim_normal_writer(owner_id="normal:old", reason_code="test")
    with pytest.raises(BinanceTestnetError): runtime.start()
    with pytest.raises(StaleWriterFenceError): previous.require_current()
    assert runtime.control.ownership_store.read().term == 2
    assert journal.count() == 0
    assert_stopped(runtime, transport)


@pytest.mark.parametrize("failure", [
    fx.response(401, {"code": -2015}), fx.response(403, {}), fx.response(302, {}),
    fx.server_error(), fx.rate_limited(), fx.banned(), TransportTimeout("timeout"),
])
def test_query_failure_never_becomes_empty_success(journal, tmp_path, failure):
    runtime, _, _, transport = wired(journal, tmp_path, [fx.time_response(), failure])
    with pytest.raises((BinanceTestnetError, TransportTimeout, RuntimeError)): runtime.start()
    assert journal.count() == 0
    assert_stopped(runtime, transport)


@pytest.mark.parametrize("kind", ["none", "nonce", "term", "client", "endpoint", "symbol", "coverage", "reports"])
def test_missing_stale_or_mismatched_batch_is_refused(journal, tmp_path, monkeypatch, kind):
    runtime, _, _, transport = wired(journal, tmp_path, startup_responses())
    original = BinanceStartupCollector.collect
    def collect(self, **kwargs):
        batch = original(self, **kwargs)
        if kind == "none": return None
        changes = {
            "nonce": {"nonce": uuid4()}, "term": {"fence_term": batch.fence_term + 1},
            "client": {"client": object()}, "endpoint": {"endpoint": "wrong"},
            "symbol": {"symbol": "ETHUSDT"}, "coverage": {"status_references": ("extra",)},
            "reports": {"open_references": ("missing",)},
        }
        return replace(batch, **changes[kind])
    monkeypatch.setattr(BinanceStartupCollector, "collect", collect)
    with pytest.raises(StartupEvidenceError): runtime.start()
    assert journal.count() == 0
    assert_stopped(runtime, transport)


@pytest.mark.parametrize("found", [True, False])
def test_required_journal_lookup_is_signed_and_negative_evidence_stays_unresolved(journal, tmp_path, found):
    seed(journal)
    lookup = fx.order_response(CLIENT_ORDER_ID, status="FILLED", executed_qty="0.001") if found else fx.rejected(-2013)
    runtime, port, client, transport = wired(journal, tmp_path, [
        fx.time_response(), fx.open_orders_response(), lookup, *fx.environment_responses(),
    ])
    if found:
        report = runtime.start()
        assert report.recovered_orders == 1
        assert report.venue_evidence["status_lookup_count"] == 1
    else:
        with pytest.raises(ProductionRecoveryRequiredError): runtime.start()
        assert_stopped(runtime, transport)
    status = [r for r in transport.requests if r.url.endswith("/order")]
    assert len(status) == 1 and "origClientOrderId=" in status[0].query
    assert client._orders == port._orders == {}


def test_open_order_covers_attempt_without_second_status_lookup(journal, tmp_path):
    seed(journal)
    runtime, _, _, transport = wired(journal, tmp_path, [
        fx.time_response(), fx.open_orders_response(CLIENT_ORDER_ID), *fx.environment_responses(),
    ])
    assert runtime.start().venue_evidence["status_lookup_count"] == 0
    assert "/api/v3/order" not in fx.request_paths(transport)


@pytest.mark.parametrize("conflict", ["anchor", "terminal", "external", "local_ahead"])
def test_full_recovery_reconstruction_keeps_anchor_and_lifecycle_conflicts(journal, tmp_path, conflict):
    if conflict != "external": seed(journal, quantity="0.002" if conflict == "anchor" else "0.001")
    if conflict == "terminal":
        for index, state in [(1, "filled"), (2, "canceled")]:
            journal.append(state_event(state, index=index, reason="test conflict").model_copy(update={"symbol": "BTCUSDT"}))
    if conflict == "local_ahead":
        journal.append(state_event("partially_filled", index=1).model_copy(update={"symbol": "BTCUSDT"}))
    orders = fx.open_orders_response(CLIENT_ORDER_ID)
    extra = []
    if conflict == "anchor":
        orders = fx.open_orders_response()
        extra = [fx.order_response(CLIENT_ORDER_ID, status="FILLED", executed_qty="0.001")]
    runtime, _, _, transport = wired(journal, tmp_path, [
        fx.time_response(), orders, *extra, *fx.environment_responses(),
    ])
    with pytest.raises(ProductionRecoveryRequiredError, match="unresolved order state") as error:
        runtime.start()
    assert type(error.value) is ProductionRecoveryRequiredError
    assert runtime.status()["last_refusal"] == "recovery_required"
    assert "/api/v3/exchangeInfo" not in fx.request_paths(transport)
    assert_stopped(runtime, transport)


@pytest.mark.parametrize("symbol", ["ETHUSDT", None])
def test_writing_journal_scope_refuses_missing_or_other_symbol(journal, tmp_path, symbol):
    seed(journal, symbol=symbol)
    runtime, _, _, transport = wired(journal, tmp_path)
    with pytest.raises(StartupEvidenceError): runtime.start()
    assert transport.requests == []
    assert_stopped(runtime, transport)


def test_writing_journal_scope_refuses_foreign_symbol_venue_trade(journal, tmp_path):
    fill_event_id = uuid4()
    trade_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    journal.append(VenueTradeRecorded(
        occurred_at=trade_time,
        correlation_id=uuid4(),
        causation_id=fill_event_id,
        producer="infrastructure.binance_testnet",
        order_id=uuid4(),
        symbol="ETHUSDT",
        payload=VenueTradeRecordPayload(
            venue_reference="ct-foreign",
            venue_order_id="42",
            venue_trade_id="7",
            fill_event_id=fill_event_id,
            price=Decimal("27000"),
            quantity=Decimal("0.001"),
            quote_quantity=Decimal("27"),
            commission=Decimal("0"),
            commission_asset="USDT",
            trade_time=trade_time,
            is_buyer=True,
            is_maker=False,
        ),
    ))
    runtime, _, _, transport = wired(journal, tmp_path)
    with pytest.raises(StartupEvidenceError):
        runtime.start()
    assert transport.requests == []
    assert_stopped(runtime, transport)


@pytest.mark.parametrize("sequence", [None, 1, 2, 50])
@pytest.mark.parametrize("phase", ["collect", "recovery", "execution"])
def test_journal_drift_is_detected_without_global_sequence_assumption(journal, tmp_path, monkeypatch, sequence, phase):
    from crypto_trader.events import RecoveryStateChanged, RecoveryStateChangePayload
    from crypto_trader.tests.production_fixtures import BASE
    journal.append(RecoveryStateChanged(occurred_at=BASE, correlation_id=uuid4(), producer="application.test",
        sequence_number=50, payload=RecoveryStateChangePayload(state="detected", reason="baseline")))
    runtime, port, client, transport = wired(journal, tmp_path, startup_responses())
    def drift():
        event = attempt_event(sequence_number=sequence).model_copy(update={"symbol": "BTCUSDT"})
        journal.append(event)
    if phase == "collect":
        original = client.startup_open_orders
        def query(symbol):
            result = original(symbol); drift(); return result
        monkeypatch.setattr(client, "startup_open_orders", query)
    elif phase == "recovery":
        original = journal.append_batch
        def append(events):
            result = original(events); drift(); return result
        monkeypatch.setattr(journal, "append_batch", append)
    else:
        original = port.start
        def start(): original(); drift()
        monkeypatch.setattr(port, "start", start)
    with pytest.raises(StartupEvidenceError): runtime.start()
    assert_stopped(runtime, transport)
    assert port.state().value == "stopped"


def test_failed_restart_discards_report_and_orphaned_resolved_pass_is_not_a_shortcut(journal, tmp_path):
    runtime, _, _, transport = wired(journal, tmp_path, startup_responses())
    first = runtime.start()
    runtime.stop()
    seed(journal)
    transport.responses.extend([fx.time_response(), fx.open_orders_response(), fx.rejected(-2013)])
    with pytest.raises(ProductionRecoveryRequiredError): runtime.start(correlation_id=uuid4())
    assert_stopped(runtime, transport)
    assert first.venue_evidence["invocation_id"]
    assert fx.request_paths(transport).count("/api/v3/openOrders") == 2


@pytest.mark.parametrize("phase", ["collect", "execution"])
def test_takeover_during_network_or_execution_start_prevents_running(journal, tmp_path, monkeypatch, phase):
    runtime, port, client, transport = wired(journal, tmp_path, startup_responses())
    def takeover():
        runtime.control.claim_normal_writer(owner_id="normal:competitor", reason_code="test")
    if phase == "collect":
        original = client.startup_open_orders
        def query(symbol): result = original(symbol); takeover(); return result
        monkeypatch.setattr(client, "startup_open_orders", query)
    else:
        original = port.start
        def start(): original(); takeover()
        monkeypatch.setattr(port, "start", start)
    with pytest.raises(StaleWriterFenceError): runtime.start()
    assert runtime.control.ownership_store.read().owner_id == "normal:competitor"
    assert port.state().value == "stopped"
    assert_stopped(runtime, transport)


def test_explicit_non_writing_startup_remains_offline(journal, tmp_path):
    runtime = build_runtime(journal=journal, state_dir=tmp_path / "state", execution=RecordingExecutionPort(INSTRUMENT))
    assert runtime.start().venue_evidence is None
    assert runtime.state().value == "running"


def test_advancing_clock_uses_fresh_final_fence_time(journal, tmp_path):
    from datetime import datetime, timedelta
    from crypto_trader.tests.production_fixtures import BASE

    runtime, _, _, transport = wired(journal, tmp_path, startup_responses())
    times = []
    def advancing():
        value = BASE + timedelta(milliseconds=len(times))
        times.append(value)
        return value
    runtime._clock = advancing
    report = runtime.start()
    assert len(times) > 3
    assert report.started_at == times[-1]
    assert report.started_at > datetime.fromisoformat(report.venue_evidence["collected_at"])
    assert runtime.state().value == "running"
    assert not any(request.is_write for request in transport.requests)





@pytest.mark.parametrize("change", ["client", "instrument", "runtime_instrument", "journal", "transport", "credentials", "capability", "sources"])
def test_context_changes_during_collection_are_refused(journal, tmp_path, monkeypatch, change):
    from crypto_trader.execution import ExecutionCapability
    runtime, port, client, transport = wired(journal, tmp_path, startup_responses())
    original = client.startup_open_orders
    def query(symbol):
        result = original(symbol)
        if change == "client": port.client = fx.client(fx.ScriptedTransport(fx.environment_responses()))
        elif change == "instrument": port.instrument = replace(INSTRUMENT, size_precision=4)
        elif change == "runtime_instrument": runtime._instrument = replace(INSTRUMENT, size_precision=4)
        elif change == "journal": port.journal = None
        elif change == "transport": client.transport = fx.ScriptedTransport(fx.environment_responses())
        elif change == "credentials": client.credentials = fx.credentials()
        elif change == "capability": port.capability = ExecutionCapability.NON_WRITING
        else: runtime._venue_sources = (StaticVenueStateSource(),)
        return result
    monkeypatch.setattr(client, "startup_open_orders", query)
    early = change in {"client", "instrument", "journal", "transport", "credentials"}
    expected = NonDurableExecutionError if change == "capability" else StartupEvidenceError
    message = (
        "contradictory declaration" if change == "capability" else
        "unsupported or changed Binance startup execution context" if early else
        "production execution/source wiring changed during startup"
    )
    with pytest.raises(expected, match=message) as error:
        runtime.start()
    assert type(error.value) is expected
    phase = "venue_evidence" if early else "post_recovery_evidence"
    assert runtime.status()["last_refusal"] == f"{phase}:{expected.__name__}"
    assert journal.count() == (0 if early else 3)
    assert_stopped(runtime, transport)


@pytest.mark.parametrize("http,code", [(400, -2010), (401, -2013), (400, "-2013")])
def test_required_lookup_errors_are_not_suppressed(journal, tmp_path, http, code):
    seed(journal)
    runtime, _, _, transport = wired(journal, tmp_path, [
        fx.time_response(), fx.open_orders_response(), fx.response(http, {"code": code}),
    ])
    with pytest.raises(BinanceTestnetError): runtime.start()
    assert journal.count() == 1  # No reconciliation success or failed pass on invalid evidence.
    assert_stopped(runtime, transport)


def test_original_journal_content_change_is_detected_even_with_same_identity(journal, tmp_path, monkeypatch):
    seed(journal)
    runtime, _, client, transport = wired(journal, tmp_path, [
        fx.time_response(), fx.open_orders_response(CLIENT_ORDER_ID), *fx.environment_responses(),
    ])
    read = journal.read_range
    query = client.startup_open_orders
    changed = False
    def collect(symbol):
        nonlocal changed
        result = query(symbol); changed = True; return result
    def read_range(**kwargs):
        events = read(**kwargs)
        if changed:
            events[0] = events[0].model_copy(update={"strategy_id": "changed.content"})
        return events
    monkeypatch.setattr(client, "startup_open_orders", collect)
    monkeypatch.setattr(journal, "read_range", read_range)
    with pytest.raises(StartupEvidenceError, match="journal changed during startup") as error:
        runtime.start()
    assert type(error.value) is StartupEvidenceError
    assert runtime.status()["last_refusal"] == "venue_evidence:StartupEvidenceError"
    assert journal.count() == 1
    assert_stopped(runtime, transport)


def test_unexpected_recovery_event_contents_are_refused(journal, tmp_path, monkeypatch):
    runtime, _, _, transport = wired(journal, tmp_path, startup_responses())
    append = journal.append_batch
    def corrupt(events):
        return append([event.model_copy(update={"producer": "application.other"}) for event in events])
    monkeypatch.setattr(journal, "append_batch", corrupt)
    with pytest.raises(StartupEvidenceError): runtime.start()
    assert_stopped(runtime, transport)


def test_cached_prior_batch_cannot_approve_restart_with_same_correlation(journal, tmp_path, monkeypatch):
    runtime, _, _, transport = wired(journal, tmp_path, startup_responses())
    original = BinanceStartupCollector.collect
    cached = []
    def collect(self, **kwargs):
        if not cached: cached.append(original(self, **kwargs))
        return cached[0]
    monkeypatch.setattr(BinanceStartupCollector, "collect", collect)
    correlation = uuid4()
    runtime.start(correlation_id=correlation)
    runtime.stop()
    # Cached collection performs no reads, and symbol rules are already cached:
    # a bypass needs only time/account responses to reach a successful restart.
    transport.responses.extend([fx.time_response(), fx.account_response()])
    requests_before = len(transport.requests)
    with pytest.raises(StartupEvidenceError, match="missing, stale or mismatched") as error:
        runtime.start(correlation_id=correlation)
    assert type(error.value) is StartupEvidenceError
    assert runtime.status()["last_refusal"] == "venue_evidence:StartupEvidenceError"
    assert len(transport.requests) == requests_before
    assert_stopped(runtime, transport)


def test_orphaned_resolved_record_after_execution_failure_does_not_skip_fresh_query(journal, tmp_path):
    runtime, _, _, transport = wired(journal, tmp_path, startup_responses(can_withdraw=True))
    with pytest.raises(BinanceTestnetError): runtime.start()
    assert any(e.payload.state == "resolved" for e in journal.read_range())
    transport.responses.extend([fx.time_response(), fx.response(200, None)])
    with pytest.raises(BinanceTestnetError): runtime.start()
    assert fx.request_paths(transport).count("/api/v3/openOrders") == 2
    assert_stopped(runtime, transport)


def test_concurrent_emergency_takeover_is_not_locked_out_by_network_collection(tmp_path):
    from crypto_trader.breakglass import OwnershipStore, OwnerKind
    from crypto_trader.tests.production_fixtures import BASE
    entered, release = Event(), Event()
    result = []
    transport = fx.ScriptedTransport(startup_responses())
    def worker():
        with SQLiteEventJournal(tmp_path / "journal.db") as journal:
            runtime, port, client, _ = wired(journal, tmp_path)
            client.transport = transport
            original = client.startup_open_orders
            def blocked(symbol):
                entered.set()
                assert release.wait(10)
                return original(symbol)
            client.startup_open_orders = blocked
            try: runtime.start()
            except Exception as exc: result.append(exc)
            result.append((runtime.state().value, port.state().value, runtime.startup_report()))
    thread = Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(10)
        record = OwnershipStore(tmp_path / "state" / "ownership.json").takeover(
            owner_id="emergency:test", owner_kind=OwnerKind.EMERGENCY,
            acquired_at=BASE, reason_code="offline-test",
        )
        assert record.term == 2
    finally:
        release.set(); thread.join(10)
    assert not thread.is_alive()
    assert isinstance(result[0], StaleWriterFenceError)
    assert result[1] == ("stopped", "stopped", None)
    assert not [r for r in transport.requests if r.is_write]


def test_writer_claim_waits_for_prior_admitted_submission_before_collecting(tmp_path):
    from crypto_trader.tests.production_fixtures import intent
    sending, release, collecting, starting = Event(), Event(), Event(), Event()
    results = []
    def prior():
        class BlockingPort(RecordingExecutionPort):
            def submit(self, request):
                sending.set()
                assert release.wait(10)
                return super().submit(request)
        with SQLiteEventJournal(tmp_path / "journal.db") as journal:
            runtime = build_runtime(journal=journal, state_dir=tmp_path / "state",
                execution=BlockingPort(INSTRUMENT), owner_id="normal:prior")
            runtime.start()
            results.append(runtime.execute_intent(intent("prior-admitted")).submitted)
    def replacement():
        with SQLiteEventJournal(tmp_path / "journal.db") as journal:
            runtime, _, client, _ = wired(journal, tmp_path, startup_responses())
            original = client.startup_open_orders
            def query(symbol): collecting.set(); return original(symbol)
            client.startup_open_orders = query
            starting.set()
            results.append(runtime.start().fence_term)
    old, new = Thread(target=prior), Thread(target=replacement)
    old.start()
    try:
        assert sending.wait(10)
        new.start()
        assert starting.wait(10)
        assert not collecting.wait(0.1)
    finally:
        release.set(); old.join(10)
        if new.ident is not None: new.join(10)
    assert not old.is_alive() and not new.is_alive()
    assert collecting.is_set()
    assert results == [True, 2]

