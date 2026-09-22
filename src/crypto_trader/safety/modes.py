"""Operational safety modes and their deterministic policy (Phase 9).

The mode is the ONLY state; every capability below is a pure function of it. That keeps the
policy and the journaled record from drifting apart.

Capability matrix:

| mode              | allows_new_risk | allows_exposure_increase | requires_cancel_open | permits_live_execution |
|-------------------|-----------------|--------------------------|----------------------|------------------------|
| normal            | yes             | yes                      | no                   | yes                    |
| reduce_only       | no              | no                       | no                   | yes                    |
| halt_new_risk     | no              | no                       | no                   | yes                    |
| cancel_open       | no              | no                       | yes                  | yes                    |
| degraded          | no              | no                       | yes                  | no                     |
| recovery_required | no              | no                       | yes                  | no                     |

``degraded`` and ``recovery_required`` never resume trading on their own: an explicit operator
transition back to ``normal`` is always required after reconciliation.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["MODE_SEVERITY", "SafetyMode"]


class SafetyMode(str, Enum):
    """The fixed Phase 9 safety-mode vocabulary (engine-independent)."""

    NORMAL = "normal"
    REDUCE_ONLY = "reduce_only"
    HALT_NEW_RISK = "halt_new_risk"
    CANCEL_OPEN = "cancel_open"
    DEGRADED = "degraded"
    RECOVERY_REQUIRED = "recovery_required"

    @property
    def severity(self) -> int:
        """Higher is more restrictive. Used only for deterministic ordering/reporting."""
        return MODE_SEVERITY[self.value]

    @property
    def allows_new_risk(self) -> bool:
        """True only in ``normal``: the AI/strategy path may create new exposure."""
        return self is SafetyMode.NORMAL

    @property
    def allows_exposure_increase(self) -> bool:
        """Whether an order that increases gross exposure is permitted at all."""
        return self is SafetyMode.NORMAL

    @property
    def requires_cancel_open(self) -> bool:
        """Whether open orders must be cancelled through the priority safety lane."""
        return self in {SafetyMode.CANCEL_OPEN, SafetyMode.DEGRADED, SafetyMode.RECOVERY_REQUIRED}

    @property
    def permits_live_execution(self) -> bool:
        """False for the post-break-glass modes: no trading until reconciliation completes."""
        return self not in {SafetyMode.DEGRADED, SafetyMode.RECOVERY_REQUIRED}

    @property
    def requires_reconciliation(self) -> bool:
        return self in {SafetyMode.DEGRADED, SafetyMode.RECOVERY_REQUIRED}


MODE_SEVERITY: dict[str, int] = {
    "normal": 0,
    "reduce_only": 1,
    "halt_new_risk": 2,
    "cancel_open": 3,
    "degraded": 4,
    "recovery_required": 5,
}
