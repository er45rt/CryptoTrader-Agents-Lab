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
    INSTRUMENT, build_runtime, startup_responses, RecordingExecutionPort,
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
    production = build_runtime(
        journal=journal, state_dir=tmp_path / "state", execution=port, **kwargs,
    )
    return production, port, client, transport


def seed(journal, *, quantity="0.001", symbol="BTCUSDT"):
    event = attempt_event(quantity=quantity).model_copy(update={"symbol": symbol})
    journal.append(event)
    return event


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

