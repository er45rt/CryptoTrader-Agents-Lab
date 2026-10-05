"""Offline Kronos evaluation adapter and walk-forward research harness."""

from .backtest import BacktestConfig, evaluate_walk_forward, evaluate_walk_forward_batched
from .contracts import KronosSignal, SignalProvider
from .pins import pin_manifest
from .provider import KronosSignalProvider, PinnedKronosBackend, PinnedKronosBackendConfig

__all__ = [
    "BacktestConfig",
    "KronosSignal",
    "KronosSignalProvider",
    "PinnedKronosBackend",
    "PinnedKronosBackendConfig",
    "SignalProvider",
    "evaluate_walk_forward",
    "evaluate_walk_forward_batched",
    "pin_manifest",
]
