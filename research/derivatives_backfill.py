"""One-shot ETHUSDT PUBLIC market-history capture, research only.

Historical rows do not prove publication time and are never causal/admission
evidence. No credentials, orders, account endpoints, collector .darc, or OMS.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, HTTPSHandler, build_opener

BASE = "https://fapi.binance.com"
SYMBOL = "ETHUSDT"
PERIOD = "5m"
STEP_MS = 300_000
DAY_MS = 86_400_000
WINDOW_MS = 30 * DAY_MS
MAX_BODY = 4_000_000
PACING_SECONDS = 1.25
FUNDING_MAX_PAGES = 80
STATS_MAX_PAGES = 25
ENDPOINTS = {
    "openInterestHist": ("/futures/data/openInterestHist", "timestamp"),
    "globalLongShortAccountRatio": ("/futures/data/globalLongShortAccountRatio", "timestamp"),
    "fundingRate": ("/fapi/v1/fundingRate", "fundingTime"),
}


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicTransport:
    """Strict official Binance public GET, no proxy, no redirect and no auth."""

    def __init__(self, *, delay=PACING_SECONDS):
        self.delay = delay
        self.last_call = 0.0
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect())

    def server_time_ms(self) -> int:
        """Anchor historical retention windows to Binance, not an unverified host clock."""
        req = Request(BASE + "/fapi/v1/time", headers={"Accept": "application/json", "User-Agent": "CryptoTrader-Research-Backfill/1"})
        with self.opener.open(req, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError("binance_clock_http_" + str(response.status))
            payload = json.loads(response.read(512))
        value = payload.get("serverTime") if isinstance(payload, dict) else None
        if type(value) is not int or not 1_500_000_000_000 <= value <= 253_402_300_799_999:
            raise RuntimeError("invalid_binance_server_clock")
        return value

    def __call__(self, path: str, params: dict) -> list[dict]:
        if path not in {name for name, _ in ENDPOINTS.values()}:
            raise ValueError("endpoint_not_allowlisted")
        if params.get("symbol") != SYMBOL:
            raise ValueError("symbol_not_allowlisted")
        url = BASE + path + "?" + urlencode(params)
        for attempt in range(3):
            elapsed = time.monotonic() - self.last_call
            if self.last_call and elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            self.last_call = time.monotonic()
            try:
                req = Request(url, headers={"Accept": "application/json", "User-Agent": "CryptoTrader-Research-Backfill/1"})
                with self.opener.open(req, timeout=10) as response:
                    if response.status != 200:
                        raise RuntimeError("unexpected_http_status_" + str(response.status))
                    body = response.read(MAX_BODY + 1)
                    if len(body) > MAX_BODY:
                        raise RuntimeError("response_exceeds_size_cap")
                payload = json.loads(body)
                if not isinstance(payload, list):
                    raise RuntimeError("unexpected_provider_shape")
                if any(not isinstance(row, dict) for row in payload):
                    raise RuntimeError("nonobject_provider_row")
                return payload
            except HTTPError as error:
                if error.code in (418, 401, 403, 451):
                    raise RuntimeError("provider_access_refused_http_" + str(error.code)) from error
                if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise RuntimeError("provider_http_" + str(error.code)) from error
                retry = error.headers.get("Retry-After", "")
                try:
                    delay = float(retry)
                except ValueError:
                    delay = 3 * (attempt + 1)
                if delay < 0 or delay > 60:
                    raise RuntimeError("provider_retry_after_requires_manual_review") from error
                time.sleep(max(2.0, delay))
            except (URLError, TimeoutError, OSError) as error:
                if attempt == 2:
                    raise RuntimeError("network_unavailable_" + type(error).__name__) from error
                time.sleep(2 * (attempt + 1))
        raise RuntimeError("unreachable_retry_exhaustion")


def validate_rows(name: str, rows: list[dict], start: int, end: int) -> list[dict]:
    field = ENDPOINTS[name][1]
    for item in rows:
        if item.get("symbol") != SYMBOL:
            raise RuntimeError("unexpected_symbol")
        timestamp = item.get(field)
        if type(timestamp) is not int or timestamp < start or timestamp > end:
            raise RuntimeError("provider_timestamp_outside_requested_window")
    return rows


def retrieve_stats(name: str, now_ms: int, transport) -> tuple[list[dict], dict]:
    start = ((now_ms - WINDOW_MS + STEP_MS - 1) // STEP_MS) * STEP_MS
    cursor = start
    records: dict[int, dict] = {}
    pages = 0
    while cursor <= now_ms:
        if pages >= STATS_MAX_PAGES:
            raise RuntimeError("statistics_page_cap")
        end = min(cursor + (500 * STEP_MS - 1), now_ms)
        params = {"symbol": SYMBOL, "period": PERIOD, "limit": 500, "startTime": cursor, "endTime": end}
        rows = validate_rows(name, transport(ENDPOINTS[name][0], params), cursor, end)
        for row in rows:
            ts = row["timestamp"]
            if ts in records and records[ts] != row:
                raise RuntimeError("conflicting_duplicate_timestamp")
            records[ts] = row
        pages += 1
        cursor = end + 1
    return [records[k] for k in sorted(records)], {"requests": pages, "window_start": iso(start), "window_end": iso(now_ms), "period": PERIOD}


def retrieve_funding(now_ms: int, transport) -> tuple[list[dict], dict]:
    # An explicit zero start asks Binance for the oldest available USD-M record.
    cursor = 0
    unique = {}
    pages = 0
    while cursor <= now_ms:
        if pages >= FUNDING_MAX_PAGES:
            raise RuntimeError("funding_page_cap")
        params = {"symbol": SYMBOL, "startTime": cursor, "endTime": now_ms, "limit": 1000}
        rows = validate_rows("fundingRate", transport(ENDPOINTS["fundingRate"][0], params), cursor, now_ms)
        pages += 1
        if not rows:
            break
        for row in rows:
            key = (row["fundingTime"], row.get("rateType"), row.get("fundingRate"), row.get("markPrice"))
            unique[key] = row
        last = max(row["fundingTime"] for row in rows)
        if last < cursor:
            raise RuntimeError("nonprogressing_funding_history")
        cursor = last + 1
        if len(rows) < 1000:
            break
    ordered = [unique[k] for k in sorted(unique)]
    return ordered, {"requests": pages, "history_requested_from": "earliest_available", "cursor_finished": iso(min(cursor, now_ms))}


def gaps(name: str, rows: list[dict]) -> list[dict]:
    if len(rows) < 2:
        return []
    field = ENDPOINTS[name][1]
    times = sorted({row[field] for row in rows})
    expected = STEP_MS if name != "fundingRate" else 8 * 60 * 60 * 1000
    output = []
    for a, b in zip(times, times[1:]):
        if b - a > expected:
            output.append({"after": iso(a), "before": iso(b),
                           "duration_minutes": (b - a) / 60000,
                           "missing_periods_estimate": (b - a) // expected - 1,
                           "interpretation": "suspected_only" if name == "fundingRate" else "missing_5m_intervals"})
    return output


def record_report(name: str, rows: list[dict], detail: dict, now_ms: int) -> dict:
    field = ENDPOINTS[name][1]
    missing = gaps(name, rows)
    days = sorted({iso(row[field])[:10] for row in rows})
    return {"endpoint": ENDPOINTS[name][0], "symbol": SYMBOL,
            "status": "captured", "rows": len(rows), "days_with_records": len(days),
            "first": iso(rows[0][field]) if rows else None,
            "last": iso(rows[-1][field]) if rows else None,
            "gap_count": len(missing), "gaps": missing,
            "note": "historical_api_availability_unknown; not causal_replay_evidence",
            "captured_at": iso(now_ms), **detail}


def _atomic_json(path: Path, value):
    # Exclusive temp avoids modifying existing captures.
    raw = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    tmp = path.with_name(path.name + ".pending")
    with tmp.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if path.exists():
        raise FileExistsError("capture_name_already_exists")
    tmp.rename(path)
    return hashlib.sha256(raw).hexdigest()


def capture(destination: Path, *, transport=None, now_ms: int | None = None) -> dict:
    if not destination.is_absolute() or destination.name.lower() != "backfill":
        raise ValueError("absolute_backfill_subdirectory_required")
    if destination.exists() and destination.is_symlink():
        raise ValueError("symlink_destination_forbidden")
    destination.mkdir(parents=True, exist_ok=True)
    transport = PublicTransport() if transport is None else transport
    clock_source = "test_supplied" if now_ms is not None else "binance_server_time"
    if now_ms is None:
        if not isinstance(transport, PublicTransport):
            raise ValueError("live_clock_requires_public_transport")
        now_ms = transport.server_time_ms()
    host_offset_ms = time.time_ns() // 1_000_000 - now_ms
    if type(now_ms) is not int or now_ms <= 0:
        raise ValueError("invalid_capture_time")
    manifest = {"schema": "deriv-backfill-v1", "symbol": SYMBOL, "as_of": iso(now_ms),
                "clock_source": clock_source, "host_minus_capture_clock_ms": host_offset_ms,
                "scope": "research_only_historical_uncertified", "results": {},
                "no_keys": True, "no_orders": True}
    for name in ENDPOINTS:
        try:
            rows, detail = (retrieve_funding(now_ms, transport) if name == "fundingRate"
                            else retrieve_stats(name, now_ms, transport))
            report = record_report(name, rows, detail, now_ms)
            filename = name + "-" + str(now_ms) + ".json"
            payload = {"metadata": report, "records": rows}
            report["file_sha256"] = _atomic_json(destination / filename, payload)
            report["file"] = filename
            manifest["results"][name] = report
        except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as error:
            manifest["results"][name] = {"status": "failed", "error": type(error).__name__ + ":" + str(error)}
        print(json.dumps({"endpoint": name, **{k: v for k, v in manifest["results"][name].items() if k in ("status", "rows", "first", "last", "gap_count", "error")}}, ensure_ascii=False), flush=True)
    manifest["all_captured"] = all(v.get("status") == "captured" for v in manifest["results"].values())
    report_name = "backfill-report-" + str(now_ms) + ".json"
    _atomic_json(destination / report_name, manifest)
    print("REPORT=" + str(destination / report_name), flush=True)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-only-ack", action="store_true")
    args = parser.parse_args(argv)
    if not args.public_only_ack:
        parser.error("public_only_ack_required")
    return 0 if capture(args.output)["all_captured"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
