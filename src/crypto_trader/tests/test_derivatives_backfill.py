"""Offline regressions for public-only, one-shot derivatives backfill."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "research" / "derivatives_backfill.py"
SPEC = importlib.util.spec_from_file_location("research_derivatives_backfill", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b)

BASE = 1_780_000_000_000
NOW = ((BASE + b.WINDOW_MS) // b.STEP_MS) * b.STEP_MS


class FixtureTransport:
    def __init__(self, *, missing=False):
        self.calls = []
        self.missing = missing

    def __call__(self, endpoint, params):
        self.calls.append((endpoint, dict(params)))
        assert endpoint in [value[0] for value in b.ENDPOINTS.values()]
        assert params["symbol"] == "ETHUSDT"
        if endpoint == "/fapi/v1/fundingRate":
            assert params["limit"] <= 1000
            assert params["startTime"] >= 0
            return [
                {"symbol": "ETHUSDT", "fundingTime": value,
                 "fundingRate": "0.00010000", "markPrice": "2500.0"}
                for value in (BASE - 8 * 60 * 60 * 1000, BASE, BASE + 8 * 60 * 60 * 1000)
                if params["startTime"] <= value <= params["endTime"]
            ]
        assert params["period"] == "5m"
        assert params["limit"] <= 500
        assert params["endTime"] - params["startTime"] < 500 * b.STEP_MS
        start = ((params["startTime"] + b.STEP_MS - 1) // b.STEP_MS) * b.STEP_MS
        rows = []
        for timestamp in range(start, params["endTime"] + 1, b.STEP_MS):
            if self.missing and (timestamp // b.STEP_MS) % 100 == 0:
                continue
            if endpoint.endswith("openInterestHist"):
                rows.append({"symbol": "ETHUSDT", "timestamp": timestamp,
                             "sumOpenInterest": "123.0", "sumOpenInterestValue": "234.0"})
            else:
                rows.append({"symbol": "ETHUSDT", "timestamp": timestamp, "longShortRatio": "1.0",
                             "longAccount": "0.5", "shortAccount": "0.5"})
        return rows


def test_provider_time_anchors_capture_without_trusting_host_clock(tmp_path):
    class Clocked(FixtureTransport, b.PublicTransport):
        def server_time_ms(self):
            return NOW

    result = b.capture(tmp_path / "backfill", transport=Clocked(), now_ms=None)
    assert result["all_captured"]
    assert result["clock_source"] == "binance_server_time"
    assert result["as_of"] == b.iso(NOW)


def test_three_endpoints_fully_paged_30_days_and_earliest_funding(tmp_path):
    transport = FixtureTransport()
    report = b.capture(tmp_path / "backfill", transport=transport, now_ms=NOW)
    assert report["all_captured"] is True
    assert list(report["results"]) == list(b.ENDPOINTS)
    oi = report["results"]["openInterestHist"]
    ratio = report["results"]["globalLongShortAccountRatio"]
    funding = report["results"]["fundingRate"]
    assert oi["rows"] == ratio["rows"] == 8629
    assert funding["rows"] == 3
    assert oi["requests"] == ratio["requests"] == 18
    assert 25 <= funding["requests"] <= b.FUNDING_MAX_PAGES
    funding_calls = [params for endpoint, params in transport.calls if endpoint.endswith("fundingRate")]
    assert all(params["endTime"] - params["startTime"] < 90 * b.DAY_MS for params in funding_calls)
    assert min(params["startTime"] for params in funding_calls) == 1_514_764_800_000
    assert all(x["gap_count"] == 0 for x in (oi, ratio, funding))
    assert len(transport.calls) == 36 + funding["requests"]
    assert all(".darc" not in x.name for x in (tmp_path / "backfill").iterdir())
    output = json.loads((tmp_path / "backfill" / oi["file"]).read_text("utf-8"))
    assert len(output["records"]) == 8629
    assert output["metadata"]["note"].startswith("historical_api_availability_unknown")


def test_stats_window_keeps_one_hour_margin_against_rolling_30_day_400():
    transport = FixtureTransport()
    rows, report = b.retrieve_stats("globalLongShortAccountRatio", NOW, transport)
    start = NOW - b.WINDOW_MS + b.RETENTION_MARGIN_MS
    assert b.RETENTION_MARGIN_MS == 3_600_000
    assert transport.calls[0][1]["startTime"] >= start
    assert report["retention_safety_margin_minutes"] == 60
    assert rows[0]["timestamp"] >= start
    assert rows[-1]["timestamp"] == NOW


def test_missing_5m_intervals_reported_explicitly(tmp_path):
    report = b.capture(tmp_path / "backfill", transport=FixtureTransport(missing=True), now_ms=NOW)
    assert report["results"]["openInterestHist"]["gap_count"] > 0
    assert report["results"]["globalLongShortAccountRatio"]["gap_count"] > 0
    assert all(g["interpretation"] == "missing_5m_intervals" for g in report["results"]["openInterestHist"]["gaps"])


def test_provider_symbol_mismatch_is_not_saved():
    with pytest.raises(RuntimeError, match="unexpected_symbol"):
        b.validate_rows("openInterestHist", [{"symbol": "BTCUSDT", "timestamp": NOW}], NOW - b.STEP_MS, NOW)


def test_provider_out_of_window_fails_closed():
    with pytest.raises(RuntimeError, match="provider_timestamp_outside_requested_window"):
        b.validate_rows("openInterestHist", [{"symbol": "ETHUSDT", "timestamp": NOW + 1}], NOW, NOW)


def test_nonobject_response_rejected():
    with pytest.raises(RuntimeError, match="unexpected_symbol"):
        b.validate_rows("globalLongShortAccountRatio", [{"timestamp": NOW}], NOW, NOW)


def test_unauthorized_endpoint_or_symbol_not_sent():
    transport = b.PublicTransport(delay=0)
    with pytest.raises(ValueError, match="endpoint_not_allowlisted"):
        transport("/fapi/v1/order", {"symbol": "ETHUSDT"})
    with pytest.raises(ValueError, match="symbol_not_allowlisted"):
        transport("/fapi/v1/fundingRate", {"symbol": "BTCUSDT"})


def test_backfill_needs_specific_absolute_destination(tmp_path):
    with pytest.raises(ValueError, match="absolute_backfill_subdirectory_required"):
        b.capture(tmp_path / "anything", transport=FixtureTransport(), now_ms=NOW)
    with pytest.raises(ValueError, match="absolute_backfill_subdirectory_required"):
        b.capture(Path("backfill"), transport=FixtureTransport(), now_ms=NOW)


def test_failed_endpoint_does_not_suppress_other_two(tmp_path):
    class PartialFailure(FixtureTransport):
        def __call__(self, endpoint, params):
            if endpoint.endswith("openInterestHist"):
                raise RuntimeError("provider_access_refused_http_451")
            return super().__call__(endpoint, params)
    report = b.capture(tmp_path / "backfill", transport=PartialFailure(), now_ms=NOW)
    assert not report["all_captured"]
    assert report["results"]["openInterestHist"]["status"] == "failed"
    assert report["results"]["globalLongShortAccountRatio"]["status"] == "captured"
    assert report["results"]["fundingRate"]["status"] == "captured"
    assert not any(p.name.startswith("openInterestHist-") for p in (tmp_path / "backfill").iterdir())


def test_provider_silent_funding_cap_does_not_count_as_full_history():
    class FakeCapped:
        def __call__(self, endpoint, params):
            assert endpoint.endswith("fundingRate")
            return [{"fundingTime": params["startTime"] + 1, "symbol": "ETHUSDT",
                     "fundingRate": "0.0001"} for _ in range(500)]

    with pytest.raises(RuntimeError, match="funding_window_still_truncated"):
        b.retrieve_funding(NOW, FakeCapped())


def test_small_funding_clock_jitter_does_not_create_a_gap():
    rows = [{"fundingTime": 1_000, "symbol": "ETHUSDT"},
            {"fundingTime": 1_000 + 8 * 60 * 60 * 1000 + 6_000, "symbol": "ETHUSDT"}]
    assert b.gaps("fundingRate", rows) == []


def test_funding_interval_change_not_treated_as_definite_missing():
    rows = [{"fundingTime": 1_000, "symbol": "ETHUSDT"},
            {"fundingTime": 1_000 + 12 * 60 * 60 * 1000, "symbol": "ETHUSDT"}]
    out = b.gaps("fundingRate", rows)
    assert len(out) == 1 and out[0]["interpretation"] == "suspected_only"


def test_existing_output_filename_never_overwritten(tmp_path):
    name = tmp_path / "existing.json"
    name.write_text("old", encoding="utf8")
    with pytest.raises(FileExistsError):
        b._atomic_json(name, {"new": True})
    assert name.read_text("utf8") == "old"
