"""Network-free provider fixtures, single-flight concurrency and HTTP boundaries."""
from concurrent.futures import ThreadPoolExecutor
import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import format_datetime
import inspect
import json
from pathlib import Path
import socket
import threading
import textwrap
from unittest.mock import Mock

import pytest
import requests
from pydantic import ValidationError

from crypto_trader.tools import binance_futures_public_tool as ft
from crypto_trader.tools import derivatives_schema as ds

NOW = 1_791_080_000_000


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("network forbidden in derivatives tests")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


class Clock:
    def __init__(self):
        self.ms, self.mono, self.sleeps = NOW, 0.0, []

    def wall(self):
        return self.ms / 1000

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.mono += seconds
        self.ms += int(seconds * 1000)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


def response(payload=None, status=200, retry_after=None):
    result = Mock(status_code=status)
    result.headers = {} if retry_after is None else {"Retry-After": retry_after}
    result.json.return_value = deepcopy(payload)
    return result


@pytest.fixture
def fixture_client():
    clock = Clock()
    state = ft.SharedPublicState(monotonic=clock.monotonic, wall=clock.wall, sleep=clock.sleep)
    payloads = {
        ds.Metric.FUNDING_INTERVAL: [{"symbol": "ETHUSDT", "fundingIntervalHours": 1}],
        ds.Metric.CURRENT_FUNDING: {"symbol": "ETHUSDT", "lastFundingRate": "0.0001",
            "nextFundingTime": NOW + 3599000, "markPrice": "2000", "indexPrice": "1999", "time": NOW},
        ds.Metric.SETTLED_FUNDING: [{"symbol": "ETHUSDT", "fundingRate": "0.0002",
            "markPrice": "1980", "fundingTime": NOW - 1000}],
        ds.Metric.CURRENT_OI: {"symbol": "ETHUSDT", "openInterest": "1234", "time": NOW - 1000},
        ds.Metric.HISTORICAL_OI: [{"symbol": "ETHUSDT", "sumOpenInterest": "1200",
            "sumOpenInterestValue": "2400000", "timestamp": NOW - 300000}],
        ds.Metric.GLOBAL_LONG_SHORT: [{"symbol": "ETHUSDT", "longShortRatio": "1.5000",
            "longAccount": "0.6000", "shortAccount": "0.4000", "timestamp": NOW - 300000}],
    }
    path_metric = {path: metric for metric, path in ft.ENDPOINTS.items()}
    transport = Mock(side_effect=lambda path, params: response(payloads[path_metric[path]]))
    return ft.BinanceFuturesPublicClient(state=state, transport=transport), clock, payloads, transport


def one(fixture, metric=ds.Metric.CURRENT_OI, **kwargs):
    return fixture[0].observations(metric, "ETHUSDT", **kwargs)[0]


def test_current_funding_from_premium_index_is_separate_from_settled(fixture_client):
    client, _, _, transport = fixture_client
    current, settled = one(fixture_client, ds.Metric.CURRENT_FUNDING), one(fixture_client, ds.Metric.SETTLED_FUNDING)
    assert isinstance(current.value, ds.CurrentFundingContext)
    assert current.value.last_funding_rate == Decimal("0.0001")
    assert current.value.next_funding_time == NOW + 3599000
    assert current.value.mark_price == Decimal("2000")
    assert current.value.index_price == Decimal("1999") and current.value.time == NOW
    assert isinstance(settled.value, ds.SettledFunding) and settled.value.funding_rate == Decimal("0.0002")
    assert current.available_at == current.fetched_at == NOW and current.replay_eligible
    assert settled.available_at is None and not settled.replay_eligible
    assert {call.args[0] for call in transport.call_args_list} == {
        "/fapi/v1/fundingInfo", "/fapi/v1/premiumIndex", "/fapi/v1/fundingRate"}


@pytest.mark.parametrize("hours", [1, 4, 8, 12])
def test_funding_interval_per_symbol_not_hardcoded(fixture_client, hours):
    client, _, payloads, _ = fixture_client
    payloads[ds.Metric.FUNDING_INTERVAL][0]["fundingIntervalHours"] = hours
    payloads[ds.Metric.CURRENT_FUNDING]["nextFundingTime"] = NOW + hours * 3600000
    current = one(fixture_client, ds.Metric.CURRENT_FUNDING)
    assert current.status == ds.Status.AVAILABLE
    assert current.value.interval.hours == hours and current.value.interval.interval_source == "fundingInfo"
    assert current.max_age_seconds == min(ds.CURRENT_FUNDING_MAX_AGE_SECONDS, hours * 3600 / ds.CURRENT_FUNDING_AGE_CYCLE_DIVISOR)
    settled = one(fixture_client, ds.Metric.SETTLED_FUNDING)
    assert settled.max_age_seconds == hours * 3600 + ds.SETTLED_FUNDING_GRACE_SECONDS


def test_documented_default_fallback_has_version_and_provenance(fixture_client):
    client, _, payloads, _ = fixture_client
    payloads[ds.Metric.FUNDING_INTERVAL] = []
    interval = client.funding_interval("ETHUSDT")
    assert interval.status == ds.Status.AVAILABLE
    assert interval.value.hours == ds.DOCUMENTED_DEFAULT_FUNDING_HOURS
    assert interval.value.interval_source == "documented_default"
    assert interval.value.default_version == ds.DOCUMENTED_DEFAULT_VERSION
    assert interval.event_time is None and interval.available_at == interval.fetched_at
    assert "360033525031" in ds.DOCUMENTED_DEFAULT_URL


@pytest.mark.parametrize("payload", [
    {}, [{"symbol": "ETHUSDT"}], [{"symbol": "ETHUSDT", "fundingIntervalHours": 0}],
    [{"symbol": "", "fundingIntervalHours": 1}],
    [{"symbol": "  ", "fundingIntervalHours": 1}],
    [{"symbol": "ETHUSDT ", "fundingIntervalHours": 1}],
])
def test_failed_or_malformed_metadata_never_uses_default(fixture_client, payload):
    client, _, payloads, transport = fixture_client
    payloads[ds.Metric.FUNDING_INTERVAL] = payload
    metadata = client.funding_interval("ETHUSDT")
    assert metadata.status == ds.Status.UNAVAILABLE and metadata.value is None
    assert metadata.quality_reason == "malformed_funding_info"
    obs = one(fixture_client, ds.Metric.CURRENT_FUNDING)
    assert obs.status == ds.Status.UNAVAILABLE and obs.value is None
    assert obs.quality_reason == "funding_interval_unavailable"
    assert transport.call_count == 1


def test_soft_schedule_inconsistency_is_suspect_but_hard_invalid_is_unavailable(fixture_client):
    _, _, payloads, _ = fixture_client
    payloads[ds.Metric.CURRENT_FUNDING]["nextFundingTime"] = NOW + 3600001
    obs = one(fixture_client, ds.Metric.CURRENT_FUNDING)
    assert obs.status == ds.Status.SUSPECT and obs.value is not None
    assert obs.quality_reason == "funding_schedule_interval_mismatch"
    assert obs.to_record()["confidence_adjustment"] == "reduce"
    fixture_client[0].state.cache.clear()
    payloads[ds.Metric.CURRENT_FUNDING]["nextFundingTime"] = NOW
    obs = one(fixture_client, ds.Metric.CURRENT_FUNDING)
    assert obs.status == ds.Status.UNAVAILABLE and obs.value is None


@pytest.mark.parametrize("field", ["lastFundingRate", "nextFundingTime", "markPrice", "indexPrice", "time"])
def test_current_funding_missing_required_fields_fail_closed(fixture_client, field):
    del fixture_client[2][ds.Metric.CURRENT_FUNDING][field]
    assert one(fixture_client, ds.Metric.CURRENT_FUNDING).status == ds.Status.UNAVAILABLE


@pytest.mark.parametrize("metric", list(ds.HISTORICAL_METRICS))
def test_historical_endpoint_rows_are_never_strict_replay_eligible(fixture_client, metric):
    obs = one(fixture_client, metric)
    assert obs.status == ds.Status.AVAILABLE and obs.event_time < NOW
    assert obs.available_at is None and obs.availability_provenance == ds.HISTORICAL_PROVENANCE
    assert not obs.replay_eligible and ds.strict_replay([obs], NOW) == ()


def test_fresh_cache_cannot_reclassify_stale_observation(fixture_client):
    client, clock, payloads, transport = fixture_client
    payloads[ds.Metric.CURRENT_OI]["time"] = NOW - 119000
    first = one(fixture_client)
    assert first.status == ds.Status.AVAILABLE
    clock.advance(2)
    second = one(fixture_client)
    assert transport.call_count == 1
    assert second.status == ds.Status.UNAVAILABLE and second.value is None
    assert second.quality_reason == "stale_observation"
    assert second.fetched_at == first.fetched_at and second.available_at == first.available_at


@pytest.mark.parametrize("stamp", [None, "bad", True, NOW // 1000, NOW * 1000, NOW + 1])
def test_invalid_timestamp_or_provider_clock_ahead_fails_closed(fixture_client, stamp):
    fixture_client[2][ds.Metric.CURRENT_OI]["time"] = stamp
    obs = one(fixture_client)
    assert obs.status == ds.Status.UNAVAILABLE and obs.value is None


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", None])
def test_impossible_oi_is_unavailable(fixture_client, amount):
    fixture_client[2][ds.Metric.CURRENT_OI]["openInterest"] = amount
    assert one(fixture_client).status == ds.Status.UNAVAILABLE


@pytest.mark.parametrize("fields", [
    {"shortAccount": "0"}, {"longAccount": "1.2"}, {"longAccount": "0.9"},
    {"longShortRatio": "100"}, {"longShortRatio": "Infinity"},
])
def test_mathematically_impossible_ratios_fail_closed(fixture_client, fields):
    fixture_client[2][ds.Metric.GLOBAL_LONG_SHORT][0].update(fields)
    assert one(fixture_client, ds.Metric.GLOBAL_LONG_SHORT).status == ds.Status.UNAVAILABLE


def test_provider_rounding_is_checked_without_magic_ratio_tolerance(fixture_client):
    fixture_client[2][ds.Metric.GLOBAL_LONG_SHORT][0].update(
        longShortRatio="1.9408", longAccount="0.6599", shortAccount="0.3400")
    assert one(fixture_client, ds.Metric.GLOBAL_LONG_SHORT).status == ds.Status.AVAILABLE


@pytest.mark.parametrize("metric", [ds.Metric.HISTORICAL_OI, ds.Metric.GLOBAL_LONG_SHORT, ds.Metric.SETTLED_FUNDING])
def test_missing_historical_rows_return_unavailable(fixture_client, metric):
    fixture_client[2][metric] = []
    assert one(fixture_client, metric).quality_reason == "missing_observation"


@pytest.mark.parametrize("error,reason", [
    (requests.exceptions.Timeout("private provider message"), "timeout_retry_budget_exhausted"),
    (requests.exceptions.ConnectionError("private provider message"), "provider_down_retry_budget_exhausted"),
])
def test_timeout_or_provider_down_has_only_two_attempts_and_no_value(fixture_client, error, reason):
    transport = fixture_client[3]
    transport.side_effect = error
    obs = one(fixture_client)
    assert obs.status == ds.Status.UNAVAILABLE and obs.value is None and obs.quality_reason == reason
    assert transport.call_count == 2 and fixture_client[1].sleeps == []
    assert "private provider message" not in json.dumps(obs.to_record())


def test_retry_after_exactly_two_seconds_is_allowed_once(fixture_client):
    client, clock, payloads, transport = fixture_client
    transport.side_effect = [response(status=429, retry_after="2"), response(payloads[ds.Metric.CURRENT_OI])]
    obs = one(fixture_client)
    assert obs.status == ds.Status.AVAILABLE
    assert obs.available_at == obs.fetched_at == NOW + 2000
    assert clock.sleeps == [2.0] and transport.call_count == 2
    assert len(client.state.attempts) == 2


@pytest.mark.parametrize("header", ["2.001", "10000000000000000000000", "Fri, 31 Dec 9999 23:59:59 GMT"])
def test_retry_after_over_two_seconds_is_immediate_and_blocks_other_keys(fixture_client, header):
    client, clock, _, transport = fixture_client
    transport.return_value = response(status=429, retry_after=header)
    transport.side_effect = None
    obs = one(fixture_client)
    assert obs.quality_reason == "rate_limited_retry_after_exceeds_budget"
    assert clock.sleeps == [] and transport.call_count == 1
    other = client.observations(ds.Metric.CURRENT_OI, "BTCUSDT")[0]
    assert other.quality_reason == "provider_rate_limit_cooldown" and transport.call_count == 1


@pytest.mark.parametrize("header", [None, "nan", "inf", "-1", "garbage", "1e999999"])
def test_unknown_retry_after_is_no_retry_and_process_cooldown(fixture_client, header):
    client, clock, _, transport = fixture_client
    transport.side_effect = None
    transport.return_value = response(status=429, retry_after=header)
    assert one(fixture_client).quality_reason == "rate_limited_retry_after_unknown"
    assert transport.call_count == 1 and clock.sleeps == []
    assert client.state.cooldown_until == ds.UNKNOWN_RATE_LIMIT_COOLDOWN_SECONDS


def test_http_date_retry_after_is_bounded(fixture_client):
    client, clock, payloads, transport = fixture_client
    date = datetime.fromtimestamp(NOW / 1000, timezone.utc) + timedelta(seconds=2)
    transport.side_effect = [response(status=429, retry_after=format_datetime(date, usegmt=True)), response(payloads[ds.Metric.CURRENT_OI])]
    assert one(fixture_client).status == ds.Status.AVAILABLE and clock.sleeps == [2]


@pytest.mark.parametrize("status,calls,reason", [(500, 2, "http_retry_budget_exhausted"),
    (418, 1, "provider_ip_banned"), (403, 1, "provider_http_error"), (302, 1, "provider_http_error")])
def test_http_failures_are_bounded(fixture_client, status, calls, reason):
    transport = fixture_client[3]
    transport.side_effect = None
    transport.return_value = response(status=status)
    obs = one(fixture_client)
    assert obs.status == ds.Status.UNAVAILABLE and obs.quality_reason == reason
    assert transport.call_count == calls and fixture_client[1].sleeps == []


def test_second_429_exhausts_retry_budget_without_second_sleep(fixture_client):
    transport = fixture_client[3]
    transport.side_effect = [response(status=429, retry_after="2"), response(status=429, retry_after="2")]
    assert one(fixture_client).quality_reason == "http_retry_budget_exhausted"
    assert fixture_client[1].sleeps == [2] and transport.call_count == 2


def test_malformed_json_has_no_retry(fixture_client):
    transport = fixture_client[3]
    bad = response()
    bad.json.side_effect = ValueError("secret body")
    transport.side_effect = None
    transport.return_value = bad
    assert one(fixture_client).quality_reason == "malformed_json" and transport.call_count == 1


def test_local_rate_limit_exhaustion_sends_nothing(fixture_client):
    client, _, _, transport = fixture_client
    for _ in range(ds.LOCAL_MAX_RPM):
        assert client.state.reserve_attempt() is None
    assert one(fixture_client).quality_reason == "local_rate_limit_exhausted"
    assert transport.call_count == 0


def test_failed_response_ttl_equals_success_ttl(fixture_client):
    client, clock, _, transport = fixture_client
    transport.side_effect = requests.exceptions.Timeout()
    assert one(fixture_client).status == ds.Status.UNAVAILABLE and transport.call_count == 2
    clock.advance(ds.CURRENT_OI_TTL_SECONDS - 1)
    assert one(fixture_client).status == ds.Status.UNAVAILABLE and transport.call_count == 2
    clock.advance(1)
    assert one(fixture_client).status == ds.Status.UNAVAILABLE and transport.call_count == 4


def test_admission_rejects_third_active_symbol_and_lease_expires(fixture_client):
    client, clock, _, transport = fixture_client
    for symbol in ("ETHUSDT", "BTCUSDT"):
        assert client.state.admit(symbol)
    obs = client.observations(ds.Metric.CURRENT_OI, "SOLUSDT")[0]
    assert obs.quality_reason == "steady_state_request_budget_exceeded" and transport.call_count == 0
    assert "SOLUSDT" not in client.state.leases
    clock.advance(ds.ADMISSION_LEASE_SECONDS)
    assert client.state.admit("SOLUSDT") and len(client.state.leases) == 1


def test_fast_polling_configuration_fails_closed():
    with pytest.raises(ValueError, match="request_budget"):
        ft.SharedPublicState(ds.PollingPolicy(1, 1, 1, 1, 1, 1))


def test_shared_cache_prevents_duplicate_requests_across_tool_instances(fixture_client, monkeypatch):
    client, _, _, transport = fixture_client
    monkeypatch.setattr(ft, "SHARED_STATE", client.state)
    monkeypatch.setattr(ft, "_public_get", transport)
    first, second = ft.BinanceFuturesPublicTool(), ft.BinanceFuturesPublicTool()
    assert json.loads(first._run("ETHUSDT"))["status"] == "available"
    assert json.loads(second._run("ETHUSDT"))["status"] == "available"
    assert transport.call_count == 6


def test_same_key_concurrency_is_single_flight(fixture_client):
    client, _, payloads, transport = fixture_client
    entered, release = threading.Event(), threading.Event()
    def delayed(path, params):
        entered.set()
        assert release.wait(2)
        return response(payloads[ds.Metric.CURRENT_OI])
    transport.side_effect = delayed
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(one, fixture_client)
        assert entered.wait(2)
        rest = [pool.submit(one, fixture_client) for _ in range(3)]
        release.set()
        results = [first.result(timeout=3), *(item.result(timeout=3) for item in rest)]
    assert all(obs.status == ds.Status.AVAILABLE for obs in results) and transport.call_count == 1


def test_different_keys_do_not_hold_global_lock_during_io(fixture_client):
    client, _, payloads, transport = fixture_client
    barrier = threading.Barrier(2)
    def parallel(path, params):
        barrier.wait(timeout=2)
        payload = deepcopy(payloads[ds.Metric.CURRENT_OI])
        payload["symbol"] = params["symbol"]
        return response(payload)
    transport.side_effect = parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.observations, ds.Metric.CURRENT_OI, symbol) for symbol in ("ETHUSDT", "BTCUSDT")]
        assert all(future.result(timeout=3)[0].status == ds.Status.AVAILABLE for future in futures)
    assert transport.call_count == 2


def test_cache_full_query_key_and_closed_history_ttl(fixture_client):
    client, clock, _, transport = fixture_client
    first = one(fixture_client, ds.Metric.HISTORICAL_OI, end_time=NOW)
    clock.advance(61)
    again = one(fixture_client, ds.Metric.HISTORICAL_OI, end_time=NOW)
    assert again.fetched_at == first.fetched_at and transport.call_count == 1
    one(fixture_client, ds.Metric.HISTORICAL_OI, period="15m", end_time=NOW)
    assert transport.call_count == 2


def test_transport_has_no_credentials_headers_cookies_or_environment_auth(monkeypatch, tmp_path):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine fapi.binance.com login forbidden password sentinel", encoding="utf-8")
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("HTTPS_PROXY", "http://invalid.proxy:1")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "missing.pem"))
    captured = []
    def send(session, prepared, **kwargs):
        captured.append((session, prepared, kwargs))
        result = requests.Response()
        result.status_code = 200
        result._content = b'{}'
        return result
    monkeypatch.setattr(requests.Session, "send", send)
    ft._public_get(ft.ENDPOINTS[ds.Metric.CURRENT_OI], {"symbol": "ETHUSDT"})
    session, prepared, kwargs = captured[0]
    assert not session.trust_env and prepared.method == "GET"
    assert "Authorization" not in prepared.headers and "X-MBX-APIKEY" not in prepared.headers
    assert "Cookie" not in prepared.headers and "signature" not in prepared.url and "timestamp" not in prepared.url
    assert kwargs["timeout"] == 3 and kwargs["verify"] is True and not kwargs["allow_redirects"]
    assert not kwargs.get("proxies")
    with pytest.raises(ValueError, match="endpoint_not_allowed"):
        ft._public_get("/fapi/v1/order", {})
    for forbidden in ("signature", "timestamp", "api_key"):
        with pytest.raises(ValueError, match="query_parameter_not_allowed"):
            ft._public_get(ft.ENDPOINTS[ds.Metric.CURRENT_OI], {"symbol": "ETHUSDT", forbidden: "bad"})
    assert len(captured) == 1


def test_adapter_source_has_no_auth_or_environment_credential_access():
    source = "\n".join(Path(module.__file__).read_text(encoding="utf-8").lower() for module in (ft, ds))
    for token in ("api_key", "api_secret", "x-mbx-apikey", "authorization", "os.environ", "os.getenv", "hmac", "requests.post", "requests.put", "requests.delete", "session.post", "session.put", "session.patch", "session.delete", "session.request", "positionrisk", "toplongshort"):
        assert token not in source
    normalize = inspect.getsource(ft.BinanceFuturesPublicClient._normalize)
    resolve = inspect.getsource(ft.BinanceFuturesPublicClient.funding_interval)
    tree = ast.parse(textwrap.dedent(normalize))
    other = ast.parse(textwrap.dedent(resolve))
    assert not any(isinstance(node, ast.Constant) and type(node.value) is int and node.value == 8
                   for node in list(ast.walk(tree)) + list(ast.walk(other)))
    assert ft.ENDPOINTS == {
        ds.Metric.SETTLED_FUNDING: "/fapi/v1/fundingRate",
        ds.Metric.CURRENT_FUNDING: "/fapi/v1/premiumIndex",
        ds.Metric.FUNDING_INTERVAL: "/fapi/v1/fundingInfo",
        ds.Metric.CURRENT_OI: "/fapi/v1/openInterest",
        ds.Metric.HISTORICAL_OI: "/futures/data/openInterestHist",
        ds.Metric.GLOBAL_LONG_SHORT: "/futures/data/globalLongShortAccountRatio",
    }
    with pytest.raises(ValidationError):
        ft.BinanceFuturesPublicInput(symbol="ETHUSDT", headers={"Authorization": "bad"})


def test_fetched_at_is_sampled_after_body_receipt(fixture_client):
    client, clock, payloads, transport = fixture_client
    result = response()
    def body():
        clock.advance(1)
        return deepcopy(payloads[ds.Metric.CURRENT_OI])
    result.json.side_effect = body
    transport.side_effect = None
    transport.return_value = result
    obs = one(fixture_client)
    assert obs.fetched_at == obs.available_at == NOW + 1000


@pytest.mark.parametrize("metric", [ds.Metric.HISTORICAL_OI, ds.Metric.GLOBAL_LONG_SHORT])
def test_period_freshness_uses_period_not_cache_age(fixture_client, metric):
    _, _, payloads, _ = fixture_client
    age = ds.PERIOD_SECONDS[ds.DEFAULT_PERIOD] * ds.PERIOD_MAX_AGE_MULTIPLIER + ds.PERIOD_MAX_AGE_GRACE_SECONDS
    payloads[metric][0]["timestamp"] = NOW - int(age * 1000) - 1
    assert one(fixture_client, metric).quality_reason == "stale_observation"


def test_settled_timestamp_after_current_context_is_unavailable(fixture_client):
    client, clock, payloads, _ = fixture_client
    clock.advance(10)
    payloads[ds.Metric.SETTLED_FUNDING][0]["fundingTime"] = NOW + 1
    result = client.snapshot("ETHUSDT")["observations"][ds.Metric.CURRENT_FUNDING.value]
    assert result["status"] == "unavailable" and result["value"] is None
    assert result["quality_reason"] == "settled_funding_after_current_context"


def test_in_flight_wait_is_explicitly_bounded(fixture_client):
    client, _, _, transport = fixture_client
    key = (ds.PROVIDER, "ETHUSDT", ds.Metric.CURRENT_OI.value, None, (("symbol", "ETHUSDT"),))
    event = Mock()
    event.wait.return_value = False
    client.state.in_flight[key] = event
    assert one(fixture_client).quality_reason == "in_flight_wait_budget_exhausted"
    event.wait.assert_called_once_with(timeout=ds.IN_FLIGHT_WAIT_SECONDS)
    assert transport.call_count == 0


def test_wall_clock_does_not_expire_monotonic_cache(fixture_client):
    client, clock, _, transport = fixture_client
    first = one(fixture_client)
    clock.ms += 121000
    cached = one(fixture_client)
    assert transport.call_count == 1 and cached.fetched_at == first.fetched_at
    assert cached.status == ds.Status.UNAVAILABLE


def test_wall_clock_rollback_cannot_make_future_availability_usable(fixture_client):
    _, clock, _, transport = fixture_client
    one(fixture_client)
    clock.ms -= 500
    cached = one(fixture_client)
    assert transport.call_count == 1
    assert cached.status == ds.Status.UNAVAILABLE and cached.value is None
    assert cached.quality_reason == "observation_after_decision_time"


def test_snapshot_fixture_replay_counts_and_all_endpoints(fixture_client):
    records = fixture_client[0].snapshot("ETHUSDT")["observations"]
    assert len(records) == 6
    assert sum(item["replay_eligible"] for item in records.values()) == 3
    assert sum(not item["replay_eligible"] for item in records.values()) == 3
    assert {call.args[0] for call in fixture_client[3].call_args_list} == set(ft.ENDPOINTS.values())


def test_next_funding_and_latest_settlement_interval_mismatch_is_suspect(fixture_client):
    client, _, payloads, _ = fixture_client
    payloads[ds.Metric.SETTLED_FUNDING][0]["fundingTime"] = NOW - 3600000
    snapshot = client.snapshot("ETHUSDT")
    record = snapshot["observations"][ds.Metric.CURRENT_FUNDING.value]
    assert record["status"] == "available" and record["replay_eligible"]
    assert snapshot["status"] == "suspect" and snapshot["confidence_adjustment"] == "reduce"
    check = snapshot["quality_checks"][0]
    assert check["quality_reason"] == "funding_settlement_schedule_mismatch"
    assert check["status"] == "suspect" and check["available_at"] is None
    assert not check["replay_eligible"] and not snapshot["replay_eligible"]


def test_snapshot_reassesses_earlier_metrics_at_delivery(fixture_client):
    client, clock, payloads, transport = fixture_client
    by_path = {path: metric for metric, path in ft.ENDPOINTS.items()}
    def delayed(path, params):
        clock.advance(20)
        return response(payloads[by_path[path]])
    transport.side_effect = delayed
    records = client.snapshot("ETHUSDT")["observations"]
    assert records[ds.Metric.CURRENT_FUNDING.value]["status"] == "unavailable"
    assert records[ds.Metric.CURRENT_FUNDING.value]["value"] is None
    assert records[ds.Metric.CURRENT_OI.value]["status"] == "unavailable"


def test_no_send_local_denial_is_not_cached_after_limiter_recovers(fixture_client):
    client, clock, _, transport = fixture_client
    for _ in range(ds.LOCAL_MAX_RPM):
        assert client.state.reserve_attempt() is None
    assert one(fixture_client).quality_reason == "local_rate_limit_exhausted"
    assert client.state.cache == {} and transport.call_count == 0
    clock.advance(ds.LOCAL_RATE_WINDOW_SECONDS)
    assert one(fixture_client).status == ds.Status.AVAILABLE and transport.call_count == 1


def test_no_send_cooldown_cannot_poison_shared_funding_info(fixture_client):
    client, clock, _, transport = fixture_client
    client.state.cooldown(2)
    assert client.funding_interval("ETHUSDT").quality_reason == "provider_rate_limit_cooldown"
    assert client.state.cache == {} and transport.call_count == 0
    clock.advance(2)
    assert client.funding_interval("ETHUSDT").status == ds.Status.AVAILABLE and transport.call_count == 1


def test_failed_metadata_recovers_at_negative_ttl(fixture_client):
    client, clock, payloads, transport = fixture_client
    transport.side_effect = requests.exceptions.Timeout()
    assert client.funding_interval("ETHUSDT").status == ds.Status.UNAVAILABLE
    assert transport.call_count == 2
    transport.side_effect = lambda path, params: response(payloads[ds.Metric.FUNDING_INTERVAL])
    clock.advance(ds.FAILURE_TTL_SECONDS - 1)
    assert client.funding_interval("ETHUSDT").status == ds.Status.UNAVAILABLE and transport.call_count == 2
    clock.advance(1)
    assert client.funding_interval("ETHUSDT").status == ds.Status.AVAILABLE and transport.call_count == 3


def test_failed_closed_history_uses_negative_not_hour_ttl(fixture_client):
    client, clock, payloads, transport = fixture_client
    transport.side_effect = requests.exceptions.ConnectionError()
    assert one(fixture_client, ds.Metric.HISTORICAL_OI, end_time=NOW).status == ds.Status.UNAVAILABLE
    entry = next(iter(client.state.cache.values()))
    assert entry.expires_at == ds.FAILURE_TTL_SECONDS < ds.CLOSED_HISTORY_TTL_SECONDS
    clock.advance(ds.FAILURE_TTL_SECONDS)
    transport.side_effect = lambda path, params: response(payloads[ds.Metric.HISTORICAL_OI])
    assert one(fixture_client, ds.Metric.HISTORICAL_OI, end_time=NOW).status == ds.Status.AVAILABLE


def test_cross_quality_assessment_does_not_backdate_a_live_replay_label(fixture_client):
    client, clock, payloads, transport = fixture_client
    primitive = one(fixture_client, ds.Metric.CURRENT_FUNDING)
    payloads[ds.Metric.SETTLED_FUNDING][0]["fundingTime"] = NOW - 3600000
    clock.advance(10)
    snapshot = client.snapshot("ETHUSDT")
    check = snapshot["quality_checks"][0]
    assert check["assessed_at"] == NOW + 10000
    assert check["dependencies"][1]["fetched_at"] > primitive.fetched_at
    assert check["dependencies"][1]["available_at"] is None
    assert not check["replay_eligible"] and not snapshot["replay_eligible"]
    current = snapshot["observations"][ds.Metric.CURRENT_FUNDING.value]
    assert current["status"] == primitive.status.value == "available"
    assert current["available_at"] == primitive.available_at and current["quality_reason"] is None
    assert ds.strict_replay([primitive], NOW) == (primitive,)


def test_hard_cross_contradiction_suppresses_only_delivered_view(fixture_client):
    client, clock, payloads, _ = fixture_client
    primitive = one(fixture_client, ds.Metric.CURRENT_FUNDING)
    clock.advance(10)
    payloads[ds.Metric.SETTLED_FUNDING][0]["fundingTime"] = NOW + 1
    snapshot = client.snapshot("ETHUSDT")
    current = snapshot["observations"][ds.Metric.CURRENT_FUNDING.value]
    assert current["status"] == "unavailable" and current["value"] is None and not current["replay_eligible"]
    assert not snapshot["replay_eligible"] and not snapshot["quality_checks"][0]["replay_eligible"]
    assert one(fixture_client, ds.Metric.CURRENT_FUNDING) == primitive


def test_near_ttl_metadata_survives_nominal_request_latency(fixture_client):
    client, clock, payloads, transport = fixture_client
    client.funding_interval("ETHUSDT")
    clock.advance(ds.FUNDING_INFO_TTL_SECONDS - 1)
    def delayed(path, params):
        clock.advance(2)
        payloads[ds.Metric.CURRENT_FUNDING]["time"] = clock.ms
        payloads[ds.Metric.CURRENT_FUNDING]["nextFundingTime"] = clock.ms + 3600000
        return response(payloads[ds.Metric.CURRENT_FUNDING])
    transport.side_effect = delayed
    current = one(fixture_client, ds.Metric.CURRENT_FUNDING)
    assert current.status == ds.Status.AVAILABLE and current.value.interval.fetched_at == NOW
    assert current.fetched_at == NOW + 61000


@pytest.mark.parametrize("metric", list(ds.HISTORICAL_METRICS))
def test_exploratory_history_preserves_many_old_valid_rows(fixture_client, metric, monkeypatch):
    client, _, payloads, _ = fixture_client
    original = payloads[metric][0]
    timestamp = "fundingTime" if metric == ds.Metric.SETTLED_FUNDING else "timestamp"
    rows = []
    for offset in (10, 9, 8, 7, 6):
        row = deepcopy(original)
        row[timestamp] = NOW - offset * 3600000
        rows.append(row)
    payloads[metric] = rows
    current_view = client.observations(metric, "ETHUSDT", limit=5)
    assert all(obs.status == ds.Status.UNAVAILABLE for obs in current_view)
    monkeypatch.setattr(ft, "BinanceFuturesPublicClient", lambda: client)
    result = json.loads(ft.BinanceFuturesPublicTool()._run("ETHUSDT", action="history", metric=metric, limit=5))
    assert result["freshness_context"] == "historical_range_no_live_freshness"
    assert len(result["observations"]) == 5 and result["usage"] == "exploratory_only"
    assert all(obs["status"] == "available" and obs["value"] is not None for obs in result["observations"])
    assert all(obs["available_at"] is None and not obs["replay_eligible"] for obs in result["observations"])


@pytest.mark.parametrize("change", [{"timestamp": NOW + 1}, {"timestamp": NOW // 1000}, {"sumOpenInterest": "NaN"}])
def test_exploratory_history_still_rejects_hard_invalid_fields(fixture_client, change):
    client, _, payloads, _ = fixture_client
    payloads[ds.Metric.HISTORICAL_OI][0].update(change)
    rows = client.observations(ds.Metric.HISTORICAL_OI, "ETHUSDT", exploratory=True, end_time=NOW)
    assert all(obs.status == ds.Status.UNAVAILABLE and obs.value is None for obs in rows)


def test_exploratory_flag_is_forbidden_for_live_and_snapshot_still_checks_age(fixture_client):
    client, _, payloads, _ = fixture_client
    assert one(fixture_client, ds.Metric.CURRENT_OI, exploratory=True).quality_reason == "invalid_query"
    payloads[ds.Metric.HISTORICAL_OI][0]["timestamp"] = NOW - 10 * 3600000
    rows = client.snapshot("ETHUSDT")["observations"]
    assert rows[ds.Metric.HISTORICAL_OI.value]["status"] == "unavailable"


def test_unexpected_transport_error_is_structured_and_single_flight_released(fixture_client):
    client, _, _, transport = fixture_client
    transport.side_effect = RuntimeError("secret transport detail")
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: one(fixture_client), range(4)))
    assert all(obs.status == ds.Status.UNAVAILABLE and obs.quality_reason == "internal_error" for obs in rows)
    assert transport.call_count == 1 and client.state.in_flight == {}
    assert "secret" not in json.dumps([obs.to_record() for obs in rows])


def test_unexpected_snapshot_error_is_structured_without_message(monkeypatch):
    client = Mock()
    client.snapshot.side_effect = RuntimeError("secret snapshot detail")
    monkeypatch.setattr(ft, "BinanceFuturesPublicClient", lambda: client)
    record = json.loads(ft.BinanceFuturesPublicTool()._run("ETHUSDT"))
    assert record["status"] == "unavailable" and record["value"] is None and record["quality_reason"] == "internal_error"
    assert "secret" not in json.dumps(record)


def test_current_funding_cache_expires_at_schedule_without_mutating_raw(fixture_client):
    client, clock, payloads, transport = fixture_client
    payloads[ds.Metric.CURRENT_FUNDING]["nextFundingTime"] = NOW + 1000
    first = one(fixture_client, ds.Metric.CURRENT_FUNDING)
    raw = next(entry.result for key, entry in client.state.cache.items() if key[2] == ds.Metric.CURRENT_FUNDING.value)
    original = deepcopy(raw.payload)
    assert first.status == ds.Status.AVAILABLE
    clock.advance(2)
    second = one(fixture_client, ds.Metric.CURRENT_FUNDING)
    assert second.status == ds.Status.UNAVAILABLE and second.value is None
    assert second.quality_reason == "stale_funding_context_after_scheduled_time"
    assert second.fetched_at == second.available_at == first.fetched_at
    assert transport.call_count == 2  # fundingInfo + premiumIndex, no forced refetch.
    assert raw.payload == original and first.value is not None


def test_current_context_already_expired_at_body_receipt_is_unavailable(fixture_client):
    client, clock, payloads, transport = fixture_client
    payloads[ds.Metric.CURRENT_FUNDING]["nextFundingTime"] = NOW + 1000
    by_path = {path: metric for metric, path in ft.ENDPOINTS.items()}
    def delayed(path, params):
        result = response(payloads[by_path[path]])
        if by_path[path] == ds.Metric.CURRENT_FUNDING:
            def decode():
                clock.advance(2)
                return deepcopy(payloads[ds.Metric.CURRENT_FUNDING])
            result.json.side_effect = decode
        return result
    transport.side_effect = delayed
    row = one(fixture_client, ds.Metric.CURRENT_FUNDING)
    assert row.status == ds.Status.UNAVAILABLE and row.value is None
    assert row.available_at == row.fetched_at == NOW + 2000
    assert row.quality_reason == "stale_funding_context_after_scheduled_time"


@pytest.mark.parametrize("action", ["snapshot", "history", "invalid_query", "internal_error"])
def test_every_tool_payload_keeps_strict_ab_gate_closed(fixture_client, monkeypatch, action):
    client = fixture_client[0]
    monkeypatch.setattr(ft, "BinanceFuturesPublicClient", lambda: client)
    tool = ft.BinanceFuturesPublicTool()
    if action == "internal_error":
        monkeypatch.setattr(client, "snapshot", Mock(side_effect=RuntimeError("private error")))
        text = tool._run("ETHUSDT")
    elif action == "history":
        text = tool._run("ETHUSDT", action="history", metric=ds.Metric.SETTLED_FUNDING)
    elif action == "invalid_query":
        text = tool._run("ETHUSDT", action="invalid")
    else:
        text = tool._run("ETHUSDT")
    payload = json.loads(text)
    gate = payload["strict_ab_adoption_gate"]
    assert gate["status"] == ds.STRICT_AB_ADOPTION_STATUS and gate["eligible"] is False
    assert gate["counts_are_not_adoption_evidence"] is True
    json.dumps(payload, allow_nan=False)
    if action == "snapshot":
        assert gate["total_observation_count"] == 6
        assert gate["replay_eligible_observation_count"] == 3
        assert gate["replay_ineligible_observation_count"] == 3
        assert gate["historical_exploratory_observation_count"] == 3
    elif action == "history":
        assert payload["usage"] == "exploratory_only"
        assert gate["replay_eligible_observation_count"] == 0
    else:
        assert payload["status"] == "unavailable" and gate["total_observation_count"] == 0


def test_adoption_counts_are_recomputed_after_cached_context_expiry(fixture_client):
    client, clock, payloads, transport = fixture_client
    payloads[ds.Metric.CURRENT_FUNDING]["nextFundingTime"] = NOW + 1000
    before = client.snapshot("ETHUSDT")
    calls = transport.call_count
    clock.advance(2)
    after = client.snapshot("ETHUSDT")
    assert transport.call_count == calls
    assert before["strict_ab_adoption_gate"]["replay_eligible_observation_count"] == 3
    assert after["strict_ab_adoption_gate"]["replay_eligible_observation_count"] == 2
    assert before["strict_ab_adoption_gate"]["eligible"] is False
    assert after["strict_ab_adoption_gate"]["eligible"] is False
