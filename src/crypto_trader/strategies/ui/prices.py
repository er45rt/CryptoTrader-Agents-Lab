"""Prices for My Portfolio (owner decision D40): Binance PUBLIC klines only, read-only.

The only network call is ohlcv.fetch_public_klines: GET to the one allow-listed URL (KLINES_URL), no keys, no redirects,
no proxy. The query carries exactly symbol, interval, startTime, endTime and limit=1000 (fixed page size; owner decision
D41) - nothing about the owner or the ledger. Pair = <ASSET>USDT. A price is the close of the last CLOSED 1-minute candle and is shown with its source and
time. When there is no pair, no network or any error, the asset gets price None plus the reason - never a guessed
number. Daily closes for the value-over-time chart are cached in <workspace>/portfolio/prices/ and only the missing
tail is fetched.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..ohlcv import KLINES_URL, fetch_public_klines

__all__ = ["BinancePrices", "SOURCE"]

SOURCE = "Binance public klines"
LATEST_TTL_S = 60.0


def _reason(exc: BaseException) -> str:
    text = str(exc)
    if "400" in text or "Invalid symbol" in text:
        return "no Binance USDT pair"
    return f"price unavailable: {type(exc).__name__}"


class BinancePrices:
    def __init__(self, cache_dir: Path, *, fetch: Callable[..., Any] = fetch_public_klines,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.dir = Path(cache_dir)
        self._fetch, self._now = fetch, now
        self._latest: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    # --- latest ---------------------------------------------------------------------------------------
    def latest(self, assets: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for asset in assets:
            with self._lock:
                hit = self._latest.get(asset)
            if hit and time.monotonic() - hit[0] < LATEST_TTL_S:
                out[asset] = hit[1]
                continue
            now = self._now()
            try:
                bars = self._fetch(f"{asset}USDT", "1m", start=now - timedelta(minutes=5), now=now, timeout=10)
                if not bars:
                    raise LookupError("no closed candle")
                last = bars[-1]
                res = {"price": str(last.close), "source": SOURCE, "at": last.close_time.isoformat(),
                       "url": KLINES_URL}
            except Exception as exc:   # any failure is shown as "not available" with its reason
                res = {"price": None, "reason": _reason(exc), "source": SOURCE}
            with self._lock:
                self._latest[asset] = (time.monotonic(), res)
            out[asset] = res
        return out

    # --- daily closes for history ----------------------------------------------------------------------
    def daily(self, asset: str, since: date) -> dict[str, Any]:
        """{'closes': {'YYYY-MM-DD': 'close'}, 'reason': None|str} for closed daily candles from `since`."""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"daily_{asset}USDT.json"
        cached: dict[str, str] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        today = self._now().date()
        want_from = since
        if cached:
            last = date.fromisoformat(max(cached))
            first = date.fromisoformat(min(cached))
            want_from = since if since < first else last + timedelta(days=1)
        reason = None
        if want_from < today:   # today's candle is not closed yet
            start = datetime.combine(want_from, datetime.min.time(), tzinfo=timezone.utc)
            try:
                bars = self._fetch(f"{asset}USDT", "1d", start=start, now=self._now(), timeout=20)
                for b in bars:
                    cached[b.open_time.date().isoformat()] = str(b.close)
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(cached, sort_keys=True), encoding="utf-8")
                os.replace(tmp, path)
            except Exception as exc:
                reason = _reason(exc)
        closes = {d: c for d, c in cached.items() if d >= since.isoformat()}
        if not closes and reason is None:
            reason = "no daily candles"
        return {"closes": closes, "reason": reason}
