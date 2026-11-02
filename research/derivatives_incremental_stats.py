"""Append-only, public ETHUSDT OI/Long-Short catch-up for a three-day Windows task.

Historical API timestamps do not prove publication time; research-only, no
causal-trading authority, credentials, private endpoints, orders, or .darc writes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import msvcrt
from pathlib import Path

if __package__:
    from . import derivatives_backfill as b
else:
    import derivatives_backfill as b

METRICS = ("openInterestHist", "globalLongShortAccountRatio")
MAX_PREVIOUS_BYTES = 8_000_000


@contextmanager
def exclusive_lock(root: Path):
    # Windows byte-range lock is automatically released if the process crashes.
    # The small lock file is reusable and is not a market-data archive.
    with (root / ".stats-incremental.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("incremental_collection_already_running") from error
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def last_saved_timestamp(root: Path, name: str) -> int | None:
    latest = None
    for path in sorted(root.glob(name + "-*.json")):
        if path.is_symlink() or path.stat().st_size > MAX_PREVIOUS_BYTES:
            raise RuntimeError("invalid_existing_archive_file")
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            info = obj["metadata"]
            rows = obj["records"]
            if (info["symbol"] != b.SYMBOL
                    or info["endpoint"] != b.ENDPOINTS[name][0]
                    or info["period"] != b.PERIOD
                    or type(rows) is not list):
                raise ValueError("wrong_archive_identity")
            stamps = []
            for row in rows:
                if (type(row) is not dict or row.get("symbol") != b.SYMBOL
                        or type(row.get("timestamp")) is not int):
                    raise ValueError("malformed_archived_row")
                stamps.append(row["timestamp"])
            if stamps != sorted(set(stamps)):
                raise ValueError("noncanonical_archive_timestamps")
            if stamps:
                latest = max(latest or 0, stamps[-1])
        except (KeyError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("existing_archive_integrity_unverified") from error
    return latest


def fetch_new(name: str, clock_ms: int, last: int | None, transport):
    edge = ((clock_ms - b.WINDOW_MS + b.RETENTION_MARGIN_MS
             + b.STEP_MS - 1) // b.STEP_MS) * b.STEP_MS
    first = max(edge, (last + b.STEP_MS) if last is not None else edge)
    first = ((first + b.STEP_MS - 1) // b.STEP_MS) * b.STEP_MS
    rows_by_time = {}
    pages = 0
    cursor = first
    while cursor <= clock_ms:
        if pages >= b.STATS_MAX_PAGES:
            raise RuntimeError("statistics_page_cap")
        end = min(cursor + 500 * b.STEP_MS - 1, clock_ms)
        params = {"symbol": b.SYMBOL, "period": b.PERIOD, "limit": 500,
                  "startTime": cursor, "endTime": end}
        received = b.validate_rows(name, transport(b.ENDPOINTS[name][0], params), cursor, end)
        pages += 1
        for row in received:
            ts = row["timestamp"]
            if ts in rows_by_time and rows_by_time[ts] != row:
                raise RuntimeError("conflicting_duplicate_timestamp")
            if last is None or ts > last:
                rows_by_time[ts] = row
        cursor = end + 1
    # Persist only the consecutive prefix. An API hole cannot advance the
    # high-water mark past missing ticks; the next run retries that hole.
    contiguous = []
    expected = first
    gaps = []
    for ts in sorted(rows_by_time):
        if ts != expected:
            gaps.append({"after": b.iso(expected - b.STEP_MS), "before": b.iso(ts),
                         "missing_periods_estimate": (ts - expected) // b.STEP_MS,
                         "interpretation": "uncommitted_missing_5m_intervals"})
            break
        contiguous.append(rows_by_time[ts])
        expected += b.STEP_MS
    retention_lost = last is not None and last + b.STEP_MS < edge
    recent = contiguous[-1]["timestamp"] if contiguous else last
    tail_stale = recent is None or clock_ms - recent > 15 * 60_000
    return contiguous, {"requests": pages, "previous_last": b.iso(last),
                        "window_start": b.iso(first), "retention_margin_minutes": 60,
                        "retention_loss_detected": retention_lost,
                        "tail_stale": tail_stale, "gap_count": len(gaps), "gaps": gaps}


def incremental_capture(root: Path, *, transport=None, now_ms=None):
    if not root.is_absolute() or root.name.casefold() != "backfill":
        raise ValueError("absolute_backfill_directory_required")
    if root.exists() and root.is_symlink():
        raise ValueError("symlink_archive_forbidden")
    root.mkdir(parents=True, exist_ok=True)
    transport = b.PublicTransport() if transport is None else transport
    supplied_clock = now_ms is not None
    if now_ms is None:
        if not isinstance(transport, b.PublicTransport):
            raise ValueError("live_clock_requires_public_transport")
        now_ms = transport.server_time_ms()
    if type(now_ms) is not int or now_ms <= 0:
        raise ValueError("invalid_provider_time")

    with exclusive_lock(root):
        report = {"schema": "deriv-stats-incremental-v1", "symbol": b.SYMBOL,
                  "clock_source": "test_supplied" if supplied_clock else "binance_server_time",
                  "captured_at": b.iso(now_ms), "no_keys": True, "no_orders": True,
                  "research_only": True, "results": {}}
        healthy = True
        for name in METRICS:
            last = last_saved_timestamp(root, name)
            try:
                rows, detail = fetch_new(name, now_ms, last, transport)
                result = {"endpoint": b.ENDPOINTS[name][0], "symbol": b.SYMBOL,
                          "period": b.PERIOD, "rows": len(rows), **detail}
                if rows:
                    filename = name + "-incremental-" + str(now_ms) + ".json"
                    payload = {"metadata": {**result, "status": "captured"}, "records": rows}
                    result["file_sha256"] = b._atomic_json(root / filename, payload)
                    result["file"] = filename
                result["status"] = ("gapped" if detail["gap_count"] or
                                    detail["retention_loss_detected"] or
                                    detail["tail_stale"] else "captured")
                healthy &= result["status"] == "captured"
            except (RuntimeError, OSError, ValueError, TypeError) as error:
                result = {"status": "failed", "error": type(error).__name__ + ":" + str(error)}
                healthy = False
            report["results"][name] = result
            print(json.dumps({"endpoint": name, "status": result["status"],
                              "new_rows": result.get("rows"),
                              "gaps": result.get("gap_count")}), flush=True)
        report["all_captured_without_gaps"] = healthy
        b._atomic_json(root / ("stats-incremental-report-" + str(now_ms) + ".json"), report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-only-ack", action="store_true")
    args = parser.parse_args(argv)
    if not args.public_only_ack:
        parser.error("public_only_ack_required")
    return 0 if incremental_capture(args.output)["all_captured_without_gaps"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
