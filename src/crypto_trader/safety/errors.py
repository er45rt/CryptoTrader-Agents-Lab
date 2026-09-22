"""Typed errors for the operational safety controls (Phase 9). Engine-independent."""

from __future__ import annotations

__all__ = ["LaneBackpressureError", "SafetyConfigError", "SafetyError", "SafetyStateError"]


class SafetyError(RuntimeError):
    """Base class for safety-layer failures."""


class SafetyConfigError(SafetyError):
    """Mandatory safety configuration is missing or invalid (fail closed)."""


class SafetyStateError(SafetyError):
    """A safety state or transition request is not usable."""


class LaneBackpressureError(SafetyError):
    """The bounded normal lane is full; safety commands are never dropped."""
