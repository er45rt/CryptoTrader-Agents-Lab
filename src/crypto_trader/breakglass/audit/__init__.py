"""Phase 13 pre-live break-glass audit.

The audit is deterministic and fully offline. It injects faults into the REAL components (the event
journal, the Phase 8 reconciliation engine, the Phase 9 safety controller, the Phase 11 Spot
Testnet adapter driven by a scripted transport, and the Phase 13 break-glass control path) and
derives the verdict mechanically.

Verdict vocabulary:

* ``BREAK-GLASS READY FOR OWNER REVIEW`` - every required item and every scenario passed;
* ``BREAK-GLASS NOT READY`` - at least one required item or scenario failed.

Readiness is NOT live authorization: no mainnet endpoint, no real capital, no withdrawal and no
liquidation is involved anywhere, and the owner remains the only decider for any live phase.
"""

from .checks import BreakGlassItem, run_break_glass_checks
from .matrix import (
    VERDICT_NOT_READY,
    VERDICT_READY,
    BreakGlassAuditReport,
    run_break_glass_audit,
)
from .scenarios import SCENARIOS, BreakGlassScenarioResult, run_all_scenarios, run_scenario

__all__ = [
    "SCENARIOS",
    "VERDICT_NOT_READY",
    "VERDICT_READY",
    "BreakGlassAuditReport",
    "BreakGlassItem",
    "BreakGlassScenarioResult",
    "run_all_scenarios",
    "run_break_glass_audit",
    "run_break_glass_checks",
    "run_scenario",
]
