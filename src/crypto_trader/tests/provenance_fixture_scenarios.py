"""Deterministic offline scenarios shared by frozen base goldens and new tests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
import json
import socket
import threading
from types import SimpleNamespace

import pytest
import requests

from crypto_trader.tools import binance_futures_public_tool as ft
from crypto_trader.tools import derivatives_schema as ds

NOW = 1_791_080_000_000
SYMBOL = "ETHUSDT"
SCENARIOS = (
    "successful_network", "cache_hit", "shared_waiter", "timeout", "http_429",
    "http_5xx", "malformed_response", "local_rate_limit", "current_funding",
    "settled_funding", "snapshot_all_metrics", "http_date_429", "cooldown_denial",
    *(f"content_failure_{status}_{error}" for status in (200, 429, 503)
      for error in ("timeout", "connection", "attribute")),
    *(f"deferred_body_{error}" for error in ("timeout", "connection", "attribute")),
    "stateful_custom_content",
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("real network forbidden")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", denied)


class Clock:
    def __init__(self):
        self.ms, self.mono, self.sleeps = NOW, 0.0, []

    def wall(self):
        return self.ms / 1000

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.ms += int(seconds * 1000)
        self.mono += seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


class Response(requests.Response):
    """Complete content-decoded mock response, with independent parse latency."""
    def __init__(self, payload, *, status=200, clock=None, parse_delay=0,
                 malformed=False, content=None, headers=None):
        super().__init__()
        self.payload = deepcopy(payload)
        self.status_code = status
        self.headers = {"Date": format_datetime(datetime.fromtimestamp(NOW / 1000, timezone.utc), usegmt=True)}
        self.headers.update(headers or {})
        self._content = (json.dumps(payload, separators=(",", ":")).encode("utf-8")
                        if content is None else content)
        self.clock, self.parse_delay, self.malformed = clock, parse_delay, malformed

    def json(self):
        if self.clock and self.parse_delay:
            self.clock.advance(self.parse_delay)
        if self.malformed:
            raise ValueError("fixture malformed JSON")
        return deepcopy(self.payload)


def payloads():
    return {
        ds.Metric.FUNDING_INTERVAL: [{"symbol": SYMBOL, "fundingIntervalHours": 1}],
        ds.Metric.CURRENT_FUNDING: {"symbol": SYMBOL, "lastFundingRate": "0.0001",
            "nextFundingTime": NOW + 3599000, "markPrice": "2000", "indexPrice": "1999", "time": NOW},
        ds.Metric.SETTLED_FUNDING: [{"symbol": SYMBOL, "fundingRate": "0.0002",
            "markPrice": "1980", "fundingTime": NOW - 1000}],
        ds.Metric.CURRENT_OI: {"symbol": SYMBOL, "openInterest": "1234", "time": NOW - 1000},
        ds.Metric.HISTORICAL_OI: [{"symbol": SYMBOL, "sumOpenInterest": "1200",
            "sumOpenInterestValue": "2400000", "timestamp": NOW - 300000}],
        ds.Metric.GLOBAL_LONG_SHORT: [{"symbol": SYMBOL, "longShortRatio": "1.5000",
            "longAccount": "0.6000", "shortAccount": "0.4000", "timestamp": NOW - 300000}],
    }


def make_client(transport=None, *, observer=None):
    clock = Clock()
    state = ft.SharedPublicState(monotonic=clock.monotonic, wall=clock.wall, sleep=clock.sleep)
    data = payloads()
    by_path = {path: metric for metric, path in ft.ENDPOINTS.items()}
    calls = []

    def invoke(path, parameters):
        calls.append((path, deepcopy(parameters)))
        if transport is not None:
            return transport(path, parameters, clock, data)
        return Response(data[by_path[path]])

    options = {"state": state, "transport": invoke}
    if observer is not None:
        options["observer"] = observer
    return ft.BinanceFuturesPublicClient(**options), clock, data, calls


def concurrent_deliveries(client, invoke, monkeypatch, *, waiters=3):
    """Release owner only after every waiter enters the adapter's single flight."""
    started, release, ready = threading.Event(), threading.Event(), threading.Event()
    count_lock = threading.Lock()
    waiter_count = 0
    original = client.transport
    real_event = threading.Event

    class TrackedEvent(real_event):
        def wait(self, timeout=None):
            nonlocal waiter_count
            with count_lock:
                waiter_count += 1
                if waiter_count == waiters:
                    ready.set()
            return super().wait(timeout)

    def transport(path, parameters):
        started.set()
        assert release.wait(5), "owner release deadline"
        return original(path, parameters)

    client.transport = transport
    with monkeypatch.context() as patch:
        # Patch only adapter Event construction, never global threading internals.
        forwarding = SimpleNamespace(**vars(threading))
        forwarding.Event = TrackedEvent
        patch.setattr(ft, "threading", forwarding)
        with ThreadPoolExecutor(max_workers=waiters + 1) as pool:
            owner = pool.submit(invoke)
            assert started.wait(5), "owner transport did not start"
            shared = [pool.submit(invoke) for _ in range(waiters)]
            try:
                assert ready.wait(5), "waiters did not enter single flight"
            finally:
                release.set()
            return [owner.result(), *(future.result() for future in shared)]


def run_scenario(name, monkeypatch):
    errors = {"timeout": requests.exceptions.Timeout,
              "connection": requests.exceptions.ConnectionError, "attribute": AttributeError}

    def scenario_transport(path, parameters, clock, data):
        metric = next(metric for metric, endpoint in ft.ENDPOINTS.items() if endpoint == path)
        if name.startswith("content_failure_"):
            _, _, status, error = name.split("_")
            class UnsupportedResponse:
                status_code = int(status)
                headers = {"Retry-After": "3"}
                @property
                def content(self):
                    raise errors[error]()
                def json(self):
                    return deepcopy(data[metric])
            return UnsupportedResponse()
        if name.startswith("deferred_body_"):
            result = requests.Response()
            result.status_code, result.headers = 200, {}
            class DeferredStream:
                def stream(self, *args, **kwargs):
                    raise errors[name.rsplit("_", 1)[1]]()
                    yield b""
            result.raw = DeferredStream()
            return result
        if name == "stateful_custom_content":
            class StatefulResponse:
                status_code, headers = 200, {}
                remaining = iter([deepcopy(data[metric])])
                @property
                def content(self):
                    return json.dumps(next(self.remaining)).encode()
                def json(self):
                    return next(self.remaining)
            return StatefulResponse()
        if name == "http_date_429":
            header = format_datetime(datetime.fromtimestamp((NOW + 2000) / 1000, timezone.utc), usegmt=True)
            return Response(None, status=429, headers={"Retry-After": header})
        if name == "timeout":
            raise requests.exceptions.Timeout()
        if name == "http_429":
            return Response(None, status=429, headers={"Retry-After": "3"})
        if name == "http_5xx":
            return Response(None, status=503)
        if name == "malformed_response":
            return Response(None, malformed=True, content=b"{")
        return Response(data[metric], clock=clock, parse_delay=1 if name == "successful_network" else 0)

    client, clock, data, calls = make_client(scenario_transport)
    def observe(metric=ds.Metric.CURRENT_OI):
        return [obs.to_record() for obs in client.observations(metric, SYMBOL)]

    if name == "snapshot_all_metrics":
        output = {"snapshot": client.snapshot(SYMBOL)}
    elif name == "shared_waiter":
        output = {"deliveries": concurrent_deliveries(client, observe, monkeypatch)}
    elif name == "cache_hit":
        first = observe()
        clock.advance(10)
        output = {"deliveries": [first, observe()]}
    elif name == "cooldown_denial":
        client.state.cooldown(2)
        output = {"observations": observe()}
    elif name == "local_rate_limit":
        for _ in range(ds.LOCAL_MAX_RPM):
            assert client.state.reserve_attempt() is None
        output = {"observations": observe()}
    else:
        metric = {"current_funding": ds.Metric.CURRENT_FUNDING,
                  "settled_funding": ds.Metric.SETTLED_FUNDING}.get(name, ds.Metric.CURRENT_OI)
        output = {"observations": observe(metric)}
    return {"analysis": output, "transport_calls": len(calls), "sleeps": clock.sleeps}
