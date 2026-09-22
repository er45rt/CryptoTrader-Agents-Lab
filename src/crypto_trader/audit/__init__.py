"""Technical live-readiness audit (Phase 12).

The audit is engine-independent and fully offline: it injects faults into the REAL components
(journal, validity, hard risk, safety, recovery, paper session and the testnet adapter with a
scripted transport) and derives a readiness verdict mechanically.

NO mainnet endpoint, NO real capital and NO withdrawal is ever involved.

Verdict: ``TECHNICALLY READY FOR OWNER REVIEW`` only when every REQUIRED readiness item passes and
every fault-injection scenario passes. Technical readiness is NOT live authorization - the owner
remains the only decider for any live phase.
"""

from .checks import ReadinessItem, risk_is_deterministic, run_static_checks
from .errors import AuditError, ScenarioError
from .matrix import (
    VERDICT_NOT_READY,
    VERDICT_READY,
    AuditReport,
    run_audit,
)
from .scenarios import SCENARIOS, ScenarioResult, run_all_scenarios, run_scenario

__all__ = [
    "AuditError",
    "AuditReport",
    "ReadinessItem",
    "SCENARIOS",
    "ScenarioError",
    "ScenarioResult",
    "VERDICT_NOT_READY",
    "VERDICT_READY",
    "risk_is_deterministic",
    "run_all_scenarios",
    "run_audit",
    "run_scenario",
    "run_static_checks",
]
