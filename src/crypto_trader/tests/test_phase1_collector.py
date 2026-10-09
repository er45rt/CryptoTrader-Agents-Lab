"""Network-free collector tests: actual approved raw adapter with buffered fixtures."""
import base64
from dataclasses import replace
import email.utils
import hashlib
import json
from pathlib import Path
import os
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest
import requests

from crypto_trader.collector import policy as p
from crypto_trader.collector import service as s
from crypto_trader.collector.journal import Journal, HEADER, END, JournalError, inspect_segment
from crypto_trader.tools import binance_futures_public_tool as a
from crypto_trader.tools import derivatives_schema as ds


TRUSTED = "http_date_window_consistent"
ADAPTER_PATH = "src/crypto_trader/tools/binance_futures_public_tool.py"
ADAPTER_BLOB_OID = "128ae45f9bd827edd7888d760cb7aeef7081daa4"
ADAPTER_BLOB_SHA256 = "68635e7b6247c6686de828a8d44c1e97956dd3e84c837e72cc61501497613d98"


def adapter_identity(data):
    """(git blob SHA1, SHA256) of LF-normalized bytes; independent of Git history."""
    data = data.replace(b"\r\n", b"\n")
    return (hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(),
            hashlib.sha256(data).hexdigest())


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", deny)


class FakeClock:
    current = None

    def __init__(self):
        self.wall = 1_791_080_000_000
        self.mono = 0
        FakeClock.current = self
    def wall_ms(self):
        return self.wall
    def mono_ns(self):
        return self.mono
    def advance(self, seconds):
        self.wall += int(seconds * 1000)
        self.mono += int(seconds * p.NS_PER_SECOND)
    def sleep(self, seconds):
        self.advance(seconds)


def records(path):
    data = path.read_bytes()
    result = []
    while data:
        _, size, _ = HEADER.unpack(data[:HEADER.size])
        result.append(json.loads(data[HEADER.size:HEADER.size + size]))
        data = data[HEADER.size + size + len(END):]
    return result


def http_date(wall_ms):
    return email.utils.formatdate(wall_ms // 1000, usegmt=True)


def response(body=b' { "raw" : 1 }\n', status=200, retry=None):
    r = requests.Response()
    r.status_code = status
    r._content = body
    r._content_consumed = True
    # Date tracks the fake wall clock at response creation, so it stays
    # consistent with fetched_at even after the clock advances.
    r.headers["Date"] = http_date(FakeClock.current.wall)
    if retry is not None:
        r.headers["Retry-After"] = retry
    return r


def make_collector(tmp_path, transport=None, sync=None, factory=None):
    clock = FakeClock()
    journal = Journal(tmp_path, "test-session", sync=sync)
    config = p.Config(tmp_path, host_time_sync_acknowledged=True)
    calls = []
    def send(path, params):
        calls.append((path, params))
        return (transport or (lambda: response()))()
    def real_factory(state, observer):
        state = a.SharedPublicState(monotonic=lambda: clock.mono / p.NS_PER_SECOND,
                                    wall=lambda: clock.wall / 1000, sleep=clock.sleep)
        return a.BinanceFuturesPublicClient(state=state, observer=observer, transport=send)
    collector = s.Collector(config, journal, clock=clock,
                            bootstrap={"startup_validated": True, "fixture": True},
                            client_factory=factory or real_factory)
    return collector, clock, calls


@pytest.mark.parametrize("metric,seconds", p.DEFAULT_INTERVALS)
def test_scheduled_cadence_gives_network_owner_and_preserves_body(tmp_path, metric, seconds):
    c, clock, calls = make_collector(tmp_path)
    c.poll(metric)
    first = records(c.journal.path)[-1]
    clock.advance(seconds)
    c.poll(metric)
    second = records(c.journal.path)[-1]
    assert first["record_type"] == second["record_type"] == "raw_observation"
    assert first["delivery_source"] == second["delivery_source"] == "network_owner"
    assert first["response_id"] != second["response_id"]
    assert first["fetched_at"] < second["fetched_at"]
    assert base64.b64decode(first["raw_body_base64"]) == response().content
    assert first["http_date"] == http_date(first["fetched_at"])
    assert first["clock_quality"] == second["clock_quality"] == TRUSTED
    assert first["available_at"] == first["fetched_at"]
    assert len(calls) == 2
    c.journal.close()


def test_legacy_timestamp_not_causal(tmp_path):
    c, clock, _ = make_collector(tmp_path)
    r = response()
    parse = r.json
    def delayed_parse():
        clock.advance(1)
        return parse()
    r.json = delayed_parse
    c.client.transport = lambda *_: r
    c.poll(ds.Metric.CURRENT_OI)
    observation = records(c.journal.path)[-1]
    assert observation["fetched_at"] == clock.wall - 1000
    assert observation["available_at"] == observation["fetched_at"]
    c.journal.close()


def test_synchronous_attempt_and_outcome_durable_before_transport_retry(tmp_path):
    trace = []
    c, _, _ = make_collector(tmp_path)
    def send(*args):
        kinds = [r["record_type"] for r in records(c.journal.path)]
        assert kinds[-1] == "before_transport"
        trace.append(kinds)
        return response(status=503)
    c.client.transport = send
    c.poll(ds.Metric.CURRENT_OI)
    assert len(trace) == 2
    assert trace[1] == ["before_transport", "after_outcome", "before_transport"]
    all_records = records(c.journal.path)
    assert all_records[-1]["record_type"] == "failed_poll"
    assert len(all_records[-1]["observer_latencies"]) == 4
    assert all(l["duration_ns"] >= 0 for l in all_records[-1]["observer_latencies"])
    c.journal.close()


@pytest.mark.parametrize("kind", ["success", "timeout", "connection", "429", "5xx",
                                  "malformed", "oversize", "unsupported", "local"])
def test_success_and_failures_durable_before_next_poll(tmp_path, kind):
    c, clock, _ = make_collector(tmp_path)
    def send(*_):
        if kind == "timeout":
            raise requests.exceptions.Timeout()
        if kind == "connection":
            raise requests.exceptions.ConnectionError()
        if kind == "429":
            return response(status=429, retry="3")
        if kind == "5xx":
            return response(status=503)
        if kind == "malformed":
            return response(body=b"broken")
        if kind == "oversize":
            return response(body=b'"' + b"x" * a.MAX_PROVENANCE_BODY_BYTES + b'"')
        if kind == "unsupported":
            return SimpleNamespace(status_code=200, headers={}, json=lambda: {})
        return response()
    c.client.transport = send
    if kind == "local":
        c.client.state.attempts.extend([0.0] * ds.LOCAL_MAX_RPM)
    c.poll(ds.Metric.CURRENT_OI)
    prior = records(c.journal.path)
    if kind == "success":
        assert prior[-1]["record_type"] == "raw_observation"
    else:
        assert not any(r["record_type"] == "raw_observation" for r in prior)
        assert any(r["record_type"] == "failed_poll" for r in prior)
    clock.advance(90)
    def next_transport(*_):
        assert records(c.journal.path)[:len(prior)] == prior
        return response()
    c.client.transport = next_transport
    c.poll(ds.Metric.CURRENT_OI)
    assert records(c.journal.path)[-1]["record_type"] == "raw_observation"
    c.journal.close()


@pytest.mark.parametrize("source", ["cache", "shared_waiter"])
def test_cache_and_shared_preserve_identity_without_new_observation(tmp_path, source):
    c, _, _ = make_collector(tmp_path)
    c.poll(ds.Metric.CURRENT_OI)
    cached = c.client.raw_delivery(ds.Metric.CURRENT_OI, p.SYMBOL)
    cached = replace(cached, delivery_source=source)
    c.client.raw_delivery = lambda *_: cached
    c.poll(ds.Metric.CURRENT_OI)
    result = records(c.journal.path)
    assert sum(r["record_type"] == "raw_observation" for r in result) == 1
    operational = result[-2]
    assert operational["record_type"] == "noncausal_delivery"
    assert operational["original_response_id"] == cached.response_id
    assert operational["original_provenance_fetched_at"] == cached.response.fetched_at
    assert "fetched_at" not in operational and "available_at" not in operational
    assert result[-1]["record_type"] == "collection_gap"
    c.journal.close()


def test_unbound_owner_is_gap_not_observation(tmp_path):
    c, _, _ = make_collector(tmp_path)
    original = c.client.raw_delivery(ds.Metric.CURRENT_OI, p.SYMBOL)
    c.client.raw_delivery = lambda *_: original
    c.poll(ds.Metric.CURRENT_OI)
    assert records(c.journal.path)[-1]["reason"] == "unbound_network_provenance"
    assert not any(r["record_type"] == "raw_observation" for r in records(c.journal.path))
    c.journal.close()


def test_diagnostics_gap_even_success_then_durable_continuation(tmp_path):
    c, clock, _ = make_collector(tmp_path)
    real = c.client.raw_delivery
    def diagnostic(*args):
        delivery = real(*args)
        c.client.observer_diagnostics.append(a.ObserverDiagnostic("after_outcome", "id", "fixture"))
        return delivery
    c.client.raw_delivery = diagnostic
    c.poll(ds.Metric.CURRENT_OI)
    assert records(c.journal.path)[-1]["reason"] == "observer_diagnostics"
    assert not any(r["record_type"] == "raw_observation" for r in records(c.journal.path))
    prior = records(c.journal.path)
    c.client.raw_delivery = real
    clock.advance(45)
    old_send = c.client.transport
    def checked(*args):
        assert records(c.journal.path)[:len(prior)] == prior
        return old_send(*args)
    c.client.transport = checked
    c.poll(ds.Metric.CURRENT_OI)
    assert records(c.journal.path)[-1]["record_type"] == "raw_observation"
    c.journal.close()


def test_gap_write_failure_stops_run_without_clean_stop(tmp_path):
    c, _, calls = make_collector(tmp_path)
    real = c.client.raw_delivery
    def diagnostic(*args):
        delivery = real(*args)
        c.client.observer_diagnostics.append(a.ObserverDiagnostic("after_outcome", "id", "fixture"))
        return delivery
    c.client.raw_delivery = diagnostic
    append = c.journal.append
    def failing(record):
        if record["record_type"] == "collection_gap":
            c.journal.poisoned = True
            raise JournalError("gap-write-fixture")
        append(record)
    c.journal.append = failing
    assert c.run() == p.EXIT_GAP_FAILED
    assert len(calls) == 1
    assert not any(r["record_type"] == "clean_stop" for r in records(c.journal.path))


def test_lifecycle_and_heartbeat_with_clock_evidence(tmp_path):
    c, clock, _ = make_collector(tmp_path)
    c.start()
    c.poll(ds.Metric.CURRENT_OI)
    clock.advance(60)
    c.heartbeat()
    c.request_stop("fixture")
    # Avoid starting twice; run() separately proves the clean lifecycle below.
    c.persist("clean_stop", shutdown_reason=c.stop_reason)
    result = records(c.journal.path)
    assert [r["record_type"] for r in result[:2]] == ["bootstrap", "collector_start"]
    assert result[-2]["record_type"] == "heartbeat"
    assert result[-2]["last_successful_fetched_at"]
    assert result[-1]["record_type"] == "clean_stop"
    assert all("wall_ms" in r and "monotonic_ns" in r for r in result)
    assert all(r["monotonic_source"] == "time.monotonic_ns" and r["monotonic_unit"] == "ns"
               for r in result)
    c.journal.close()


@pytest.mark.parametrize("jump", [10000, -10000])
def test_wall_step_residual_and_monotonic_ordering(tmp_path, jump):
    c, clock, _ = make_collector(tmp_path)
    c.start()
    clock.advance(1)
    clock.wall += jump
    c.heartbeat()
    result = records(c.journal.path)
    assert result[-1]["wall_step_alarm"]
    assert result[-1]["wall_step_residual_ms"] == jump
    assert result[-1]["monotonic_ns"] >= result[-2]["monotonic_ns"]
    c.journal.close()


def test_run_clean_stop_after_current_call_no_extra_poll(tmp_path):
    c, _, calls = make_collector(tmp_path)
    send = c.client.transport
    def stop(*args):
        c.request_stop("test-signal")
        return send(*args)
    c.client.transport = stop
    assert c.run() == 0
    result = records(c.journal.path)
    assert len(calls) == 1
    assert result[-2]["record_type"] == "raw_observation"
    assert result[-1]["record_type"] == "clean_stop"
    assert result[-1]["shutdown_reason"] == "test-signal"


def test_policy_cadence_actual_ttl_and_request_budget(tmp_path):
    config = p.Config(tmp_path, host_time_sync_acknowledged=True)
    budget = config.validate()
    assert budget["steady_rpm"] == 4
    assert budget["retry_average_rpm"] == 8
    assert budget["worst_case_window_attempts"] == 12
    assert budget["startup_attempts"] == 8
    assert budget["headroom_percent"] == pytest.approx(86.6666667)
    for metric, interval in config.intervals:
        assert interval > ds.PollingPolicy().ttl(metric) + p.MIN_TTL_MARGIN_SECONDS
    with pytest.raises(ValueError):
        replace(config, symbol="BTCUSDT").validate()
    with pytest.raises(ValueError):
        replace(config, host_time_sync_acknowledged=False).validate()
    for metric, _ in config.intervals:
        intervals = tuple((m, ds.PollingPolicy().ttl(m) + p.MIN_TTL_MARGIN_SECONDS
                           if m == metric else seconds) for m, seconds in config.intervals)
        with pytest.raises(ValueError, match="cadence"):
            replace(config, intervals=intervals).validate()
    low_ttl = ds.PollingPolicy(current_oi=0, current_funding=0, long_short=0, funding_info=0)
    with pytest.raises(ValueError, match="budget"):
        replace(config, intervals=tuple((m, 6) for m, _ in config.intervals)).validate(low_ttl)


@pytest.mark.parametrize("failure,close_stderr", [("write", False), ("write", True),
                                                ("latency", False), ("gap_failed", True),
                                                ("baseexception", True)])
def test_actual_observer_fatal_exit_prevents_transport(tmp_path, failure, close_stderr):
    script = """
import os,socket,sys
from pathlib import Path
from crypto_trader.collector.bootstrap import guarded_import
adapter,proof=guarded_import(sleep=lambda _:None)
assert proof['startup_validated'],proof
from crypto_trader.collector.policy import Config
from crypto_trader.collector.journal import Journal,JournalError
from crypto_trader.collector.service import Collector
root=Path(sys.argv[1]);mode=sys.argv[2]
if sys.argv[3]=='closed':
    os.close(2)
j=Journal(root,'fatal-child')
assert j.file.fileno() not in (0,1,2)
c=Collector(Config(root,host_time_sync_acknowledged=True),j,bootstrap=proof)
def transport(*args):
    (root/'TRANSPORT-WAS-CALLED').write_text('bad')
    raise AssertionError('no send permitted')
c.client.transport=transport
original=j.append
def fail(record):
    if mode=='gap_failed' and record['record_type']=='collection_gap':
        raise JournalError('cannot-persist-gap')
    if record['record_type']=='before_transport':
        if mode=='baseexception':
            raise KeyboardInterrupt('fixture')
        if mode in ('write','gap_failed'):
            j.poisoned=True
            raise JournalError('fixture')
        import time
        time.sleep(1.05)
    original(record)
j.append=fail
c.poll(adapter.ds.Metric.CURRENT_OI)
raise AssertionError('fatal observer returned')
"""
    child = subprocess.run([sys.executable, "-B", "-c", script, str(tmp_path), failure,
                            "closed" if close_stderr else "open"],
                           capture_output=True, text=True, timeout=90)
    assert child.returncode == (p.EXIT_GAP_FAILED if failure == "gap_failed"
                                else p.EXIT_FATAL_WITH_GAP), child.stderr
    assert not (tmp_path / "TRANSPORT-WAS-CALLED").exists()
    all_records = [r for segment in tmp_path.glob("*.darc") for r in records(segment)]
    assert all(inspect_segment(segment).status == "complete_valid"
               for segment in tmp_path.glob("*.darc"))
    if failure != "gap_failed":
        assert any(r["record_type"] == "collection_gap" and r["fatal"] for r in all_records)
    assert not any(r["record_type"] == "clean_stop" for r in all_records)


def test_adapter_source_unchanged_and_no_new_network_paths():
    import ast
    import crypto_trader.collector as package
    root = Path(package.__file__).resolve().parents[3]
    # Pin the approved adapter by content (Git blob OID and SHA256 of LF-normalized
    # bytes), independent of Git history/subprocess; a future authorized adapter
    # change must explicitly update this pin. Whole-patch base preservation belongs
    # to the external manifest, not a permanent freeze of unrelated source.
    assert (root / ADAPTER_PATH).is_file()
    assert adapter_identity((root / ADAPTER_PATH).read_bytes()) == (
        ADAPTER_BLOB_OID, ADAPTER_BLOB_SHA256)
    for path in Path(package.__file__).parent.glob("*.py"):
        source = path.read_text()
        assert "f" + "api." not in source
        assert "/fapi/v1/time" not in source
        tree = ast.parse(source)
        imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imports += [alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names]
        assert not any(n.startswith(("crypto_trader.risk", "crypto_trader.execution",
                                     "crypto_trader.production", "crypto_trader.recovery",
                                     "crypto_trader.crew", "crypto_trader.agents")) for n in imports)
        assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr in ("get", "post", "request", "send")
                       and isinstance(n.func.value, ast.Name) and n.func.value.id == "requests"
                       for n in ast.walk(tree))


def test_entrypoint_second_instance_no_archive_records_or_poll(tmp_path, monkeypatch):
    from crypto_trader.collector import __main__ as entry
    from crypto_trader.collector import journal as j
    monkeypatch.setattr(sys, "argv", ["collector", "--archive", str(tmp_path),
                                     "--host-time-sync-acknowledged"])
    monkeypatch.setattr(entry, "guarded_import", lambda **kwargs:
                        (None, {"startup_validated": True}))
    monkeypatch.setattr(entry, "assert_process_isolation", lambda: None)
    monkeypatch.setattr(j, "Journal", lambda *args: pytest.fail("second instance wrote archive"))
    with j.ArchiveLock(tmp_path):
        assert entry.main() == p.EXIT_LOCKED
    assert not list(tmp_path.glob("*.darc"))


def test_failed_startup_entrypoint_never_starts_collection(tmp_path, monkeypatch):
    from crypto_trader.collector import __main__ as entry
    monkeypatch.setattr(sys, "argv", ["collector", "--archive", str(tmp_path)])
    monkeypatch.setattr(entry, "guarded_import", lambda **kwargs:
                        (None, {"startup_validated": False, "external_attempt_count": 1}))
    monkeypatch.setattr(s, "Collector", lambda *args, **kwargs: pytest.fail("polling after failed startup"))
    monkeypatch.setattr(entry, "assert_process_isolation", lambda: None)
    assert entry.main() == entry.STARTUP_FAILURE_EXIT
    assert not list(tmp_path.iterdir())


def test_entrypoint_rejects_loaded_analysis_agent(monkeypatch):
    from crypto_trader.collector import __main__ as entry
    monkeypatch.setitem(sys.modules, "crypto_trader.agents.fixture", object())
    with pytest.raises(RuntimeError, match="own_process"):
        entry.assert_process_isolation()


def test_exceptional_adapter_call_still_inspects_diagnostics(tmp_path):
    c, _, _ = make_collector(tmp_path)
    def failing(*args):
        c.client.observer_diagnostics.append(a.ObserverDiagnostic("before_transport", "id", "fixture"))
        raise ValueError("fixture")
    c.client.raw_delivery = failing
    c.poll(ds.Metric.CURRENT_OI)
    result = records(c.journal.path)
    assert result[-2]["record_type"] == "failed_poll"
    assert result[-1]["reason"] == "observer_diagnostics"
    c.journal.close()


def test_adapter_pin_is_mutation_sensitive():
    import crypto_trader.collector as package
    root = Path(package.__file__).resolve().parents[3]
    data = (root / ADAPTER_PATH).read_bytes()
    normalized = data.replace(b"\r\n", b"\n")
    assert adapter_identity(normalized.replace(b"\n", b"\r\n")) == (ADAPTER_BLOB_OID, ADAPTER_BLOB_SHA256)
    mutated = bytearray(normalized)
    mutated[len(mutated) // 2] ^= 1
    oid, digest = adapter_identity(bytes(mutated))
    assert oid != ADAPTER_BLOB_OID
    assert digest != ADAPTER_BLOB_SHA256


def _no_date(r, clock):
    del r.headers["Date"]


def _bad_date(r, clock):
    r.headers["Date"] = "not an http date"


def _timing_unavailable(r, clock):
    clock.mono -= 10 * p.NS_PER_SECOND  # negative elapsed time -> RTT unavailable


def _skewed(r, clock):
    r.headers["Date"] = http_date(clock.wall - 60_000)


def _future_quality(monkeypatch):
    monkeypatch.setattr(a, "_clock_evidence", lambda *args: ("future_quality_v2", None))


@pytest.mark.parametrize("quality,mutate", [
    ("http_date_missing", _no_date),
    ("http_date_malformed", _bad_date),
    ("clock_timing_unavailable", _timing_unavailable),
    ("clock_skew_exceeds_tolerance", _skewed),
    ("future_quality_v2", None),
])
def test_untrusted_clock_quality_fails_closed_then_trusted_poll_recovers(
        tmp_path, monkeypatch, quality, mutate):
    c, clock, _ = make_collector(tmp_path)
    def send(*_):
        r = response()
        if mutate is not None:
            mutate(r, clock)
        return r
    c.client.transport = send
    if mutate is None:
        with monkeypatch.context() as scoped:
            _future_quality(scoped)
            c.poll(ds.Metric.CURRENT_OI)
    else:
        c.poll(ds.Metric.CURRENT_OI)
    prior = records(c.journal.path)
    assert not any(r["record_type"] == "raw_observation" for r in prior)
    failed, gap = prior[-2], prior[-1]
    assert failed["record_type"] == "failed_poll"
    assert failed["reason"] == "untrusted_clock_quality"
    assert failed["response"]["clock_quality"] == quality
    assert failed["response"]["http_status"] == 200
    assert "available_at" not in failed and "fetched_at" not in failed
    assert gap["record_type"] == "collection_gap"
    assert gap["reason"] == "untrusted_clock_quality"
    assert gap["clock_quality"] == quality
    assert gap["response_id"] == failed["response"]["response_id"]
    # A later trusted poll yields a raw_observation and leaves prior bytes alone.
    clock.advance(90)
    def trusted(*_):
        assert records(c.journal.path)[:len(prior)] == prior
        return response()
    c.client.transport = trusted
    c.poll(ds.Metric.CURRENT_OI)
    after = records(c.journal.path)
    assert after[:len(prior)] == prior
    assert after[-1]["record_type"] == "raw_observation"
    assert after[-1]["clock_quality"] == TRUSTED
    c.journal.close()


def test_trusted_clock_quality_positive_control(tmp_path):
    c, _, _ = make_collector(tmp_path)
    c.poll(ds.Metric.CURRENT_OI)
    result = records(c.journal.path)
    assert result[-1]["record_type"] == "raw_observation"
    assert result[-1]["clock_quality"] == TRUSTED
    assert not any(r["record_type"] in ("failed_poll", "collection_gap") for r in result)
    c.journal.close()
