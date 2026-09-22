"""Typed errors for the deterministic Hard Risk engine (Phase 7). Engine-independent."""

from __future__ import annotations

__all__ = ["RiskConfigError", "RiskError", "RiskStateError"]


class RiskError(RuntimeError):
    """Base class for risk-layer failures."""


class RiskConfigError(RiskError):
    """Mandatory risk configuration is missing or invalid (fail closed)."""


class RiskStateError(RiskError):
    """The supplied risk state is not usable."""
