from __future__ import annotations

from pathlib import Path

import pandas as pd

from crypto_trader.research.kronos.binance_history import snapshot_binance_spot_klines

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TARGET_START = pd.Timestamp("2025-10-01T00:00:00Z")
TARGET_END = pd.Timestamp("2026-10-01T00:00:00Z")
CONTEXT_SIZE = 512
INTERVALS = {
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "1h": pd.Timedelta(hours=1),
}


def main() -> None:
    for symbol in ("BTCUSDT", "ETHUSDT"):
        for interval, delta in INTERVALS.items():
            # Pull exactly 512 candles before the one-year OOS target period so the
            # first evaluated target can be 2025-10-01 without sacrificing target span.
            snapshot_start = TARGET_START - CONTEXT_SIZE * delta
            output = DATA / f"{symbol}_{interval}_2025-10-01_2026-10-01.csv"
            print(
                f"snapshot {symbol} {interval}: warmup={snapshot_start.isoformat()} "
                f"target=[{TARGET_START.isoformat()}, {TARGET_END.isoformat()}) -> {output}",
                flush=True,
            )
            snapshot_binance_spot_klines(
                symbol=symbol,
                interval=interval,
                start=snapshot_start,
                end=TARGET_END,
                output_path=output,
            )


if __name__ == "__main__":
    main()
