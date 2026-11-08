"""OHLCV data for strategies: local files (CSV / Freqtrade feather) and Binance PUBLIC klines.

Integrity rules enforced here (fail closed):
  * bars are strictly increasing, on the timeframe grid, with consistent OHLC (see Bar);
  * the last bar returned is always CLOSED at `now` (an open candle is never handed to a strategy);
  * an optional hard `not_after` cut-off: no bar opening at/after it is ever kept.
Public reads are GET-only to one allow-listed URL, no credentials, no redirects.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

from .api import TIMEFRAMES, Bar, StrategyError

__all__ = ["load_bars", "bars_from_rows", "fetch_public_klines", "check_bars", "KLINES_URL"]

KLINES_URL = "https://api.binance.com/api/v3/klines"


def _dt(value: object) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value) / (1000 if value > 1e11 else 1), tz=timezone.utc)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if hasattr(dt, "to_pydatetime"):
        dt = dt.to_pydatetime()
    if dt.tzinfo is None:
        raise StrategyError(f"timestamp {value!r} has no timezone; use UTC (e.g. ...+00:00).")
    return dt.astimezone(timezone.utc)


def bars_from_rows(rows: Iterable[dict], timeframe: str) -> list[Bar]:
    out = []
    for row in rows:
        out.append(Bar(
            open_time=_dt(row["date"]),
            open=Decimal(str(row["open"])), high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])), close=Decimal(str(row["close"])),
            volume=Decimal(str(row["volume"])), timeframe=timeframe,
        ))
    return out


def check_bars(bars: Sequence[Bar], *, allow_gaps: bool = False) -> None:
    if not bars:
        raise StrategyError("no bars.")
    step = TIMEFRAMES[bars[0].timeframe]
    for prev, cur in zip(bars, bars[1:]):
        if cur.timeframe != prev.timeframe:
            raise StrategyError("mixed timeframes in one series.")
        delta = cur.open_time - prev.open_time
        if delta <= timedelta(0):
            raise StrategyError(f"bars not strictly increasing at {cur.open_time.isoformat()}.")
        if delta % step:
            raise StrategyError(f"bar off the {cur.timeframe} grid at {cur.open_time.isoformat()}.")
        if delta != step and not allow_gaps:
            raise StrategyError(f"gap of {delta} before {cur.open_time.isoformat()} (pass allow_gaps=True to accept).")


def _cut(bars: list[Bar], start: datetime | None, end: datetime | None, not_after: datetime | None,
         now: datetime | None) -> list[Bar]:
    keep = []
    for b in bars:
        if start is not None and b.open_time < start:
            continue
        if end is not None and b.open_time >= end:
            continue
        if not_after is not None and b.open_time >= not_after:
            continue
        if now is not None and b.close_time > now:
            continue
        keep.append(b)
    return keep


_OHLCV = ["date", "open", "high", "low", "close", "volume"]


def _feather_rows_before(path: Path, not_after: datetime | None) -> list[dict]:
    """Feather (Arrow IPC) rows before `not_after`, record batch by record batch.

    For each batch only the date column is converted to find the cut; OHLCV values are converted for the rows
    before it only, and no batch after the one holding the first cut-off row is ever loaded."""
    import pyarrow as pa
    import pyarrow.ipc as ipc

    rows: list[dict] = []
    with pa.memory_map(str(path), "r") as source:
        reader = ipc.open_file(source)
        for k in range(reader.num_record_batches):
            batch = reader.get_batch(k)
            n = batch.num_rows
            if not_after is not None:
                dates = batch.column(batch.schema.get_field_index("date")).to_pylist()
                n = next((j for j, d in enumerate(dates) if _dt(d) >= not_after), n)
            rows.extend(batch.slice(0, n).select(_OHLCV).to_pylist())
            if n < batch.num_rows:
                break
    return rows


def load_bars(path: str | Path, timeframe: str, *, start: datetime | None = None,
              end: datetime | None = None, not_after: datetime | None = None,
              allow_gaps: bool = False) -> list[Bar]:
    """Load CSV (columns date,open,high,low,close,volume) or a Freqtrade .feather file."""
    path = Path(path)
    if timeframe not in TIMEFRAMES:
        raise StrategyError(f"unsupported timeframe {timeframe!r}.")
    # Reading stops at the first row at/after `not_after` (files are chronological; check_bars enforces it):
    # only that row's date is decoded, its prices/volume and every later row are never read or converted.
    if path.suffix == ".feather":
        rows = _feather_rows_before(path, not_after)
    elif path.suffix == ".csv":
        rows = []
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if not_after is not None and _dt(row["date"]) >= not_after:
                    break
                rows.append(row)
    else:
        raise StrategyError("data file must be .csv or .feather.")
    bars = _cut(bars_from_rows(rows, timeframe), start, end, not_after, None)
    check_bars(bars, allow_gaps=allow_gaps)
    return bars


def _tls_context(cafile: str | None = None):
    r"""ssl.create_default_context(cafile=...) for a client, WITHOUT the SSLKEYLOGFILE key log: the same certificate
    verification and hostname check, never a write to whatever path that environment variable names. A long-running
    paper session inherited SSLKEYLOGFILE='\\.\aswMonFltProxy\<id>'; once that path refused writes, every TLS setup
    (ssl.py: context.keylog_filename = ...) failed until the process restarted (paper session 2026-11-08 traceback)."""
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)          # check_hostname=True, verify_mode=CERT_REQUIRED
    if cafile:
        ctx.load_verify_locations(cafile=cafile)
    else:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    return ctx


def fetch_forming_open(symbol: str, timeframe: str, *, now: datetime | None = None,
                       cafile: str | None = None, timeout: float = 20.0) -> tuple[datetime, Decimal] | None:
    """(open_time, open) of the candle forming at `now` - its open price is final once it starts."""
    import ssl
    import urllib.parse
    import urllib.request

    now = now or datetime.now(timezone.utc)
    step = TIMEFRAMES[timeframe]
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    start = epoch + ((now - epoch) // step) * step
    ctx = _tls_context(cafile)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    query = urllib.parse.urlencode({"symbol": symbol, "interval": timeframe,
                                    "startTime": int(start.timestamp() * 1000), "limit": 1})
    req = urllib.request.Request(f"{KLINES_URL}?{query}", method="GET", headers={"User-Agent": "ct-strategy-ohlcv/1"})
    with opener.open(req, timeout=timeout) as resp:
        page = json.loads(resp.read())
    if not page or int(page[0][0]) != int(start.timestamp() * 1000):
        return None
    return start, Decimal(str(page[0][1]))


def fetch_public_klines(symbol: str, timeframe: str, *, start: datetime, end: datetime | None = None,
                        now: datetime | None = None, cafile: str | None = None,
                        not_after: datetime | None = None, timeout: float = 20.0) -> list[Bar]:
    """Closed klines from Binance's public endpoint (GET only, no keys). Paginates by 1000."""
    import ssl
    import urllib.parse
    import urllib.request

    if timeframe not in TIMEFRAMES:
        raise StrategyError(f"unsupported timeframe {timeframe!r}.")
    now = now or datetime.now(timezone.utc)
    ctx = _tls_context(cafile)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: ANN001
            raise StrategyError("redirect refused for public klines.")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect,
                                         urllib.request.HTTPSHandler(context=ctx))
    rows: list[dict] = []
    cursor = int(start.timestamp() * 1000)
    stop = int((end or now).timestamp() * 1000)
    while cursor < stop:
        query = urllib.parse.urlencode({"symbol": symbol, "interval": timeframe, "startTime": cursor,
                                        "endTime": stop - 1, "limit": 1000})
        req = urllib.request.Request(f"{KLINES_URL}?{query}", method="GET",
                                     headers={"User-Agent": "ct-strategy-ohlcv/1"})
        with opener.open(req, timeout=timeout) as resp:
            page = json.loads(resp.read())
        if not page:
            break
        for k in page:
            rows.append({"date": int(k[0]), "open": k[1], "high": k[2], "low": k[3], "close": k[4],
                         "volume": k[5]})
        cursor = int(page[-1][0]) + int(TIMEFRAMES[timeframe].total_seconds() * 1000)
        if len(page) < 1000:
            break
    bars = _cut(bars_from_rows(rows, timeframe), start, end, not_after, now)
    if bars:
        check_bars(bars, allow_gaps=True)
    return bars
