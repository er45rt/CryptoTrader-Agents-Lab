"""Deterministic bridge from safety mode to the Hard Risk engine inputs (Phase 9).

The safety layer sits *above* the deterministic risk layer: it does not re-implement risk and it
does not bypass it. It only derives the Phase 7 :class:`RiskLimits` the risk engine must use for
the current safety mode, so "halt blocks new risk" is enforced through the existing deterministic
engine rather than through a second, parallel rule set.

No new architecture is implied: the mapping is a pure function of the safety mode.
"""

from __future__ import annotations

from dataclasses import replace

from crypto_trader.risk import RiskLimits

from .controls import SafetyController
from .errors import SafetyStateError
from .modes import SafetyMode

__all__ = ["risk_limits_for_safety"]


def risk_limits_for_safety(controller: SafetyController, limits: RiskLimits) -> RiskLimits:
    """Return the risk limits the deterministic Hard Risk engine must use right now.

    * ``normal``            - the configured limits, unchanged.
    * ``reduce_only``       - exposure-increasing orders are blocked (``reduce_only_mode``).
    * everything else, or an engaged kill switch - all new risk is halted (``trading_halted``),
      which the Phase 7 engine enforces with the deterministic reason ``trading_halted``.
    """
    if not isinstance(controller, SafetyController):
        raise SafetyStateError("a SafetyController is required.")
    if not isinstance(limits, RiskLimits):
        raise SafetyStateError("a resolved RiskLimits rule set is required.")

    mode = controller.mode
    if controller.kill_switch_engaged:
        return replace(limits, trading_halted=True)
    if mode is SafetyMode.NORMAL:
        return limits
    if mode is SafetyMode.REDUCE_ONLY:
        return replace(limits, reduce_only_mode=True)
    return replace(limits, trading_halted=True)
