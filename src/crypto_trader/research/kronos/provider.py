"""Fail-closed Kronos signal provider for offline research.

The provider is intentionally not wired into CrewAI, proposals, OMS, risk, or execution.
It accepts an injectable backend so schema/failure logic can be tested without loading
the real model. The pinned backend only loads local snapshots and verifies all pins.
"""

from __future__ import annotations

import math
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np
import pandas as pd

from .contracts import KronosSignal, SignalProvider
from .pins import (
    KRONOS_CODE_COMMIT,
    KRONOS_MAX_CONTEXT,
    KRONOS_MIN_SAMPLE_COUNT,
    KRONOS_MODEL_REVISION,
    KRONOS_MODEL_VERSION,
    KRONOS_TOKENIZER_REVISION,
)

_REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")
_REVISION_MARKER = ".kronos_revision"


class KronosSamplingBackend(Protocol):
    def sample_returns(
        self,
        candles: pd.DataFrame,
        timestamps: pd.DatetimeIndex,
        *,
        horizon_steps: int,
        sample_count: int,
    ) -> Sequence[float]:
        """Return one terminal-close return for each stochastic sample."""


@dataclass(frozen=True)
class PinnedKronosBackendConfig:
    source_dir: Path
    model_dir: Path
    tokenizer_dir: Path
    device: str = "cpu"
    max_context: int = KRONOS_MAX_CONTEXT
    temperature: float = 1.0
    top_k: int = 0
    top_p: float = 0.9


class PinnedKronosBackend:
    """Adapter around the exact pinned Kronos code and local HF snapshots."""

    def __init__(self, config: PinnedKronosBackendConfig) -> None:
        self.config = config
        self._predictor = None

    @staticmethod
    def _require_revision_marker(path: Path, expected: str) -> None:
        marker = path / _REVISION_MARKER
        if not marker.is_file():
            raise RuntimeError(f"missing revision marker: {marker}")
        actual = marker.read_text(encoding="utf-8").strip()
        if actual != expected:
            raise RuntimeError(f"snapshot revision mismatch at {path}: expected {expected}, got {actual}")

    def _verify_pins(self) -> None:
        source_dir = self.config.source_dir.resolve()
        completed = subprocess.run(
            ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        actual_code = completed.stdout.strip()
        if actual_code != KRONOS_CODE_COMMIT:
            raise RuntimeError(
                f"Kronos source commit mismatch: expected {KRONOS_CODE_COMMIT}, got {actual_code}"
            )
        self._require_revision_marker(self.config.model_dir, KRONOS_MODEL_REVISION)
        self._require_revision_marker(self.config.tokenizer_dir, KRONOS_TOKENIZER_REVISION)

    def _load(self):
        if self._predictor is not None:
            return self._predictor

        self._verify_pins()
        source = str(self.config.source_dir.resolve())
        if source not in sys.path:
            sys.path.insert(0, source)

        from model.kronos import Kronos, KronosPredictor, KronosTokenizer

        tokenizer = KronosTokenizer.from_pretrained(str(self.config.tokenizer_dir.resolve()))
        model = Kronos.from_pretrained(str(self.config.model_dir.resolve()))
        if hasattr(tokenizer, "eval"):
            tokenizer.eval()
        if hasattr(model, "eval"):
            model.eval()

        self._predictor = KronosPredictor(
            model,
            tokenizer,
            device=self.config.device,
            max_context=self.config.max_context,
        )
        return self._predictor

    def _sample_one_step_batch(
        self,
        candles_list: Sequence[pd.DataFrame],
        timestamps_list: Sequence[pd.DatetimeIndex],
        *,
        sample_count: int,
    ) -> list[list[float]]:
        """Vectorize independent one-step contexts while preserving per-context samples."""
        predictor = self._load()
        import torch
        from model.kronos import calc_time_stamps, sample_from_logits

        if len(candles_list) != len(timestamps_list) or not candles_list:
            raise ValueError("batch candles/timestamps must be non-empty and have equal lengths")

        feature_cols = ["open", "high", "low", "close", "volume", "amount"]
        normalized: list[np.ndarray] = []
        stamps: list[np.ndarray] = []
        means: list[np.ndarray] = []
        stds: list[np.ndarray] = []
        last_closes: list[float] = []
        expected_len: int | None = None

        for candles, timestamps in zip(candles_list, timestamps_list, strict=True):
            frame = candles.copy()
            if "amount" not in frame.columns:
                frame["amount"] = (
                    frame["volume"] * frame[["open", "high", "low", "close"]].mean(axis=1)
                )
            x = frame[feature_cols].to_numpy(dtype=np.float32)
            if expected_len is None:
                expected_len = x.shape[0]
            elif x.shape[0] != expected_len:
                raise ValueError("batched one-step inference requires equal context lengths")
            x_mean = np.mean(x, axis=0)
            x_std = np.std(x, axis=0)
            normalized.append(
                np.clip((x - x_mean) / (x_std + 1e-5), -predictor.clip, predictor.clip)
            )
            stamps.append(
                calc_time_stamps(pd.Series(timestamps)).to_numpy(dtype=np.float32)
            )
            means.append(x_mean)
            stds.append(x_std)
            last_closes.append(float(candles["close"].iloc[-1]))

        x_tensor = torch.from_numpy(np.stack(normalized, axis=0)).to(predictor.device)
        stamp_tensor = torch.from_numpy(np.stack(stamps, axis=0)).to(predictor.device)
        batch_size = len(candles_list)

        with torch.inference_mode():
            x_tensor = torch.clip(x_tensor, -predictor.clip, predictor.clip)
            token_pre, token_post = predictor.tokenizer.encode(x_tensor, half=True)
            s1_logits, context = predictor.model.decode_s1(token_pre, token_post, stamp_tensor)

            last_s1_logits = s1_logits[:, -1, :].repeat_interleave(sample_count, dim=0)
            sampled_pre = sample_from_logits(
                last_s1_logits,
                temperature=self.config.temperature,
                top_k=self.config.top_k,
                top_p=self.config.top_p,
                sample_logits=True,
            )

            repeated_context = context.repeat_interleave(sample_count, dim=0)
            s2_logits = predictor.model.decode_s2(repeated_context, sampled_pre)
            sampled_post = sample_from_logits(
                s2_logits[:, -1, :],
                temperature=self.config.temperature,
                top_k=self.config.top_k,
                top_p=self.config.top_p,
                sample_logits=True,
            )

            repeated_pre = token_pre.repeat_interleave(sample_count, dim=0)
            repeated_post = token_post.repeat_interleave(sample_count, dim=0)
            full_pre = torch.cat([repeated_pre, sampled_pre], dim=1)
            full_post = torch.cat([repeated_post, sampled_post], dim=1)
            total_len = full_pre.size(1)
            context_start = max(0, total_len - self.config.max_context)
            decoded = predictor.tokenizer.decode(
                [
                    full_pre[:, context_start:total_len].contiguous(),
                    full_post[:, context_start:total_len].contiguous(),
                ],
                half=True,
            )
            normalized_terminal = decoded[:, -1, :].cpu().numpy()

        mean_matrix = np.repeat(np.stack(means, axis=0), sample_count, axis=0)
        std_matrix = np.repeat(np.stack(stds, axis=0), sample_count, axis=0)
        terminal = normalized_terminal * (std_matrix + 1e-5) + mean_matrix
        terminal = terminal.reshape(batch_size, sample_count, -1)
        result: list[list[float]] = []
        for index in range(batch_size):
            returns = terminal[index, :, 3] / last_closes[index] - 1.0
            if returns.shape != (sample_count,) or not np.isfinite(returns).all():
                raise ValueError("Kronos produced invalid batched one-step stochastic returns")
            result.append(returns.astype(float).tolist())
        return result

    def _sample_one_step(
        self,
        candles: pd.DataFrame,
        timestamps: pd.DatetimeIndex,
        *,
        sample_count: int,
    ) -> Sequence[float]:
        """Equivalent one-step sampling with the deterministic context encoded once."""
        return self._sample_one_step_batch(
            [candles], [timestamps], sample_count=sample_count
        )[0]

    def sample_returns_batch(
        self,
        candles_list: Sequence[pd.DataFrame],
        timestamps_list: Sequence[pd.DatetimeIndex],
        *,
        horizon_steps: int,
        sample_count: int,
    ) -> list[list[float]]:
        if horizon_steps != 1:
            raise ValueError("batched research path currently supports one-step horizons only")
        if sample_count < KRONOS_MIN_SAMPLE_COUNT:
            raise ValueError("sample_count below mandatory minimum")
        if any(len(frame) > self.config.max_context for frame in candles_list):
            raise ValueError("backend received context longer than pinned maximum")
        return self._sample_one_step_batch(
            candles_list, timestamps_list, sample_count=sample_count
        )

    def sample_returns(
        self,
        candles: pd.DataFrame,
        timestamps: pd.DatetimeIndex,
        *,
        horizon_steps: int,
        sample_count: int,
    ) -> Sequence[float]:
        predictor = self._load()
        if len(candles) > self.config.max_context:
            raise ValueError("backend received context longer than pinned maximum")
        if sample_count < KRONOS_MIN_SAMPLE_COUNT:
            raise ValueError("sample_count below mandatory minimum")

        diffs = timestamps.to_series().diff().dropna()
        if diffs.empty:
            raise ValueError("at least two timestamps are required")
        interval = diffs.iloc[-1]

        if horizon_steps == 1:
            return self._sample_one_step(candles, timestamps, sample_count=sample_count)

        y_timestamp = pd.DatetimeIndex(
            [timestamps[-1] + interval * step for step in range(1, horizon_steps + 1)]
        )
        frames = [candles.copy(deep=False) for _ in range(sample_count)]
        x_times = [pd.Series(timestamps) for _ in range(sample_count)]
        y_times = [pd.Series(y_timestamp) for _ in range(sample_count)]

        forecasts = predictor.predict_batch(
            df_list=frames,
            x_timestamp_list=x_times,
            y_timestamp_list=y_times,
            pred_len=horizon_steps,
            T=self.config.temperature,
            top_k=self.config.top_k,
            top_p=self.config.top_p,
            sample_count=1,
            verbose=False,
        )
        last_close = float(candles["close"].iloc[-1])
        returns: list[float] = []
        for forecast in forecasts:
            terminal_close = float(forecast["close"].iloc[-1])
            ret = terminal_close / last_close - 1.0
            if not math.isfinite(ret):
                raise ValueError("Kronos produced a non-finite return")
            returns.append(ret)
        if len(returns) != sample_count:
            raise ValueError("Kronos returned an unexpected sample count")
        return returns


@dataclass(frozen=True)
class KronosSignalProvider(SignalProvider):
    backend: KronosSamplingBackend
    sample_count: int = KRONOS_MIN_SAMPLE_COUNT
    max_context: int = KRONOS_MAX_CONTEXT
    model_version: str = KRONOS_MODEL_VERSION

    def __post_init__(self) -> None:
        if self.sample_count < KRONOS_MIN_SAMPLE_COUNT:
            raise ValueError(
                f"sample_count must be >= {KRONOS_MIN_SAMPLE_COUNT}; got {self.sample_count}"
            )
        if not 2 <= self.max_context <= KRONOS_MAX_CONTEXT:
            raise ValueError(f"max_context must be in [2, {KRONOS_MAX_CONTEXT}]")
        object.__setattr__(self, "_last_error", None)

    @property
    def last_error(self) -> str | None:
        return getattr(self, "_last_error", None)

    def _no_signal(self, horizon: str, input_end_timestamp: str | None, reason: str) -> KronosSignal:
        object.__setattr__(self, "_last_error", reason)
        return KronosSignal(
            direction="no_signal",
            expected_return=None,
            confidence=0.0,
            horizon=horizon,
            model_version=self.model_version,
            input_end_timestamp=input_end_timestamp,
        )

    @staticmethod
    def _normalize(candles: pd.DataFrame, max_context: int) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
        if not isinstance(candles, pd.DataFrame):
            raise TypeError("candles must be a pandas DataFrame")
        if candles.empty:
            raise ValueError("candles are empty")
        missing = [name for name in _REQUIRED_COLUMNS if name not in candles.columns]
        if missing:
            raise ValueError(f"missing candle columns: {missing}")

        frame = candles.copy()
        if "timestamp" in frame.columns:
            timestamps = pd.DatetimeIndex(pd.to_datetime(frame.pop("timestamp"), utc=True))
        elif isinstance(frame.index, pd.DatetimeIndex):
            timestamps = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True))
        else:
            raise ValueError("candles require a DatetimeIndex or timestamp column")

        if len(frame) != len(timestamps):
            raise ValueError("timestamp/candle length mismatch")
        if len(frame) < 2:
            raise ValueError("at least two candles are required")
        if not timestamps.is_monotonic_increasing or timestamps.has_duplicates:
            raise ValueError("timestamps must be strictly increasing")

        numeric = frame.loc[:, list(_REQUIRED_COLUMNS)].apply(pd.to_numeric, errors="coerce")
        values = numeric.to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError("candles contain missing or non-finite OHLCV values")
        if (numeric[["open", "high", "low", "close"]] <= 0).any().any():
            raise ValueError("prices must be positive")
        if (numeric["volume"] < 0).any():
            raise ValueError("volume must be non-negative")
        if (numeric["high"] < numeric[["open", "low", "close"]].max(axis=1)).any():
            raise ValueError("high is inconsistent with OHLC")
        if (numeric["low"] > numeric[["open", "high", "close"]].min(axis=1)).any():
            raise ValueError("low is inconsistent with OHLC")

        diffs = timestamps.to_series().diff().dropna()
        if diffs.empty or (diffs != diffs.iloc[0]).any():
            raise ValueError("candles must have a regular interval with no gaps")

        frame.loc[:, list(_REQUIRED_COLUMNS)] = numeric
        if "amount" in frame.columns:
            amount = pd.to_numeric(frame["amount"], errors="coerce")
            if not np.isfinite(amount.to_numpy(dtype=float)).all() or (amount < 0).any():
                raise ValueError("amount must be finite and non-negative")
            frame.loc[:, "amount"] = amount

        if len(frame) > max_context:
            frame = frame.iloc[-max_context:].copy()
            timestamps = timestamps[-max_context:]
        frame.index = pd.RangeIndex(len(frame))
        return frame, timestamps

    @staticmethod
    def _confidence(sample_returns: np.ndarray) -> float:
        mean_return = float(np.mean(sample_returns))
        dispersion = float(np.std(sample_returns, ddof=1))
        denom = abs(mean_return) + dispersion
        if denom <= 1e-15:
            return 0.0
        return float(np.clip(abs(mean_return) / denom, 0.0, 1.0))

    def analyze(
        self,
        candles: pd.DataFrame,
        *,
        horizon: str,
        horizon_steps: int = 1,
        timeout_seconds: float | None = None,
    ) -> KronosSignal:
        input_end: str | None = None
        try:
            if not isinstance(horizon, str) or not horizon.strip():
                raise ValueError("horizon must be a non-empty string")
            if not isinstance(horizon_steps, int) or isinstance(horizon_steps, bool) or horizon_steps < 1:
                raise ValueError("horizon_steps must be a positive integer")
            if timeout_seconds is not None and timeout_seconds <= 0:
                raise ValueError("timeout_seconds must be positive")

            frame, timestamps = self._normalize(candles, self.max_context)
            input_end = timestamps[-1].isoformat()

            if timeout_seconds is None:
                raw_samples = self.backend.sample_returns(
                    frame,
                    timestamps,
                    horizon_steps=horizon_steps,
                    sample_count=self.sample_count,
                )
            else:
                pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kronos-signal")
                future = pool.submit(
                    self.backend.sample_returns,
                    frame,
                    timestamps,
                    horizon_steps=horizon_steps,
                    sample_count=self.sample_count,
                )
                try:
                    raw_samples = future.result(timeout=timeout_seconds)
                except FutureTimeoutError as exc:
                    future.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise TimeoutError(f"Kronos inference exceeded {timeout_seconds}s") from exc
                else:
                    pool.shutdown(wait=True)

            samples = np.asarray(list(raw_samples), dtype=float)
            if samples.shape != (self.sample_count,) or not np.isfinite(samples).all():
                raise ValueError("backend returned invalid stochastic samples")

            expected_return = float(np.mean(samples))
            confidence = self._confidence(samples)
            direction = "long" if expected_return > 0 else "short" if expected_return < 0 else "flat"
            object.__setattr__(self, "_last_error", None)
            return KronosSignal(
                direction=direction,
                expected_return=expected_return,
                confidence=confidence,
                horizon=horizon,
                model_version=self.model_version,
                input_end_timestamp=input_end,
            )
        except Exception as exc:
            return self._no_signal(horizon or "unknown", input_end, f"{type(exc).__name__}: {exc}")

    def analyze_json(
        self,
        candles: pd.DataFrame,
        *,
        horizon: str,
        horizon_steps: int = 1,
        timeout_seconds: float | None = None,
    ) -> str:
        """Return the exact six-field public signal payload as JSON."""

        return self.analyze(
            candles,
            horizon=horizon,
            horizon_steps=horizon_steps,
            timeout_seconds=timeout_seconds,
        ).to_json()

    def analyze_batch(
        self,
        candles_batch: Sequence[pd.DataFrame],
        *,
        horizon: str,
        horizon_steps: int = 1,
        timeout_seconds: float | None = None,
    ) -> list[KronosSignal]:
        """Fail-closed batch wrapper used only by the offline walk-forward harness."""

        if not candles_batch:
            return []
        if horizon_steps != 1 or not hasattr(self.backend, "sample_returns_batch"):
            return [
                self.analyze(
                    candles,
                    horizon=horizon,
                    horizon_steps=horizon_steps,
                    timeout_seconds=timeout_seconds,
                )
                for candles in candles_batch
            ]

        normalized: list[pd.DataFrame] = []
        timestamps_list: list[pd.DatetimeIndex] = []
        input_ends: list[str] = []
        try:
            for candles in candles_batch:
                frame, timestamps = self._normalize(candles, self.max_context)
                normalized.append(frame)
                timestamps_list.append(timestamps)
                input_ends.append(timestamps[-1].isoformat())

            batch_fn = getattr(self.backend, "sample_returns_batch")
            if timeout_seconds is None:
                raw_batch = batch_fn(
                    normalized,
                    timestamps_list,
                    horizon_steps=horizon_steps,
                    sample_count=self.sample_count,
                )
            else:
                pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kronos-batch")
                future = pool.submit(
                    batch_fn,
                    normalized,
                    timestamps_list,
                    horizon_steps=horizon_steps,
                    sample_count=self.sample_count,
                )
                try:
                    raw_batch = future.result(timeout=timeout_seconds)
                except FutureTimeoutError as exc:
                    future.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise TimeoutError(f"Kronos batch inference exceeded {timeout_seconds}s") from exc
                else:
                    pool.shutdown(wait=True)

            if len(raw_batch) != len(normalized):
                raise ValueError("backend returned unexpected batch size")

            signals: list[KronosSignal] = []
            for raw_samples, input_end in zip(raw_batch, input_ends, strict=True):
                samples = np.asarray(list(raw_samples), dtype=float)
                if samples.shape != (self.sample_count,) or not np.isfinite(samples).all():
                    raise ValueError("backend returned invalid stochastic samples")
                expected_return = float(np.mean(samples))
                confidence = self._confidence(samples)
                direction = (
                    "long" if expected_return > 0 else "short" if expected_return < 0 else "flat"
                )
                signals.append(
                    KronosSignal(
                        direction=direction,
                        expected_return=expected_return,
                        confidence=confidence,
                        horizon=horizon,
                        model_version=self.model_version,
                        input_end_timestamp=input_end,
                    )
                )
            object.__setattr__(self, "_last_error", None)
            return signals
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            object.__setattr__(self, "_last_error", reason)
            results: list[KronosSignal] = []
            for idx in range(len(candles_batch)):
                input_end = input_ends[idx] if idx < len(input_ends) else None
                results.append(self._no_signal(horizon or "unknown", input_end, reason))
            return results
