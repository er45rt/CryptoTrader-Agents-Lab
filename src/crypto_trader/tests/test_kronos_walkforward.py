from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_trader.research.kronos import BacktestConfig, KronosSignal, evaluate_walk_forward, evaluate_walk_forward_batched


def candles(n: int = 80) -> pd.DataFrame:
    ts = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC")
    returns = np.where(np.arange(n) % 3 == 0, 0.002, -0.0005)
    close = 100.0 * np.cumprod(1.0 + returns)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": close * 0.9998,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": np.full(n, 100.0),
        }
    )


class SpyProvider:
    def __init__(self):
        self.input_ends = []

    def analyze(self, frame, *, horizon, horizon_steps=1, timeout_seconds=None):
        input_end = pd.to_datetime(frame["timestamp"].iloc[-1], utc=True)
        self.input_ends.append(input_end)
        first = float(frame["close"].iloc[0])
        last = float(frame["close"].iloc[-1])
        expected = (last / first - 1.0) / max(1, len(frame) - 1)
        direction = "long" if expected > 0 else "short" if expected < 0 else "flat"
        return KronosSignal(
            direction=direction,
            expected_return=expected,
            confidence=0.7,
            horizon=horizon,
            model_version="spy",
            input_end_timestamp=input_end.isoformat(),
        )


def test_walk_forward_never_passes_target_or_future_candles_to_provider():
    provider = SpyProvider()
    result = evaluate_walk_forward(
        candles(),
        provider,
        BacktestConfig(horizon_label="15m", context_size=12, stride=1),
    )
    rows = result["rows"]

    target_times = pd.to_datetime(rows["target_timestamp"], utc=True)
    input_times = pd.to_datetime(rows["input_end_timestamp"], utc=True)

    assert len(provider.input_ends) == len(rows)
    assert list(input_times) == provider.input_ends
    assert (input_times < target_times).all()


def test_future_mutation_cannot_change_already_formed_contexts():
    original = candles()
    mutated = original.copy()
    cutoff = 45
    mutated.loc[cutoff:, "close"] *= 50.0
    mutated.loc[cutoff:, "open"] *= 50.0
    mutated.loc[cutoff:, "high"] *= 50.0
    mutated.loc[cutoff:, "low"] *= 50.0

    first = SpyProvider()
    second = SpyProvider()
    cfg = BacktestConfig(horizon_label="15m", context_size=12, stride=1)
    a = evaluate_walk_forward(original, first, cfg)["rows"]
    b = evaluate_walk_forward(mutated, second, cfg)["rows"]

    # Any forecast whose input ended before the mutation point must be identical.
    mutation_ts = pd.to_datetime(original.loc[cutoff, "timestamp"], utc=True)
    mask = pd.to_datetime(a["input_end_timestamp"], utc=True) < mutation_ts
    assert np.allclose(
        a.loc[mask, "kronos_expected_return"].to_numpy(dtype=float),
        b.loc[mask, "kronos_expected_return"].to_numpy(dtype=float),
    )


def test_costs_are_applied_as_round_trip_entry_plus_exit():
    provider = SpyProvider()
    cfg = BacktestConfig(
        horizon_label="15m",
        context_size=12,
        stride=5,
        fee_per_side=0.001,
        slippage_per_side=0.0002,
    )
    result = evaluate_walk_forward(candles(), provider, cfg)

    assert result["config"]["round_trip_cost"] == pytest.approx(0.0024)
    assert "diebold_mariano_vs_baselines" in result
    assert result["coverage"]["total_points"] > 0


class BatchSpyProvider(SpyProvider):
    def analyze_batch(self, frames, *, horizon, horizon_steps=1, timeout_seconds=None):
        return [
            self.analyze(
                frame,
                horizon=horizon,
                horizon_steps=horizon_steps,
                timeout_seconds=timeout_seconds,
            )
            for frame in frames
        ]


def test_batched_walk_forward_preserves_no_lookahead_boundary():
    provider = BatchSpyProvider()
    result = evaluate_walk_forward_batched(
        candles(),
        provider,
        BacktestConfig(horizon_label="15m", context_size=12, stride=3),
        batch_size=4,
    )
    rows = result["rows"]
    input_times = pd.to_datetime(rows["input_end_timestamp"], utc=True)
    target_times = pd.to_datetime(rows["target_timestamp"], utc=True)

    assert list(input_times) == provider.input_ends
    assert (input_times < target_times).all()
