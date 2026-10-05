from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from crypto_trader.research.kronos import KronosSignalProvider, PinnedKronosBackend, PinnedKronosBackendConfig

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "artifacts" / "Kronos-source"
MODEL = ROOT / "artifacts" / "Kronos-small"
TOKENIZER = ROOT / "artifacts" / "Kronos-Tokenizer-base"


def frame() -> pd.DataFrame:
    n = 512
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    x = np.arange(n)
    close = 100_000.0 * (1.0 + 0.00001 * x + 0.0005 * np.sin(x / 12.0))
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": close * 0.9999,
            "high": close * 1.0005,
            "low": close * 0.9995,
            "close": close,
            "volume": np.full(n, 100.0),
            "amount": close * 100.0,
        }
    )


def main() -> None:
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    backend = PinnedKronosBackend(
        PinnedKronosBackendConfig(
            source_dir=SOURCE,
            model_dir=MODEL,
            tokenizer_dir=TOKENIZER,
            device="cpu",
        )
    )
    provider = KronosSignalProvider(backend=backend, sample_count=20)
    # First call includes model load; second is steady-state inference.
    t0 = time.perf_counter()
    first = provider.analyze(frame(), horizon="5m")
    load_plus_inference = time.perf_counter() - t0

    t1 = time.perf_counter()
    second = provider.analyze(frame(), horizon="5m")
    steady = time.perf_counter() - t1

    result = {
        "load_plus_inference_seconds": load_plus_inference,
        "steady_state_inference_seconds": steady,
        "direction": second.direction,
        "expected_return": second.expected_return,
        "confidence": second.confidence,
        "error": provider.last_error,
    }
    (ROOT / "benchmark.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
