"""Typed errors for the decision validity layer (Phase 6). Engine-independent."""

from __future__ import annotations

__all__ = ["MarketStateError", "ValidityConfigError", "ValidityError"]


class ValidityError(RuntimeError):
    """Base class for validity/staleness failures."""


class ValidityConfigError(ValidityError):
    """Mandatory validity configuration is missing or invalid (fail closed)."""


class MarketStateError(ValidityError):
    """A market observation or provider is not usable."""
