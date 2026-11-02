"""Offline proofs for the temporary Windows three-day derivatives statistics task."""
import json
from pathlib import Path

import pytest

from research import derivatives_backfill as b
from research import derivatives_incremental_stats as inc

NOW = 1_800_000_000_000 // b.STEP_MS * b.STEP_MS


class PublicFixture:
    def __init__(self, fail_name=None):
        self.calls = []
        self.fail_name = fail_name

    def __call__(self, path, params):
        self.calls.append((path, params))
        assert params["symbol"] == b.SYMBOL
        assert params["period"] == b.PERIOD == "5m"
        assert params["limit"] == 500
        assert path in (b.ENDPOINTS["openInterestHist"][0],
                        b.ENDPOINTS["globalLongShortAccountRatio"][0])
        name = "openInterestHist" if path.endswith("openInterestHist") else "globalLongShortAccountRatio"
        if name == self.fail_name:
            raise RuntimeError("fixture_network_failure")
        times = range(params["startTime"], params["endTime"] + 1, b.STEP_MS)
        if name == "openInterestHist":
            return [{"symbol": b.SYMBOL, "timestamp": ts,
                     "sumOpenInterest": "10"} for ts in times]
        return [{"symbol": b.SYMBOL, "timestamp": ts, "longAccount": "0.5",
                 "shortAccount": "0.5"} for ts in times]


def seed(root, name, latest):
    root.mkdir(parents=True, exist_ok=True)
    data = {"metadata": {"endpoint": b.ENDPOINTS[name][0], "symbol": b.SYMBOL,
                         "period": b.PERIOD},
            "records": [{"symbol": b.SYMBOL, "timestamp": latest}]}
    (root / (name + "-seed.json")).write_text(json.dumps(data), encoding="utf-8")


def test_three_day_increment_fetches_only_new_ticks_no_funding(tmp_path):
    root = tmp_path / "backfill"
    last = NOW - 3 * b.DAY_MS
    for name in inc.METRICS:
        seed(root, name, last)
    transport = PublicFixture()
    result = inc.incremental_capture(root, transport=transport, now_ms=NOW)
    assert result["all_captured_without_gaps"]
    assert len(transport.calls) == 4
    assert all(request["startTime"] >= last + b.STEP_MS
               for _, request in transport.calls)
    for name in inc.METRICS:
        data = result["results"][name]
        assert data["rows"] == 864
        assert data["gap_count"] == 0
        records = json.loads((root / data["file"]).read_text(encoding="utf-8"))["records"]
        assert records[0]["timestamp"] == last + b.STEP_MS
        assert records[-1]["timestamp"] == NOW
        assert not set(r["timestamp"] for r in records) & {last}
    assert not list(root.glob("*.darc"))


def test_repeated_run_adds_new_only_and_never_overwrites(tmp_path):
    root = tmp_path / "backfill"
    for name in inc.METRICS:
        seed(root, name, NOW - b.STEP_MS)
    first = inc.incremental_capture(root, transport=PublicFixture(), now_ms=NOW)
    assert first["all_captured_without_gaps"]
    original = {name: (root / first["results"][name]["file"]).read_bytes()
                for name in inc.METRICS}
    second = inc.incremental_capture(root, transport=PublicFixture(),
                                     now_ms=NOW + b.STEP_MS)
    assert second["all_captured_without_gaps"]
    for name in inc.METRICS:
        assert second["results"][name]["rows"] == 1
        assert second["results"][name]["previous_last"] == b.iso(NOW)
        assert (root / first["results"][name]["file"]).read_bytes() == original[name]
        all_times = []
        for file in root.glob(name + "-incremental-*.json"):
            all_times += [r["timestamp"] for r in json.loads(file.read_text())["records"]]
        assert all_times == [NOW, NOW + b.STEP_MS]


def test_retention_loss_is_explicit_and_prevents_success(tmp_path):
    root = tmp_path / "backfill"
    for name in inc.METRICS:
        seed(root, name, NOW - 31 * b.DAY_MS)
    class Empty:
        def __call__(self, endpoint, params):
            return []
    result = inc.incremental_capture(root, transport=Empty(), now_ms=NOW)
    assert not result["all_captured_without_gaps"]
    assert all(result["results"][name]["retention_loss_detected"] for name in inc.METRICS)
    assert all(result["results"][name]["status"] == "gapped" for name in inc.METRICS)


def test_existing_corruption_blocks_collection_before_network(tmp_path):
    root = tmp_path / "backfill"
    root.mkdir()
    (root / "openInterestHist-corrupt.json").write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="existing_archive_integrity_unverified"):
        inc.incremental_capture(root, transport=PublicFixture(), now_ms=NOW)


def test_missing_5m_tick_never_advances_high_water_mark(tmp_path):
    root = tmp_path / "backfill"
    for name in inc.METRICS:
        seed(root, name, NOW - 6 * b.STEP_MS)

    class DropOne(PublicFixture):
        def __call__(self, path, params):
            return [row for row in super().__call__(path, params)
                    if row["timestamp"] != NOW - 4 * b.STEP_MS]

    failed = inc.incremental_capture(root, transport=DropOne(), now_ms=NOW)
    assert not failed["all_captured_without_gaps"]
    for name in inc.METRICS:
        meta = failed["results"][name]
        assert meta["status"] == "gapped"
        assert meta["gap_count"] == 1
        assert meta["rows"] == 1
        assert inc.last_saved_timestamp(root, name) == NOW - 5 * b.STEP_MS

    repaired = inc.incremental_capture(root, transport=PublicFixture(),
                                       now_ms=NOW + b.STEP_MS)
    assert repaired["all_captured_without_gaps"]
    for name in inc.METRICS:
        assert repaired["results"][name]["rows"] == 6
        assert repaired["results"][name]["previous_last"] == b.iso(NOW - 5 * b.STEP_MS)


def test_second_instance_refused_by_windows_lock(tmp_path):
    root = tmp_path / "backfill"
    root.mkdir()
    with inc.exclusive_lock(root):
        with pytest.raises(RuntimeError, match="incremental_collection_already_running"):
            with inc.exclusive_lock(root):
                pass


def test_partial_endpoint_failure_can_recover_without_duplicate(tmp_path):
    root = tmp_path / "backfill"
    for name in inc.METRICS:
        seed(root, name, NOW - b.DAY_MS)
    first = inc.incremental_capture(root,
                                    transport=PublicFixture(fail_name="globalLongShortAccountRatio"),
                                    now_ms=NOW)
    assert not first["all_captured_without_gaps"]
    assert first["results"]["openInterestHist"]["status"] == "captured"
    second = inc.incremental_capture(root, transport=PublicFixture(),
                                     now_ms=NOW + b.STEP_MS)
    assert second["all_captured_without_gaps"]
    assert second["results"]["openInterestHist"]["rows"] == 1
    assert second["results"]["globalLongShortAccountRatio"]["rows"] == 289
