"""Restart recovery + reconciliation foundation (Phase 8).

Engine-independent by design: this package never imports Nautilus, CrewAI, the LLM layer or any
broker/storage engine. It reconciles OUR application view against an execution/venue view
without either silently overwriting the other.

Guarantees:

* an ambiguous submission is ``UNKNOWN_PENDING_RECONCILIATION`` - never silently converted to
  failed/rejected and never blindly retried;
* a crash before a send (no submission-attempt marker) provably resolves to ``NEVER_SUBMITTED``;
* every state change is an explicit adjustment carrying source, before state, after state and a
  reason code - there is no silent state correction;
* reconciliation outcomes are journaled through the existing Phase 3 ``recovery.state.changed``
  contract (no contract change).
"""

from .engine import ReconciliationEngine
from .errors import ReconciliationError, RecoveryConfigError, RecoveryError
from .journaling import reconciliation_events, record_reconciliation
from .models import (
    AdjustmentSource,
    LocalOrderState,
    OrderStateName,
    ReconciliationAdjustment,
    ReconciliationOutcome,
    ReconciliationReport,
    VenueOrderReport,
)
from .policy import SUPPORTED_UNKNOWN_EXTERNAL_ORDER_POLICY, ReconciliationPolicy, load_policy
from .reconstruction import (
    RECONSTRUCTION_SNAPSHOT_SCHEMA,
    OrderStateReconstructor,
    reconstruct_order_states,
)
from .runtime import (
    ReconciliationSources,
    RecoveryRun,
    RestartRecovery,
    StaticVenueStateSource,
    VenueStateSource,
)

__all__ = [
    "AdjustmentSource",
    "LocalOrderState",
    "OrderStateName",
    "OrderStateReconstructor",
    "RECONSTRUCTION_SNAPSHOT_SCHEMA",
    "ReconciliationAdjustment",
    "ReconciliationEngine",
    "ReconciliationError",
    "ReconciliationOutcome",
    "ReconciliationPolicy",
    "ReconciliationReport",
    "ReconciliationSources",
    "RecoveryConfigError",
    "RecoveryError",
    "RecoveryRun",
    "RestartRecovery",
    "SUPPORTED_UNKNOWN_EXTERNAL_ORDER_POLICY",
    "StaticVenueStateSource",
    "VenueOrderReport",
    "VenueStateSource",
    "load_policy",
    "reconstruct_order_states",
    "reconciliation_events",
    "record_reconciliation",
]
