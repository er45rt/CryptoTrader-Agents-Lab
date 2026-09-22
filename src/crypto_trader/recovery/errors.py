"""Typed errors for recovery and reconciliation (Phase 8). Engine-independent."""

from __future__ import annotations

__all__ = ["RecoveryConfigError", "RecoveryError", "ReconciliationError"]


class RecoveryError(RuntimeError):
    """Base class for recovery-layer failures."""


class RecoveryConfigError(RecoveryError):
    """Mandatory recovery/reconciliation configuration is missing (fail closed)."""


class ReconciliationError(RecoveryError):
    """Reconciliation could not be performed faithfully."""
