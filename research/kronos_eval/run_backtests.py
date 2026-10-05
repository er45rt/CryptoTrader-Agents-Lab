from __future__ import annotations

import json
import math
import os
import platform
import random
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from crypto_trader.research.kronos import (
    BacktestConfig,
    KronosSignalProvider,
    PinnedKronosBackend,
    PinnedKronosBackendConfig,
    evaluate_walk_forward_batched,
    pin_manifest,
)
from crypto_trader.research.kronos.binance_history import load_offline_snapshot

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RESULTS = ROOT / "results"
SOURCE = Path(os.getenv("KRONOS_SOURCE_DIR", str(ROOT / "artifacts" / "Kronos-source")))
MODEL = ROOT / "artifacts" / "Kronos-small"
TOKENIZER = ROOT / "artifacts" / "Kronos-Tokenizer-base"

SYMBOLS = tuple(x for x in os.getenv("KRONOS_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if x)
INTERVALS = tuple(x for x in os.getenv("KRONOS_INTERVALS", "5m,15m,1h").split(",") if x)
DATA_SUFFIX = "2025-10-01_2026-10-01"
TARGET_OOS_START = "2025-10-01T00:00:00+00:00"
TARGET_OOS_END_EXCLUSIVE = "2026-10-01T00:00:00+00:00"
# 89 origins make the derived stride rotate through intraday clock phases on all three frames;
# 73 is intentionally avoided because a 365-day year / 73 = exactly 5 days and aliases midnight.
TARGET_EVALUATION_POINTS = int(os.getenv("KRONOS_TARGET_POINTS", "89"))
SEED = 20261005
CONTEXT_SIZE = int(os.getenv("KRONOS_CONTEXT_SIZE", "512"))
SAMPLE_COUNT = int(os.getenv("KRONOS_SAMPLE_COUNT", "20"))
FEE_PER_SIDE = 0.001
SLIPPAGE_PER_SIDE = 0.0002
BATCH_SIZE = int(os.getenv("KRONOS_BATCH_SIZE", "2"))


def _json_safe(value):
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    return value


def _summary_without_rows(result: dict[str, object]) -> dict[str, object]:
    return {key: _json_safe(value) for key, value in result.items() if key != "rows"}


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    # This i7-3630QM performs best with four intra-op threads and one inter-op
    # thread; benchmarked locally before the evaluation. This only affects the
    # offline research runner, never production runtime configuration.
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)

    backend = PinnedKronosBackend(
        PinnedKronosBackendConfig(
            source_dir=SOURCE,
            model_dir=MODEL,
            tokenizer_dir=TOKENIZER,
            device="cpu",
        )
    )
    provider = KronosSignalProvider(backend=backend, sample_count=SAMPLE_COUNT)

    suite: dict[str, object] = {
        "seed": SEED,
        "target_evaluation_points_per_series": TARGET_EVALUATION_POINTS,
        "context_size": CONTEXT_SIZE,
        "sample_count": SAMPLE_COUNT,
        "fee_per_side": FEE_PER_SIDE,
        "slippage_per_side": SLIPPAGE_PER_SIDE,
        "round_trip_cost": 2 * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE),
        "oos_target_start": TARGET_OOS_START,
        "oos_target_end_exclusive": TARGET_OOS_END_EXCLUSIVE,
        "batch_size": BATCH_SIZE,
        "pins": pin_manifest(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "torch": torch.__version__,
            "torch_cuda_available": bool(torch.cuda.is_available()),
            "device": "cpu",
        },
        "series": {},
    }

    for symbol in SYMBOLS:
        for interval in INTERVALS:
            key = f"{symbol}_{interval}"
            path = DATA / f"{key}_{DATA_SUFFIX}.csv"
            if not path.is_file():
                raise FileNotFoundError(f"missing offline snapshot: {path}")

            candles = load_offline_snapshot(path)
            usable = len(candles) - CONTEXT_SIZE
            if usable < 20:
                raise RuntimeError(f"{key} has too few candles after context warm-up")
            stride = max(1, math.ceil(usable / TARGET_EVALUATION_POINTS))

            cfg = BacktestConfig(
                horizon_label=interval,
                horizon_steps=1,
                context_size=CONTEXT_SIZE,
                stride=stride,
                momentum_lookback=12,
                fee_per_side=FEE_PER_SIDE,
                slippage_per_side=SLIPPAGE_PER_SIDE,
                inference_timeout_seconds=None,
            )
            print(
                f"START {key}: candles={len(candles)} stride={stride} "
                f"expected_points~{math.ceil(usable/stride)}",
                flush=True,
            )
            started = time.perf_counter()
            result = evaluate_walk_forward_batched(candles, provider, cfg, batch_size=BATCH_SIZE)
            elapsed = time.perf_counter() - started
            result["elapsed_seconds"] = elapsed
            result["data"] = {
                "path": str(path),
                "candles": int(len(candles)),
                "start": candles["timestamp"].iloc[0].isoformat(),
                "end": candles["timestamp"].iloc[-1].isoformat(),
            }
            rows = result["rows"]
            assert isinstance(rows, pd.DataFrame)
            result["data"]["first_oos_target"] = str(rows["target_timestamp"].iloc[0])
            result["data"]["last_oos_target"] = str(rows["target_timestamp"].iloc[-1])
            rows_path = RESULTS / f"{key}_walkforward_rows.csv"
            rows.to_csv(rows_path, index=False)

            summary = _summary_without_rows(result)
            suite["series"][key] = summary
            (RESULTS / f"{key}_summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (RESULTS / "suite_summary.partial.json").write_text(
                json.dumps(_json_safe(suite), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(
                f"DONE {key}: points={summary['coverage']['total_points']} "
                f"valid={summary['coverage']['kronos_valid_points']} "
                f"p50={summary['latency']['p50_seconds']:.3f}s "
                f"elapsed={elapsed:.1f}s",
                flush=True,
            )

    suite["completed"] = True
    (RESULTS / "suite_summary.json").write_text(
        json.dumps(_json_safe(suite), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
