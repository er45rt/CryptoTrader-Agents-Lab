"""Typed errors for the execution boundary (Phase 4).

Engine-independent: this module never imports Nautilus.
"""

from __future__ import annotations

__all__ = [
    "EngineVersionError",
    "ExecutionBoundaryError",
    "QueueAwareConfigurationError",
    "RuntimeStateError",
    "TranslationError",
    "UnsupportedExecutionRequestError",
]


class ExecutionBoundaryError(RuntimeError):
    """Base class for every execution-boundary failure."""


class UnsupportedExecutionRequestError(ExecutionBoundaryError):
    """The request cannot be represented faithfully by the engine; fail closed."""


class EngineVersionError(ExecutionBoundaryError):
    """The installed engine version is not the supported, tested version."""


class QueueAwareConfigurationError(ExecutionBoundaryError):
    """Queue-aware simulation was requested without the tested configuration."""


class TranslationError(ExecutionBoundaryError):
    """A value could not be translated without loss or guessing."""


class RuntimeStateError(ExecutionBoundaryError):
    """The runtime was used in a lifecycle state that does not permit the operation."""
