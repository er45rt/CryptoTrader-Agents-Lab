"""Deterministic operational safety controls (Phase 9).

Engine-independent by design: no Nautilus, CrewAI, LLM, broker or storage engine imports. The
safety lane carves out a bounded, always-available path for halt/cancel/kill-switch commands so
they never wait behind AI inference or strategy backlog.

Guarantees:

* the mode is the only state, and every capability is a pure function of it;
* the kill switch is deterministic, observable, journaled, restart-persistent and fail-closed
  (an unreadable persisted state restores into ``recovery_required``, never ``normal``);
* repeated identical requests are idempotent no-ops and produce no duplicate event;
* a break-glass use forces ``degraded`` -> ``recovery_required`` and prohibits new trading until
  reconciliation and an explicit operator transition back to ``normal``;
* break-glass ships as an interface plus a simulated implementation only - there is no venue
  connectivity and no credential anywhere in this package.
"""

from .breakglass import BreakGlassAction, BreakGlassPort, BreakGlassRequest, SimulatedBreakGlass
from .controls import SAFETY_STATE_SCHEMA_VERSION, SafetyController, SafetyTransition, restore_safety_controller
from .errors import LaneBackpressureError, SafetyConfigError, SafetyError, SafetyStateError
from .integration import risk_limits_for_safety
from .journaling import record_safety_state, record_safety_transitions, safety_state_events
from .lane import LaneCommand, LanePriority, PrioritySafetyLane
from .modes import MODE_SEVERITY, SafetyMode

__all__ = [
    "BreakGlassAction",
    "BreakGlassPort",
    "BreakGlassRequest",
    "LaneBackpressureError",
    "LaneCommand",
    "LanePriority",
    "MODE_SEVERITY",
    "PrioritySafetyLane",
    "SAFETY_STATE_SCHEMA_VERSION",
    "SafetyConfigError",
    "SafetyController",
    "SafetyError",
    "SafetyMode",
    "SafetyStateError",
    "SafetyTransition",
    "SimulatedBreakGlass",
    "record_safety_state",
    "record_safety_transitions",
    "restore_safety_controller",
    "risk_limits_for_safety",
    "safety_state_events",
]
