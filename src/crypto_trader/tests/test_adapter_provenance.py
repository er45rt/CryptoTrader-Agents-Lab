"""Offline tests for additive raw provenance and synchronous observer boundaries."""
import ast
from collections import Counter
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from email.utils import format_datetime
import gzip
import hashlib
import json
import inspect
import io
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
import requests
from urllib3.response import HTTPResponse

from crypto_trader.tools import binance_futures_public_tool as ft
from crypto_trader.tools import derivatives_schema as ds
from crypto_trader.tests.provenance_fixture_scenarios import (
    NOW, SYMBOL, Response, concurrent_deliveries, make_client, no_network,
)



def raw(client, metric=ds.Metric.CURRENT_OI, **kwargs):
    return client.raw_delivery(metric, SYMBOL, **kwargs)


def date_at(ms):
    return format_datetime(datetime.fromtimestamp(ms / 1000, timezone.utc), usegmt=True)


def test_one_owner_shared_waiters_and_cache_reuse_exact_response(monkeypatch):
    events = []
    client, clock, _, calls = make_client(observer=events.append)
    deliveries = concurrent_deliveries(client, lambda: raw(client), monkeypatch)
    assert Counter(d.delivery_source for d in deliveries) == {"network_owner": 1, "shared_waiter": 3}
    assert len({d.response_id for d in deliveries}) == 1
    assert deliveries[0].response_id is not None
    assert all(d.response is deliveries[0].response for d in deliveries)
    assert all(d.fetched_at == NOW and not d.cache_hit for d in deliveries)
    origin = deliveries[0].cache_origin_at
    assert all(d.cache_origin_at == origin for d in deliveries)
    clock.advance(5)
    cached = raw(client)
    assert cached.delivery_source == "cache" and cached.cache_hit
    assert cached.response_id == deliveries[0].response_id
    assert cached.response is deliveries[0].response
    assert cached.fetched_at == NOW and cached.read_at == NOW + 5000
    assert cached.cache_origin_at == origin
    assert len(calls) == 1 and [event.phase for event in events] == ["before_transport", "after_outcome"]


def test_identical_values_from_distinct_responses_have_distinct_ids():
    client, clock, _, calls = make_client()
    first = raw(client)
    clock.advance(ds.CURRENT_OI_TTL_SECONDS)
    second = raw(client)
    assert first.response.raw_body == second.response.raw_body
    assert first.response_id != second.response_id
    assert second.fetched_at > first.fetched_at
    assert first.delivery_source == second.delivery_source == "network_owner"
    assert len(calls) == 2


def test_pre_parse_fetched_at_is_separate_from_unchanged_legacy_timestamp():
    def transport(path, parameters, clock, data):
        return Response(data[ds.Metric.CURRENT_OI], clock=clock, parse_delay=1)
    client, _, _, _ = make_client(transport)
    delivery = raw(client)
    assert delivery.fetched_at == NOW
    assert delivery.result.fetched_at == NOW + 1000
    assert delivery.cache_origin_at == delivery.read_at == NOW + 1000
    assert [field.name for field in fields(ft.RawResult)] == ["payload", "fetched_at", "reason", "attempts"]


def test_before_and_after_hooks_are_synchronous_and_ordered():
    sequence = []
    captured = []
    def observer(event):
        sequence.append(event.phase + "_enter")
        if event.phase == "after_outcome":
            captured.append(event.response.fetched_at)
        sequence.append(event.phase + "_exit")
    def transport(path, parameters, clock, data):
        assert sequence == ["before_transport_enter", "before_transport_exit"]
        sequence.append("transport")
        clock.advance(0.125)
        response = Response(data[ds.Metric.CURRENT_OI], clock=clock, parse_delay=1)
        original = response.json
        def parse():
            assert captured == [NOW + 125]
            sequence.append("json")
            return original()
        response.json = parse
        sequence.append("complete_body_received")
        return response
    client, _, _, calls = make_client(transport, observer=observer)
    delivery = raw(client)
    assert sequence == ["before_transport_enter", "before_transport_exit", "transport",
                        "complete_body_received", "after_outcome_enter", "after_outcome_exit", "json"]
    assert delivery.fetched_at == NOW + 125
    assert delivery.result.fetched_at == NOW + 1125 and len(calls) == 1
    assert delivery.response.rtt_ms == pytest.approx(125)


@pytest.mark.parametrize("error,reason,attempts", [
    (requests.exceptions.Timeout, "timeout_retry_budget_exhausted", ds.TOTAL_ATTEMPTS),
    (requests.exceptions.ConnectionError, "provider_down_retry_budget_exhausted", ds.TOTAL_ATTEMPTS),
    (requests.exceptions.RequestException, "provider_transport_error", 1),
    (RuntimeError, "internal_error", 1),
])
def test_no_response_failures_use_decided_at_not_fetched_at(error, reason, attempts):
    def transport(*args):
        raise error("detail must not become a market value")
    events = []
    client, _, _, calls = make_client(transport, observer=events.append)
    delivery = raw(client)
    assert delivery.result.reason == reason and delivery.result.payload is None
    assert delivery.fetched_at is None and delivery.response_id is None and delivery.decided_at == NOW
    assert len(calls) == attempts
    assert len(events) == 2 * attempts
    for index in range(attempts):
        before, after = events[2 * index:2 * index + 2]
        assert before.phase == "before_transport" and after.phase == "after_outcome"
        assert before.attempt_id == after.attempt_id
        assert not before.transport_invoked and after.transport_invoked
        assert after.response is None and after.decided_at == NOW


@pytest.mark.parametrize("status,header,malformed,attempts,reason", [
    (429, "3", False, 1, "rate_limited_retry_after_exceeds_budget"),
    (429, "2", False, ds.TOTAL_ATTEMPTS, "http_retry_budget_exhausted"),
    (503, None, False, ds.TOTAL_ATTEMPTS, "http_retry_budget_exhausted"),
    (200, None, True, 1, "malformed_json"),
])
def test_error_response_evidence_is_operational_not_fake_market_data(status, header, malformed, attempts, reason):
    def transport(path, parameters, clock, data):
        return Response(None, status=status, malformed=malformed,
                        headers={} if header is None else {"Retry-After": header})
    client, clock, _, calls = make_client(transport)
    delivery = raw(client)
    assert delivery.result.reason == reason and delivery.result.payload is None
    assert delivery.fetched_at is None and delivery.decided_at is not None
    responses = [event.response for event in delivery.provenance.events if event.response]
    assert len(responses) == attempts and len({response.response_id for response in responses}) == attempts
    assert len(calls) == attempts
    assert all(response.outcome == "response_received" and response.fetched_at is not None for response in responses)
    assert all(response.http_status == status and response.retry_after == header for response in responses)
    assert delivery.response_id == responses[-1].response_id
    retained = sum(len(response.raw_body or b"") for response in responses)
    assert retained <= ds.TOTAL_ATTEMPTS * ft.MAX_PROVENANCE_BODY_BYTES
    assert clock.sleeps == ([2.0] if header == "2" else [])


def test_local_denial_emits_after_only_and_does_not_fabricate_network_receipt():
    events = []
    client, _, _, calls = make_client(observer=events.append)
    for _ in range(ds.LOCAL_MAX_RPM):
        assert client.state.reserve_attempt() is None
    delivery = raw(client)
    assert delivery.delivery_source == "local_decision"
    assert delivery.result.reason == "local_rate_limit_exhausted"
    assert delivery.fetched_at is None and delivery.response_id is None and delivery.decided_at == NOW
    assert not calls and client.state.cache == {}
    assert len(events) == 1 and events[0].phase == "after_outcome"
    assert not events[0].transport_invoked


def test_retry_then_local_denial_retains_earlier_response_without_backdating_final_failure():
    events = []
    client, clock, data, calls = make_client(observer=events.append)
    original = client.transport
    def transport(path, parameters):
        original(path, parameters)
        for _ in range(ds.LOCAL_MAX_RPM - 1):
            assert client.state.reserve_attempt() is None
        return Response(None, status=503)
    client.transport = transport
    delivery = raw(client)
    assert len(calls) == 1 and delivery.result.attempts == 1
    assert delivery.result.reason == "local_rate_limit_exhausted"
    assert delivery.delivery_source == "network_owner"
    assert delivery.response_id is None and delivery.fetched_at is None and delivery.decided_at == NOW
    assert [event.phase for event in events] == ["before_transport", "after_outcome", "after_outcome"]
    original_response = events[1].response
    assert original_response.response_id and original_response.fetched_at == NOW
    assert not events[-1].transport_invoked and events[-1].response is None
    clock.advance(1)
    cached = raw(client)
    assert cached.delivery_source == "cache" and cached.fetched_at is None
    assert cached.decided_at == delivery.decided_at
    assert cached.provenance is delivery.provenance
    assert cached.provenance.events[1].response is original_response


def test_oversized_body_is_not_retained_or_truncated_and_analysis_is_unchanged():
    body = b" " * (ft.MAX_PROVENANCE_BODY_BYTES + 1)
    def transport(path, parameters, clock, data):
        return Response(data[ds.Metric.CURRENT_OI], content=body)
    client, _, data, calls = make_client(transport)
    delivery = raw(client)
    assert delivery.response.raw_body is None
    assert delivery.response.raw_body_size == len(body)
    assert delivery.response.capture_reason == "raw_body_exceeds_provenance_cap"
    assert delivery.provenance_status == "unavailable"
    assert delivery.fetched_at is None and delivery.decided_at is not None
    assert delivery.result.payload == data[ds.Metric.CURRENT_OI] and delivery.result.reason is None
    assert len(calls) == 1


def test_requests_content_is_preserved_after_library_decompression(monkeypatch):
    decoded = b' { "symbol": "ETHUSDT", "openInterest": "1234", "time": 1791079999000 }\n'
    encoded = gzip.compress(decoded)
    def send(adapter, prepared, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.headers = {"Date": date_at(NOW), "Content-Encoding": "gzip"}
        response.raw = HTTPResponse(body=io.BytesIO(encoded), headers={"Content-Encoding": "gzip"}, preload_content=False)
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    client, _, _, _ = make_client()
    client.transport = ft._public_get
    delivery = raw(client)
    assert delivery.response.raw_body == decoded and delivery.response.raw_body != encoded
    assert delivery.response.raw_body_size == len(decoded)
    assert delivery.result.payload["openInterest"] == "1234"


@pytest.mark.parametrize("kind", ["unsupported", "unbuffered", "override"])
def test_unsupported_content_is_not_consumed_or_allowed_as_provenance(kind):
    reads = []
    client, _, data, _ = make_client()
    class Unsupported:
        status_code, headers = 200, {}
        @property
        def content(self):
            reads.append("content")
            return b"consume"
        def json(self):
            return data[ds.Metric.CURRENT_OI]
    if kind == "unsupported":
        response = Unsupported()
    elif kind == "unbuffered":
        response = requests.Response()
        response.status_code = 200
        response.json = lambda: data[ds.Metric.CURRENT_OI]
    else:
        class Overridden(Response):
            @property
            def content(self):
                reads.append("content")
                return b"consume"
        response = Overridden(data[ds.Metric.CURRENT_OI])
    client.transport = lambda *args: response
    delivery = raw(client)
    assert not reads and delivery.provenance_status == "unavailable"
    assert delivery.fetched_at is None and delivery.decided_at is not None
    assert delivery.result.reason is None and delivery.result.payload == data[ds.Metric.CURRENT_OI]


@pytest.mark.parametrize("date", [None, "", "not a date"])
def test_missing_or_malformed_http_date_never_uses_premium_event_time(date):
    client, _, data, _ = make_client()
    response = Response(data[ds.Metric.CURRENT_FUNDING])
    if date is None:
        response.headers.pop("Date")
    else:
        response.headers["Date"] = date
    client.transport = lambda *args: response
    delivery = raw(client, ds.Metric.CURRENT_FUNDING)
    assert delivery.response.http_date == date
    assert delivery.response.clock_quality in ("http_date_missing", "http_date_malformed")
    assert delivery.response.clock_window_ms is None
    assert delivery.result.payload["time"] == NOW
    assert delivery.response.fetched_at == NOW


@pytest.mark.parametrize("offset,quality", [
    (-2001, "clock_skew_exceeds_tolerance"), (-2000, "http_date_window_consistent"),
    (3000, "http_date_window_consistent"), (3001, "clock_skew_exceeds_tolerance"),
])
def test_http_date_uncertainty_window_has_conservative_lower_and_upper_bounds(offset, quality):
    client, clock, data, _ = make_client()
    clock.ms += offset
    client.transport = lambda *args: Response(data[ds.Metric.CURRENT_OI])
    delivery = raw(client)
    assert delivery.response.clock_quality == quality
    assert delivery.response.clock_window_ms == (NOW - ft.HTTP_DATE_SKEW_TOLERANCE_MS,
        NOW + ft.HTTP_DATE_PRECISION_MS + ft.HTTP_DATE_SKEW_TOLERANCE_MS)
    assert ft.HTTP_DATE_SKEW_TOLERANCE_MS >= 2000


def test_rtt_excludes_before_observer_delay_and_json_parsing():
    holder = {}
    def observer(event):
        if event.phase == "before_transport":
            holder["clock"].advance(5)
    def transport(path, parameters, clock, data):
        clock.advance(0.025)
        return Response(data[ds.Metric.CURRENT_OI], clock=clock, parse_delay=1,
                        headers={"Date": date_at(clock.ms)})
    client, clock, _, _ = make_client(transport, observer=observer)
    holder["clock"] = clock
    delivery = raw(client)
    assert delivery.response.rtt_ms == pytest.approx(25)
    assert delivery.fetched_at == NOW + 5025 and delivery.result.fetched_at == NOW + 6025
    assert delivery.response.clock_quality == "http_date_window_consistent"


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_observer_failure_is_isolated_without_retry_or_cache_identity_mutation(error):
    def observer(event):
        raise error("do not leak this detail")
    client, _, _, calls = make_client(observer=observer)
    first = raw(client)
    cached = raw(client)
    assert first.result.reason is None and first.result is cached.result
    assert first.response_id == cached.response_id and first.fetched_at == cached.fetched_at
    assert first.delivery_source == "network_owner" and cached.delivery_source == "cache"
    assert len(calls) == 1
    diagnostics = tuple(client.observer_diagnostics)
    assert len(diagnostics) == 2
    assert {diagnostic.exception_type for diagnostic in diagnostics} == {error.__name__}
    assert "do not leak" not in repr(diagnostics)


def test_observer_diagnostics_are_bounded_client_local_and_events_immutable():
    def observer(event):
        raise RuntimeError()
    client, clock, _, _ = make_client(observer=observer)
    for _ in range(ft.OBSERVER_DIAGNOSTIC_LIMIT):
        delivery = raw(client)
        clock.advance(ds.CURRENT_OI_TTL_SECONDS + 1)
    assert len(client.observer_diagnostics) == ft.OBSERVER_DIAGNOSTIC_LIMIT
    other = ft.BinanceFuturesPublicClient(state=client.state)
    assert not other.observer_diagnostics and other.observer is None
    event = delivery.provenance.events[0]
    with pytest.raises(FrozenInstanceError):
        event.phase = "modified"
    with pytest.raises(FrozenInstanceError):
        delivery.response.response_id = "modified"


def test_callbacks_are_invoked_outside_shared_state_lock():
    holder, acquired = {}, []
    def observer(event):
        def probe():
            lock = holder["client"].state.lock
            obtained = lock.acquire(timeout=1)
            acquired.append(obtained)
            if obtained:
                lock.release()
        worker = threading.Thread(target=probe)
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
    client, _, _, calls = make_client(observer=observer)
    holder["client"] = client
    delivery = raw(client)
    assert delivery.result.reason is None and acquired == [True, True] and len(calls) == 1
    assert not client.observer_diagnostics


def test_same_key_observer_reentry_uses_existing_bounded_wait_without_extra_attempt(monkeypatch):
    holder, nested, waits = {}, [], []
    class ImmediateDeadline(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            return False
    forwarding = SimpleNamespace(**vars(threading))
    forwarding.Event = ImmediateDeadline
    monkeypatch.setattr(ft, "threading", forwarding)
    def observer(event):
        if event.phase == "before_transport":
            nested.append(raw(holder["client"]))
    client, _, _, calls = make_client(observer=observer)
    holder["client"] = client
    delivery = raw(client)
    assert delivery.result.reason is None and delivery.result.attempts == 1 and len(calls) == 1
    assert waits == [ds.IN_FLIGHT_WAIT_SECONDS]
    assert nested[0].delivery_source == "shared_waiter"
    assert nested[0].result.reason == "in_flight_wait_budget_exhausted"
    assert nested[0].fetched_at is None and nested[0].decided_at is not None


def test_missing_shared_result_is_a_failure_without_network_evidence():
    client, _, _, calls = make_client()
    key = (ds.PROVIDER, SYMBOL, ds.Metric.CURRENT_OI.value, None, (("symbol", SYMBOL),))
    event = threading.Event()
    event.set()
    client.state.in_flight[key] = event
    delivery = raw(client)
    assert delivery.delivery_source == "shared_waiter" and delivery.result.reason == "missing_shared_result"
    assert delivery.fetched_at is None and delivery.response_id is None and delivery.decided_at is not None
    assert not calls


@pytest.mark.parametrize("metric,kwargs", [
    (ds.Metric.CURRENT_OI, {"symbol": "ethusdt"}),
    (ds.Metric.CURRENT_OI, {"period": "invalid"}),
    (ds.Metric.CURRENT_OI, {"limit": True}),
    (ds.Metric.CURRENT_OI, {"limit": 0}),
    (ds.Metric.CURRENT_OI, {"end_time": NOW + 1}),
    (ds.Metric.CURRENT_OI, {"start_time": NOW}),
    (ds.Metric.HISTORICAL_OI, {"start_time": NOW, "end_time": NOW - 1}),
    (ds.Metric.HISTORICAL_OI, {"start_time": NOW // 1000}),
    (ds.Metric.SETTLED_FUNDING, {"limit": ds.MAX_FUNDING_HISTORY_LIMIT + 1}),
    (ds.Metric.FUNDING_INTERVAL, {"symbol": "bad"}),
])
def test_raw_admission_and_query_validation_match_analysis(metric, kwargs):
    symbol = kwargs.get("symbol", SYMBOL)
    parameters = {key: value for key, value in kwargs.items() if key != "symbol"}
    client, _, _, calls = make_client()
    legacy = client.observations(metric, symbol, **parameters)[0]
    delivery = client.raw_delivery(metric, symbol, **parameters)
    assert legacy.quality_reason == delivery.result.reason
    assert delivery.fetched_at is None and not calls


def test_funding_metadata_admits_real_symbol_and_reuses_global_key():
    client, _, _, calls = make_client()
    delivery = raw(client, ds.Metric.FUNDING_INTERVAL)
    assert set(client.state.leases) == {SYMBOL}
    assert next(iter(client.state.cache))[1] == "*"
    assert delivery.response_id is not None and len(calls) == 1


def test_historical_raw_fetch_does_not_change_historical_analysis_replay_eligibility():
    client, _, _, calls = make_client()
    delivery = raw(client, ds.Metric.HISTORICAL_OI)
    historical = client.observations(ds.Metric.HISTORICAL_OI, SYMBOL)[0]
    assert delivery.fetched_at == NOW and historical.available_at is None
    assert not historical.replay_eligible and not ds.strict_replay([historical], NOW)
    assert len(calls) == 1


def test_source_contract_has_no_new_endpoint_host_or_file_io():
    # Source-text references (including decorators), independent of Git checkout
    # state and interpreter ast.dump changes. Reproduce offline at approved blob:
    # python -B <external-evidence>/derive_base_contract.py
    # The script uses git show b3ce636:src/crypto_trader/tools/binance_futures_public_tool.py;
    # its input is blob 7d8fa65bbbe2690f3817691e7f96a9dc202b03b7,
    # SHA256 2f733196adfae1095b9b2de70a433cdee46f5196afd84158ed62fd6e89452944.
    # These are static source-boundary guards, not regenerated analysis goldens.
    references = {"ENDPOINTS":"5f9c43f55668eb09711fa527a8f6b9bba3ffb50a0eb93f99056d77a721c96d41","PUBLIC_BASE_URL":"36ac7f1310a5a5595a2e8219162c3b6b915dad2303e967670ab0f9130a54c1a8","QUERY_KEYS":"cce113f070ee11341ebcf5db48b25c20f634e2368b9f6d654247401eba34e01d","RETRYABLE_HTTP":"1a6204762e91a2728d863d7d6c2d9165b3c5d718b4f5e11eed5be15e1c3d3029","RawResult":"6b2dda809b78a47d3ab9b72b21ad2c8f007b865637d93c59043eaac90faf020e","_public_get":"b9b1ff6fd1b68b480bedf2d777dc6a590c99afa67209bb2569096cf343ac0089","host_literals":"829d2db73563cded7692a8b400f02981ebb4c1d6e9316ac0f4ad5e6d92d4e8f7"}
    source = Path(ft.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    actual = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in {"RawResult", "_public_get"}:
            text = "".join("@" + ast.get_source_segment(source, d) + "\n" for d in node.decorator_list)
            text += ast.get_source_segment(source, node)
            actual[node.name] = hashlib.sha256(text.encode()).hexdigest()
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"PUBLIC_BASE_URL", "ENDPOINTS", "QUERY_KEYS", "RETRYABLE_HTTP"}:
                    actual[target.id] = hashlib.sha256(ast.get_source_segment(source, node.value).encode()).hexdigest()
    fragment = "fa" + "pi."
    literals = Counter(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                       and isinstance(n.value, str) and fragment in n.value)
    actual["host_literals"] = hashlib.sha256(json.dumps(sorted(literals.items()), separators=(",", ":")).encode()).hexdigest()
    assert actual == references
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            assert not (isinstance(node.func, ast.Name) and node.func.id == "open")
            assert not (isinstance(node.func, ast.Attribute) and node.func.attr in
                        ("write_text", "write_bytes", "mkdir", "unlink", "remove", "Thread", "Queue", "create_task"))

@pytest.mark.parametrize("error,attempts", [
    (requests.exceptions.Timeout, ds.TOTAL_ATTEMPTS),
    (requests.exceptions.ConnectionError, ds.TOTAL_ATTEMPTS),
    (RuntimeError, 1), (AttributeError, 1),
])
def test_post_receipt_json_failure_does_not_emit_duplicate_after_events(error, attempts):
    events = []
    def transport(path, parameters, clock, data):
        response = Response(data[ds.Metric.CURRENT_OI])
        def parse():
            raise error()
        response.json = parse
        return response
    client, _, _, calls = make_client(transport, observer=events.append)
    delivery = raw(client)
    assert delivery.result.payload is None and delivery.fetched_at is None
    assert len(calls) == attempts and len(events) == 2 * attempts
    assert [event.phase for event in events] == ["before_transport", "after_outcome"] * attempts
    assert len({event.response.response_id for event in events if event.response}) == attempts


def test_async_observer_functions_are_rejected_before_transport():
    async def callback(event):
        raise AssertionError("must not be dispatched")
    with pytest.raises(ValueError, match="observer_must_be_synchronous"):
        make_client(observer=callback)


def test_returned_coroutine_is_isolated_and_never_dispatched():
    ran = []
    async def callback(event):
        ran.append(event.phase)
    def observer(event):
        return callback(event)
    client, _, _, calls = make_client(observer=observer)
    delivery = raw(client)
    assert not ran and len(calls) == 1 and delivery.result.reason is None
    assert len(client.observer_diagnostics) == 2
    assert {entry.exception_type for entry in client.observer_diagnostics} == {"TypeError"}


@pytest.mark.parametrize("status", [429, 418])
def test_cooldown_is_visible_before_after_observer_can_issue_other_key(status):
    holder, nested = {}, []
    def observer(event):
        if event.phase == "after_outcome" and event.response:
            client = holder["client"]
            assert client.state.cooldown_until > client.state.monotonic()
            other = ft.BinanceFuturesPublicClient(state=client.state,
                transport=lambda *args: pytest.fail("provider cooldown must block this transport"))
            nested.append(other.raw_delivery(ds.Metric.CURRENT_FUNDING, SYMBOL))
    def transport(path, parameters, clock, data):
        return Response(None, status=status, headers={"Retry-After": "3"})
    client, _, _, calls = make_client(transport, observer=observer)
    holder["client"] = client
    delivery = raw(client)
    assert len(calls) == 1 and len(nested) == 1
    assert nested[0].result.reason == "provider_rate_limit_cooldown"
    assert nested[0].result.attempts == 0 and nested[0].fetched_at is None
    assert not client.observer_diagnostics
    assert delivery.result.reason in ("rate_limited_retry_after_exceeds_budget", "provider_ip_banned")


@pytest.mark.parametrize("cause", ["exhausted", "cooldown"])
def test_before_observer_delay_rechecks_budget_at_actual_transport_invocation(cause):
    holder = {}
    def observer(event):
        if event.phase == "before_transport":
            client, clock = holder["client"], holder["clock"]
            clock.advance(ds.LOCAL_RATE_WINDOW_SECONDS + 1)
            if cause == "exhausted":
                for _ in range(ds.LOCAL_MAX_RPM):
                    assert client.state.reserve_attempt() is None
            else:
                client.state.cooldown(5)
    client, clock, _, calls = make_client(observer=observer)
    holder.update(client=client, clock=clock)
    delivery = raw(client)
    assert not calls and delivery.result.attempts == 0 and delivery.fetched_at is None
    assert delivery.result.reason == ("local_rate_limit_exhausted" if cause == "exhausted" else "provider_rate_limit_cooldown")
    assert len(client.state.attempts) == (ds.LOCAL_MAX_RPM if cause == "exhausted" else 0)
    assert client.state.cache == {}
    assert [e.phase for e in delivery.provenance.events] == ["before_transport", "after_outcome"]
    assert not delivery.provenance.events[-1].transport_invoked


def test_actual_reservation_timestamp_excludes_before_observer_journal_delay():
    holder = {}
    def observer(event):
        if event.phase == "before_transport":
            holder["clock"].advance(ds.LOCAL_RATE_WINDOW_SECONDS + 1)
    client, clock, _, calls = make_client(observer=observer)
    holder["clock"] = clock
    delivery = raw(client)
    assert delivery.result.reason is None and len(calls) == 1
    assert list(client.state.attempts) == [clock.monotonic()]
    assert delivery.fetched_at == clock.ms


@pytest.mark.parametrize("supported", [False, True])
def test_stateful_headers_properties_are_never_invoked_by_provenance_capture(supported):
    client, _, data, _ = make_client()
    reads = []
    remaining = iter([data[ds.Metric.CURRENT_OI]])
    if supported:
        class Custom(Response):
            @property
            def headers(self):
                reads.append("headers")
                next(remaining)
                return {}
            @headers.setter
            def headers(self, value):
                pass
            def json(self):
                return next(remaining)
        # Bypass fixture construction: its headers.update would invoke this
        # deliberately stateful property before the adapter is exercised.
        response = Custom.__new__(Custom)
        response.__dict__.update(status_code=200, _content=b'{"openInterest":"5"}')
    else:
        class Custom:
            status_code = 200
            @property
            def headers(self):
                reads.append("headers")
                next(remaining)
                return {}
            def json(self):
                return next(remaining)
        response = Custom()
    client.transport = lambda *args: response
    delivery = raw(client)
    assert not reads and delivery.result.reason is None
    assert delivery.result.payload == data[ds.Metric.CURRENT_OI]
    assert delivery.provenance_status == "unavailable" and delivery.fetched_at is None


@pytest.mark.parametrize("kind", ["function", "instance"])
def test_async_generator_observer_registration_is_rejected(kind):
    async def generator(event):
        yield event
    class Callback:
        async def __call__(self, event):
            yield event
    with pytest.raises(ValueError, match="observer_must_be_synchronous"):
        make_client(observer=generator if kind == "function" else Callback())


def test_async_generator_result_is_diagnosed_without_advancing_or_scheduling():
    ran = []
    async def generator(event):
        ran.append(event.phase)
        yield event
    client, _, _, calls = make_client(observer=lambda event: generator(event))
    delivery = raw(client)
    assert not ran and len(calls) == 1 and delivery.result.reason is None
    assert len(client.observer_diagnostics) == 2
    assert {diagnostic.exception_type for diagnostic in client.observer_diagnostics} == {"TypeError"}


def test_outer_delivery_failure_retains_already_emitted_response_trace():
    client, _, _, calls = make_client()
    original = client._http
    def fails_after_response(metric, parameters, trace):
        original(metric, parameters, trace)
        raise RuntimeError("outer failure")
    client._http = fails_after_response
    delivery = raw(client)
    assert len(calls) == 1 and delivery.result.reason == "internal_error"
    assert delivery.result.attempts == 0 and client.state.cache == {}
    assert delivery.delivery_source == "network_owner"
    assert delivery.response_id is not None and delivery.response.raw_body is not None
    assert delivery.fetched_at is None and delivery.decided_at is not None
    assert [event.phase for event in delivery.provenance.events] == ["before_transport", "after_outcome"]


def test_real_buffered_requests_oversize_valid_json_keeps_analysis_payload():
    client, _, data, _ = make_client()
    content = json.dumps(data[ds.Metric.CURRENT_OI]).encode("utf-8")
    content += b" " * (ft.MAX_PROVENANCE_BODY_BYTES + 1 - len(content))
    response = requests.Response()
    response.status_code = 200
    response.headers = {"Date": date_at(NOW)}
    response._content = content
    client.transport = lambda *args: response
    delivery = raw(client)
    assert delivery.result.payload == data[ds.Metric.CURRENT_OI] and delivery.result.reason is None
    assert delivery.response.raw_body is None and delivery.response.raw_body_size == len(content)
    assert delivery.response.capture_reason == "raw_body_exceeds_provenance_cap"
    assert delivery.fetched_at is None and delivery.decided_at is not None
