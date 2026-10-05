"""Historical Binance spot snapshotting through the project's existing public data tool.

Network access exists only in the explicit snapshot step. Backtests consume the saved
CSV files and perform no network calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

_INTERVAL_MS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
}


def _tool():
    # Lazy import keeps the offline evaluator independent from CrewAI at import time.
    from crypto_trader.tools.binance_public_tool import BinancePublicTool

    return BinancePublicTool()


def snapshot_binance_spot_klines(
    *,
    symbol: str,
    interval: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    output_path: Path,
) -> Path:
    """Fetch a bounded historical range via BinancePublicTool and persist an offline CSV."""

    if interval not in _INTERVAL_MS:
        raise ValueError(f"unsupported interval: {interval}")
    symbol = symbol.upper().strip()
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    else:
        start_ts = start_ts.tz_convert("UTC")
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    else:
        end_ts = end_ts.tz_convert("UTC")
    if end_ts <= start_ts:
        raise ValueError("end must be after start")

    start_ms = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000) - 1
    interval_ms = _INTERVAL_MS[interval]
    cursor = start_ms
    rows: list[list[object]] = []
    client = _tool()

    while cursor <= end_ms:
        raw = client._run(
            action="get_kline_data",
            parameters={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        payload = json.loads(raw)
        if payload.get("status") != "ok":
            raise RuntimeError(f"Binance data snapshot failed: {payload}")
        batch = payload.get("data") or []
        if not batch:
            break
        rows.extend(batch)
        last_open = int(batch[-1][0])
        next_cursor = last_open + interval_ms
        if next_cursor <= cursor:
            raise RuntimeError("Binance pagination did not advance")
        cursor = next_cursor
        if len(batch) < 1000:
            break

    if not rows:
        raise RuntimeError("Binance returned no candles for requested range")

    columns = [
        "open_time_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time_ms",
        "amount",
        "trade_count",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
        "ignore",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    frame["timestamp"] = pd.to_datetime(frame["open_time_ms"], unit="ms", utc=True)
    for name in ("open", "high", "low", "close", "volume", "amount"):
        frame[name] = pd.to_numeric(frame[name], errors="raise")

    frame = (
        frame.loc[:, ["timestamp", "open", "high", "low", "close", "volume", "amount"]]
        .drop_duplicates(subset=["timestamp"], keep="last")
        .sort_values("timestamp", kind="stable")
        .reset_index(drop=True)
    )

    expected = pd.Timedelta(milliseconds=interval_ms)
    gaps = frame["timestamp"].diff().dropna()
    if not gaps.empty and (gaps != expected).any():
        bad = frame.loc[gaps.index[gaps != expected][:5], "timestamp"].astype(str).tolist()
        raise RuntimeError(f"historical snapshot contains interval gaps near {bad}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False)
    return output_path


def load_offline_snapshot(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=["timestamp"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame
