from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from crypto_trader.research.kronos import KronosSignalProvider
from crypto_trader.research.kronos.pins import (
    KRONOS_CODE_COMMIT_DATE_UTC,
    KRONOS_MAX_CONTEXT,
    KRONOS_MIN_SAMPLE_COUNT,
    KRONOS_MODEL_REVISION_DATE_UTC,
    KRONOS_TOKENIZER_REVISION_DATE_UTC,
)


def candles(n: int = 600) -> pd.DataFrame:
    ts = pd.date_range("2025-01-01", periods=n, freq="5min", tz="UTC")
    close = 100.0 + np.arange(n) * 0.01
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": close - 0.01,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": np.full(n, 10.0),
            "amount": np.full(n, 1000.0),
        }
    )


class FakeBackend:
    def __init__(self, returns=None, error: Exception | None = None):
        self.returns = list(returns if returns is not None else np.linspace(0.005, 0.015, 20))
        self.error = error
        self.calls = []

    def sample_returns(self, frame, timestamps, *, horizon_steps, sample_count):
        self.calls.append((len(frame), timestamps[-1], horizon_steps, sample_count))
        if self.error is not None:
            raise self.error
        return self.returns


def test_schema_context_cap_and_minimum_stochastic_samples():
    backend = FakeBackend()
    provider = KronosSignalProvider(backend=backend, sample_count=20)

    signal = provider.analyze(candles(), horizon="5m")

    assert signal.direction == "long"
    assert signal.expected_return == pytest.approx(0.01)
    assert 0.0 <= signal.confidence <= 1.0
    assert set(signal.to_dict()) == {
        "direction",
        "expected_return",
        "confidence",
        "horizon",
        "model_version",
        "input_end_timestamp",
    }
    assert backend.calls[0][0] == KRONOS_MAX_CONTEXT
    assert backend.calls[0][3] == KRONOS_MIN_SAMPLE_COUNT


def test_public_json_payload_has_exact_required_fields():
    provider = KronosSignalProvider(backend=FakeBackend(), sample_count=20)

    import json
    payload = json.loads(provider.analyze_json(candles(30), horizon="5m"))

    assert set(payload) == {
        "direction",
        "expected_return",
        "confidence",
        "horizon",
        "model_version",
        "input_end_timestamp",
    }


def test_confidence_decreases_when_sample_dispersion_increases():
    tight = KronosSignalProvider(
        backend=FakeBackend([0.0099, 0.0101] * 10),
        sample_count=20,
    ).analyze(candles(30), horizon="5m")
    wide = KronosSignalProvider(
        backend=FakeBackend([-0.01, 0.03] * 10),
        sample_count=20,
    ).analyze(candles(30), horizon="5m")

    assert tight.expected_return == pytest.approx(wide.expected_return)
    assert tight.confidence > wide.confidence


@pytest.mark.parametrize(
    "mutator",
    [
        lambda df: df.drop(columns=["volume"]),
        lambda df: df.assign(close=np.nan),
        lambda df: df.assign(volume=-1.0),
    ],
)
def test_invalid_or_incomplete_data_fails_closed(mutator):
    provider = KronosSignalProvider(backend=FakeBackend(), sample_count=20)
    signal = provider.analyze(mutator(candles(30)), horizon="5m")

    assert signal.direction == "no_signal"
    assert signal.expected_return is None
    assert signal.confidence == 0.0


def test_backend_exception_fails_closed():
    provider = KronosSignalProvider(backend=FakeBackend(error=RuntimeError("boom")), sample_count=20)
    signal = provider.analyze(candles(30), horizon="5m")

    assert signal.direction == "no_signal"
    assert signal.expected_return is None
    assert "RuntimeError" in (provider.last_error or "")


class SlowBackend(FakeBackend):
    def sample_returns(self, frame, timestamps, *, horizon_steps, sample_count):
        time.sleep(0.05)
        return super().sample_returns(
            frame, timestamps, horizon_steps=horizon_steps, sample_count=sample_count
        )


def test_timeout_fails_closed():
    provider = KronosSignalProvider(backend=SlowBackend(), sample_count=20)
    signal = provider.analyze(candles(30), horizon="5m", timeout_seconds=0.001)

    assert signal.direction == "no_signal"
    assert signal.confidence == 0.0
    assert "TimeoutError" in (provider.last_error or "")


def test_sample_count_below_twenty_is_rejected():
    with pytest.raises(ValueError):
        KronosSignalProvider(backend=FakeBackend(), sample_count=19)


def test_time_gap_is_missing_data_and_fails_closed():
    frame = candles(30)
    frame.loc[15:, "timestamp"] = frame.loc[15:, "timestamp"] + pd.Timedelta(minutes=5)
    provider = KronosSignalProvider(backend=FakeBackend(), sample_count=20)

    signal = provider.analyze(frame, horizon="5m")

    assert signal.direction == "no_signal"
    assert signal.expected_return is None
    assert "regular interval" in (provider.last_error or "")


def test_pinned_source_model_and_tokenizer_predate_oos_start():
    cutoff = pd.Timestamp("2025-10-01T00:00:00Z")
    for raw in (
        KRONOS_CODE_COMMIT_DATE_UTC,
        KRONOS_MODEL_REVISION_DATE_UTC,
        KRONOS_TOKENIZER_REVISION_DATE_UTC,
    ):
        assert pd.Timestamp(raw) < cutoff
