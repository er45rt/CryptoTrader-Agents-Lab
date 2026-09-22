"""Typed errors for the shadow/paper trading layer (Phase 10). Engine-independent."""

from __future__ import annotations

__all__ = [
    "PaperConfigError",
    "PaperError",
    "PaperStateError",
    "SimulationError",
]


class PaperError(RuntimeError):
    """Base class for shadow/paper trading failures."""


class PaperConfigError(PaperError):
    """Mandatory session configuration is missing or invalid (fail closed)."""


class PaperStateError(PaperError):
    """The session was used in a state that does not permit the operation."""


class SimulationError(PaperError):
    """The simulated execution pass could not be performed faithfully."""
