"""Typed errors for the live-readiness audit harness (Phase 12)."""

from __future__ import annotations

__all__ = ["AuditError", "ScenarioError"]


class AuditError(RuntimeError):
    """The audit could not be executed faithfully."""


class ScenarioError(AuditError):
    """A fault-injection scenario could not be set up."""
