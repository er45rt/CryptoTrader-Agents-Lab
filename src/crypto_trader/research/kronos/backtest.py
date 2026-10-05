"""Strict walk-forward evaluator for the offline Kronos research gate."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from statistics import NormalDist
from typing import Iterable

import numpy as np
import pandas as pd

from .contracts import SignalProvider

_METHODS = ("kronos", "naive_last_price", "random_walk_drift", "momentum")


@dataclass(frozen=True)
class BacktestConfig:
    horizon_label: str
    horizon_steps: int = 1
    context_size: int = 512
    stride: int = 1
    momentum_lookback: int = 12
    fee_per_side: float = 0.001
    slippage_per_side: float = 0.0002
    inference_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.horizon_steps < 1:
            raise ValueError("horizon_steps must be positive")
        if self.context_size < 2:
            raise ValueError("context_size must be >= 2")
        if self.stride < 1:
            raise ValueError("stride must be positive")
        if self.momentum_lookback < 1:
            raise ValueError("momentum_lookback must be positive")
        if self.fee_per_side < 0 or self.slippage_per_side < 0:
            raise ValueError("cost rates must be non-negative")

    @property
    def round_trip_cost(self) -> float:
        return 2.0 * (self.fee_per_side + self.slippage_per_side)


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _baseline_returns(context: pd.DataFrame, horizon_steps: int, momentum_lookback: int) -> dict[str, float]:
    closes = context["close"].to_numpy(dtype=float)
    if closes.size < 2:
        raise ValueError("baseline context requires at least two closes")

    simple_returns = closes[1:] / closes[:-1] - 1.0
    drift = float(np.mean(simple_returns[-min(len(simple_returns), 512):]))
    rw = drift * horizon_steps

    lookback = min(momentum_lookback, closes.size - 1)
    raw_momentum = closes[-1] / closes[-1 - lookback] - 1.0
    momentum = float(raw_momentum / lookback * horizon_steps)

    return {
        "naive_last_price": 0.0,
        "random_walk_drift": rw,
        "momentum": momentum,
    }


def _strategy_net(expected_return: float, actual_return: float, round_trip_cost: float) -> tuple[int, float]:
    # Do not open a one-horizon trade unless the forecast edge exceeds the estimated
    # full entry+exit friction. This rule is applied identically to every model.
    position = _sign(expected_return) if abs(expected_return) > round_trip_cost else 0
    net = position * actual_return - (round_trip_cost if position else 0.0)
    return position, float(net)


def _confidence_calibration(rows: pd.DataFrame) -> list[dict[str, float | int]]:
    if rows.empty:
        return []
    valid = rows.loc[rows["kronos_valid"]].copy()
    if valid.empty:
        return []

    edges = np.linspace(0.0, 1.0, 6)
    result: list[dict[str, float | int]] = []
    for idx in range(len(edges) - 1):
        left = float(edges[idx])
        right = float(edges[idx + 1])
        if idx == len(edges) - 2:
            mask = (valid["kronos_confidence"] >= left) & (valid["kronos_confidence"] <= right)
        else:
            mask = (valid["kronos_confidence"] >= left) & (valid["kronos_confidence"] < right)
        bucket = valid.loc[mask]
        if bucket.empty:
            continue
        accuracy = float(
            np.mean(
                np.sign(bucket["kronos_expected_return"].to_numpy(dtype=float))
                == np.sign(bucket["actual_return"].to_numpy(dtype=float))
            )
        )
        result.append(
            {
                "confidence_min": left,
                "confidence_max": right,
                "count": int(len(bucket)),
                "mean_confidence": float(bucket["kronos_confidence"].mean()),
                "direction_accuracy": accuracy,
            }
        )
    return result


def diebold_mariano_absolute_error(
    actual: Iterable[float],
    forecast_a: Iterable[float],
    forecast_b: Iterable[float],
) -> dict[str, float | int | None]:
    """One-sided DM-style test: positive statistic means A has lower absolute error than B.

    A is the candidate (Kronos); B is one baseline. For one-step, non-overlapping
    forecast errors the lag-0 variance is appropriate. The p-value is asymptotic.
    """

    y = np.asarray(list(actual), dtype=float)
    a = np.asarray(list(forecast_a), dtype=float)
    b = np.asarray(list(forecast_b), dtype=float)
    if not (y.shape == a.shape == b.shape) or y.size < 20:
        return {"n": int(y.size), "statistic": None, "p_value_one_sided": None}

    # Positive differential => baseline incurs more absolute error => candidate wins.
    differential = np.abs(y - b) - np.abs(y - a)
    mean_d = float(np.mean(differential))
    variance = float(np.var(differential, ddof=1))
    if not math.isfinite(variance) or variance <= 0:
        return {"n": int(y.size), "statistic": None, "p_value_one_sided": None}

    stat = mean_d / math.sqrt(variance / y.size)
    p_value = 1.0 - NormalDist().cdf(stat)
    return {
        "n": int(y.size),
        "statistic": float(stat),
        "p_value_one_sided": float(p_value),
    }


def paired_bootstrap_net_advantage(
    candidate_net: Iterable[float],
    baseline_net: Iterable[float],
    *,
    seed: int,
    resamples: int = 5000,
) -> dict[str, float | int | None]:
    """Paired percentile-bootstrap CI for candidate minus baseline net return."""

    candidate = np.asarray(list(candidate_net), dtype=float)
    baseline = np.asarray(list(baseline_net), dtype=float)
    if candidate.shape != baseline.shape or candidate.size < 20:
        return {
            "n": int(candidate.size),
            "mean_advantage": None,
            "ci95_low": None,
            "ci95_high": None,
            "resamples": int(resamples),
        }
    differences = candidate - baseline
    rng = np.random.default_rng(seed)
    n = differences.size
    boot = np.empty(resamples, dtype=float)
    for i in range(resamples):
        boot[i] = float(np.mean(differences[rng.integers(0, n, size=n)]))
    low, high = np.percentile(boot, [2.5, 97.5])
    return {
        "n": int(n),
        "mean_advantage": float(np.mean(differences)),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "resamples": int(resamples),
    }


def _metric_summary(rows: pd.DataFrame, prefix: str) -> dict[str, float | int | None]:
    valid_col = f"{prefix}_valid"
    valid = rows.loc[rows[valid_col]].copy()
    if valid.empty:
        return {
            "n": 0,
            "direction_accuracy": None,
            "mae_return": None,
            "strategy_net_return_sum": None,
            "strategy_mean_net_return": None,
            "trades": 0,
        }

    forecast = valid[f"{prefix}_expected_return"].to_numpy(dtype=float)
    actual = valid["actual_return"].to_numpy(dtype=float)
    direction_accuracy = float(np.mean(np.sign(forecast) == np.sign(actual)))
    mae = float(np.mean(np.abs(forecast - actual)))
    return {
        "n": int(len(valid)),
        "direction_accuracy": direction_accuracy,
        "mae_return": mae,
        "strategy_net_return_sum": float(valid[f"{prefix}_net_return"].sum()),
        "strategy_mean_net_return": float(valid[f"{prefix}_net_return"].mean()),
        "trades": int(np.count_nonzero(valid[f"{prefix}_position"].to_numpy(dtype=int))),
    }


def evaluate_walk_forward(
    candles: pd.DataFrame,
    provider: SignalProvider,
    config: BacktestConfig,
) -> dict[str, object]:
    """Evaluate strictly out of sample; target candle is never passed to provider/baselines."""

    if not isinstance(candles, pd.DataFrame) or candles.empty:
        raise ValueError("candles must be a non-empty DataFrame")
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = sorted(required.difference(candles.columns))
    if missing:
        raise ValueError(f"missing columns: {missing}")

    frame = candles.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
    if frame["timestamp"].duplicated().any():
        raise ValueError("duplicate timestamps are not allowed")

    first_target = config.context_size
    last_target_exclusive = len(frame) - config.horizon_steps + 1
    if first_target >= last_target_exclusive:
        raise ValueError("not enough candles for the requested walk-forward window")

    rows: list[dict[str, object]] = []
    for target_idx in range(first_target, last_target_exclusive, config.stride):
        input_end_idx = target_idx - 1
        target_end_idx = target_idx + config.horizon_steps - 1
        context = frame.iloc[target_idx - config.context_size : target_idx].copy()

        input_close = float(frame.at[input_end_idx, "close"])
        target_close = float(frame.at[target_end_idx, "close"])
        actual_return = target_close / input_close - 1.0
        input_end_ts = frame.at[input_end_idx, "timestamp"]
        target_ts = frame.at[target_end_idx, "timestamp"]

        started = time.perf_counter()
        signal = provider.analyze(
            context,
            horizon=config.horizon_label,
            horizon_steps=config.horizon_steps,
            timeout_seconds=config.inference_timeout_seconds,
        )
        latency = time.perf_counter() - started

        row: dict[str, object] = {
            "input_end_timestamp": input_end_ts.isoformat(),
            "target_timestamp": target_ts.isoformat(),
            "actual_return": float(actual_return),
            "kronos_latency_seconds": float(latency),
            "kronos_confidence": float(signal.confidence),
            "kronos_valid": signal.direction != "no_signal" and signal.expected_return is not None,
            "kronos_expected_return": float(signal.expected_return) if signal.expected_return is not None else np.nan,
        }

        baseline = _baseline_returns(context, config.horizon_steps, config.momentum_lookback)
        if row["kronos_valid"]:
            position, net = _strategy_net(
                float(row["kronos_expected_return"]),
                float(actual_return),
                config.round_trip_cost,
            )
            row["kronos_position"] = position
            row["kronos_net_return"] = net
        else:
            row["kronos_position"] = 0
            row["kronos_net_return"] = 0.0

        for name, forecast in baseline.items():
            row[f"{name}_valid"] = True
            row[f"{name}_expected_return"] = float(forecast)
            position, net = _strategy_net(float(forecast), float(actual_return), config.round_trip_cost)
            row[f"{name}_position"] = position
            row[f"{name}_net_return"] = net
        rows.append(row)

    result_rows = pd.DataFrame(rows)
    metrics = {method: _metric_summary(result_rows, method) for method in _METHODS}

    valid_kronos = result_rows.loc[result_rows["kronos_valid"]].copy()
    dm: dict[str, object] = {}
    bootstrap_net: dict[str, object] = {}
    if not valid_kronos.empty:
        for index, baseline in enumerate(_METHODS[1:]):
            dm[baseline] = diebold_mariano_absolute_error(
                valid_kronos["actual_return"],
                valid_kronos["kronos_expected_return"],
                valid_kronos[f"{baseline}_expected_return"],
            )
            bootstrap_net[baseline] = paired_bootstrap_net_advantage(
                valid_kronos["kronos_net_return"],
                valid_kronos[f"{baseline}_net_return"],
                seed=20261005 + index,
            )

    latency_values = result_rows["kronos_latency_seconds"].to_numpy(dtype=float)
    latency = {
        "count": int(latency_values.size),
        "mean_seconds": float(np.mean(latency_values)),
        "p50_seconds": float(np.percentile(latency_values, 50)),
        "p95_seconds": float(np.percentile(latency_values, 95)),
        "max_seconds": float(np.max(latency_values)),
    }

    return {
        "config": {
            "horizon_label": config.horizon_label,
            "horizon_steps": config.horizon_steps,
            "context_size": config.context_size,
            "stride": config.stride,
            "momentum_lookback": config.momentum_lookback,
            "fee_per_side": config.fee_per_side,
            "slippage_per_side": config.slippage_per_side,
            "round_trip_cost": config.round_trip_cost,
        },
        "rows": result_rows,
        "metrics": metrics,
        "confidence_calibration": _confidence_calibration(result_rows),
        "diebold_mariano_vs_baselines": dm,
        "bootstrap_net_advantage_vs_baselines": bootstrap_net,
        "latency": latency,
        "coverage": {
            "total_points": int(len(result_rows)),
            "kronos_valid_points": int(result_rows["kronos_valid"].sum()),
            "kronos_no_signal_points": int((~result_rows["kronos_valid"]).sum()),
        },
    }


def evaluate_walk_forward_batched(
    candles: pd.DataFrame,
    provider: SignalProvider,
    config: BacktestConfig,
    *,
    batch_size: int = 8,
) -> dict[str, object]:
    """Strict walk-forward evaluation using an offline inference batch.

    Batching changes only compute scheduling. Every forecast retains its own historical
    context and target. For latency, the full batch wall time is assigned to every
    member (a conservative upper bound for online one-signal inference).
    """

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if not isinstance(candles, pd.DataFrame) or candles.empty:
        raise ValueError("candles must be a non-empty DataFrame")
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = sorted(required.difference(candles.columns))
    if missing:
        raise ValueError(f"missing columns: {missing}")

    frame = candles.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
    if frame["timestamp"].duplicated().any():
        raise ValueError("duplicate timestamps are not allowed")

    first_target = config.context_size
    last_target_exclusive = len(frame) - config.horizon_steps + 1
    if first_target >= last_target_exclusive:
        raise ValueError("not enough candles for the requested walk-forward window")
    target_indices = list(range(first_target, last_target_exclusive, config.stride))

    rows: list[dict[str, object]] = []
    analyze_batch = getattr(provider, "analyze_batch", None)
    if not callable(analyze_batch):
        raise TypeError("provider must implement analyze_batch for batched walk-forward")

    for batch_start in range(0, len(target_indices), batch_size):
        indices = target_indices[batch_start : batch_start + batch_size]
        contexts = [
            frame.iloc[index - config.context_size : index].copy()
            for index in indices
        ]

        started = time.perf_counter()
        signals = analyze_batch(
            contexts,
            horizon=config.horizon_label,
            horizon_steps=config.horizon_steps,
            timeout_seconds=config.inference_timeout_seconds,
        )
        batch_latency = time.perf_counter() - started
        if len(signals) != len(indices):
            raise RuntimeError("provider returned a mismatched signal batch")

        for target_idx, context, signal in zip(indices, contexts, signals, strict=True):
            input_end_idx = target_idx - 1
            target_end_idx = target_idx + config.horizon_steps - 1
            input_close = float(frame.at[input_end_idx, "close"])
            target_close = float(frame.at[target_end_idx, "close"])
            actual_return = target_close / input_close - 1.0
            input_end_ts = frame.at[input_end_idx, "timestamp"]
            target_ts = frame.at[target_end_idx, "timestamp"]

            row: dict[str, object] = {
                "input_end_timestamp": input_end_ts.isoformat(),
                "target_timestamp": target_ts.isoformat(),
                "actual_return": float(actual_return),
                "kronos_latency_seconds": float(batch_latency),
                "kronos_batch_size": int(len(indices)),
                "kronos_confidence": float(signal.confidence),
                "kronos_valid": signal.direction != "no_signal" and signal.expected_return is not None,
                "kronos_expected_return": (
                    float(signal.expected_return)
                    if signal.expected_return is not None
                    else np.nan
                ),
            }

            baseline = _baseline_returns(
                context, config.horizon_steps, config.momentum_lookback
            )
            if row["kronos_valid"]:
                position, net = _strategy_net(
                    float(row["kronos_expected_return"]),
                    float(actual_return),
                    config.round_trip_cost,
                )
                row["kronos_position"] = position
                row["kronos_net_return"] = net
            else:
                row["kronos_position"] = 0
                row["kronos_net_return"] = 0.0

            for name, forecast in baseline.items():
                row[f"{name}_valid"] = True
                row[f"{name}_expected_return"] = float(forecast)
                position, net = _strategy_net(
                    float(forecast), float(actual_return), config.round_trip_cost
                )
                row[f"{name}_position"] = position
                row[f"{name}_net_return"] = net
            rows.append(row)

    result_rows = pd.DataFrame(rows)
    metrics = {method: _metric_summary(result_rows, method) for method in _METHODS}

    valid_kronos = result_rows.loc[result_rows["kronos_valid"]].copy()
    dm: dict[str, object] = {}
    bootstrap_net: dict[str, object] = {}
    if not valid_kronos.empty:
        for index, baseline in enumerate(_METHODS[1:]):
            dm[baseline] = diebold_mariano_absolute_error(
                valid_kronos["actual_return"],
                valid_kronos["kronos_expected_return"],
                valid_kronos[f"{baseline}_expected_return"],
            )
            bootstrap_net[baseline] = paired_bootstrap_net_advantage(
                valid_kronos["kronos_net_return"],
                valid_kronos[f"{baseline}_net_return"],
                seed=20261005 + index,
            )

    latency_values = result_rows["kronos_latency_seconds"].to_numpy(dtype=float)
    amortized = (
        result_rows["kronos_latency_seconds"].to_numpy(dtype=float)
        / result_rows["kronos_batch_size"].to_numpy(dtype=float)
    )
    latency = {
        "count": int(latency_values.size),
        "mean_seconds": float(np.mean(latency_values)),
        "p50_seconds": float(np.percentile(latency_values, 50)),
        "p95_seconds": float(np.percentile(latency_values, 95)),
        "max_seconds": float(np.max(latency_values)),
        "mean_amortized_seconds": float(np.mean(amortized)),
        "p95_amortized_seconds": float(np.percentile(amortized, 95)),
        "measurement": "full batch wall time assigned to each signal; amortized fields are throughput only",
    }

    return {
        "config": {
            "horizon_label": config.horizon_label,
            "horizon_steps": config.horizon_steps,
            "context_size": config.context_size,
            "stride": config.stride,
            "momentum_lookback": config.momentum_lookback,
            "fee_per_side": config.fee_per_side,
            "slippage_per_side": config.slippage_per_side,
            "round_trip_cost": config.round_trip_cost,
            "batch_size": int(batch_size),
        },
        "rows": result_rows,
        "metrics": metrics,
        "confidence_calibration": _confidence_calibration(result_rows),
        "diebold_mariano_vs_baselines": dm,
        "bootstrap_net_advantage_vs_baselines": bootstrap_net,
        "latency": latency,
        "coverage": {
            "total_points": int(len(result_rows)),
            "kronos_valid_points": int(result_rows["kronos_valid"].sum()),
            "kronos_no_signal_points": int((~result_rows["kronos_valid"]).sum()),
        },
    }
