from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from crypto_trader.research.kronos import PinnedKronosBackend, PinnedKronosBackendConfig

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "artifacts" / "Kronos-source"
MODEL = ROOT / "artifacts" / "Kronos-small"
TOKENIZER = ROOT / "artifacts" / "Kronos-Tokenizer-base"


def make_frame() -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    n = 512
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    x = np.arange(n)
    close = 100_000.0 * (1.0 + 0.00001 * x + 0.0005 * np.sin(x / 12.0))
    frame = pd.DataFrame(
        {
            "open": close * 0.9999,
            "high": close * 1.0005,
            "low": close * 0.9995,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": close * 100.0,
        }
    )
    return frame, ts


def main() -> None:
    backend = PinnedKronosBackend(
        PinnedKronosBackendConfig(
            source_dir=SOURCE,
            model_dir=MODEL,
            tokenizer_dir=TOKENIZER,
            device="cpu",
        )
    )
    frame, ts = make_frame()
    predictor = backend._load()
    count = 20

    torch.manual_seed(20261005)
    t0 = time.perf_counter()
    fast = np.asarray(backend._sample_one_step(frame, ts, sample_count=count), dtype=float)
    fast_s = time.perf_counter() - t0

    future = pd.Series(pd.DatetimeIndex([ts[-1] + (ts[-1] - ts[-2])]))
    torch.manual_seed(20261005)
    t1 = time.perf_counter()
    forecasts = predictor.predict_batch(
        df_list=[frame.copy(deep=False) for _ in range(count)],
        x_timestamp_list=[pd.Series(ts) for _ in range(count)],
        y_timestamp_list=[future for _ in range(count)],
        pred_len=1,
        T=backend.config.temperature,
        top_k=backend.config.top_k,
        top_p=backend.config.top_p,
        sample_count=1,
        verbose=False,
    )
    slow_s = time.perf_counter() - t1
    last = float(frame["close"].iloc[-1])
    reference = np.asarray([float(x["close"].iloc[-1]) / last - 1.0 for x in forecasts])

    # Identical ordering is not guaranteed because the optimized path vectorizes
    # random draws differently. Compare distributional moments and sorted samples.
    result = {
        "sample_count": count,
        "fast_seconds": fast_s,
        "reference_seconds": slow_s,
        "speedup": slow_s / fast_s,
        "fast_mean": float(fast.mean()),
        "reference_mean": float(reference.mean()),
        "fast_std": float(fast.std(ddof=1)),
        "reference_std": float(reference.std(ddof=1)),
        "sorted_max_abs_diff": float(np.max(np.abs(np.sort(fast) - np.sort(reference)))),
        "fast_samples": fast.tolist(),
        "reference_samples": reference.tolist(),
    }
    (ROOT / "fast_path_validation.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in result.items() if not k.endswith("_samples")}, indent=2))


if __name__ == "__main__":
    main()
