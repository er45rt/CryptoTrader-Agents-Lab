"""Narrow, offline-only signal contract used by the Kronos research adapter."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol, runtime_checkable

SignalDirection = Literal["long", "short", "flat", "no_signal"]


@dataclass(frozen=True)
class KronosSignal:
    direction: SignalDirection
    expected_return: float | None
    confidence: float
    horizon: str
    model_version: str
    input_end_timestamp: str | None

    def __post_init__(self) -> None:
        if self.direction not in {"long", "short", "flat", "no_signal"}:
            raise ValueError("invalid signal direction")
        if not isinstance(self.confidence, (int, float)) or not math.isfinite(float(self.confidence)):
            raise ValueError("confidence must be finite")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.expected_return is not None:
            if not isinstance(self.expected_return, (int, float)) or not math.isfinite(float(self.expected_return)):
                raise ValueError("expected_return must be finite or null")
        if not isinstance(self.horizon, str) or not self.horizon.strip():
            raise ValueError("horizon must be non-empty")
        if not isinstance(self.model_version, str) or not self.model_version.strip():
            raise ValueError("model_version must be non-empty")
        if self.direction == "no_signal":
            if self.expected_return is not None:
                raise ValueError("no_signal must not carry an expected return")
            if float(self.confidence) != 0.0:
                raise ValueError("no_signal confidence must be zero")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)


@runtime_checkable
class SignalProvider(Protocol):
    def analyze(
        self,
        candles: Any,
        *,
        horizon: str,
        horizon_steps: int = 1,
        timeout_seconds: float | None = None,
    ) -> KronosSignal:
        """Return one typed signal. Failures must be represented as no_signal."""
