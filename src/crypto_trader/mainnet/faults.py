"""Synthetic fault injection for the mainnet read-only dry-run (Phase 15) - TEST/FAULT-ONLY.

Stage 3 status (OPTION B): this module is RETAINED ONLY AS TEST / SYNTHETIC FAULT-INJECTION
MACHINERY. It is deliberately NOT part of the production durability path: production durability
comes exclusively from the approved ``order.submission.attempted`` attempt -> authorization -> send
boundary in the Binance testnet adapter (Stage 1 + Stage 2), and nothing here is evidence of that
durability. It is no longer re-exported from ``crypto_trader.mainnet``; the remaining consumers are
the dedicated offline fault tests, which import this module explicitly.

Two classes of fault MUST NOT be conflated:

1. **Immediate deterministic rejection** - permission, risk, validity, balance or filter failure.
   It is decided BEFORE the hypothetical send boundary, so it creates no ambiguous order state.
2. **Post-send ambiguity** - a timeout, connection reset, lost response or otherwise unknown
   outcome AFTER the hypothetical send boundary. The order may exist at the venue, so it MUST become
   ``UNKNOWN_PENDING_RECONCILIATION`` and drive the REAL Phase 8 recovery/reconciliation state
   machine. Simulating a pre-submit rejection and calling it "ambiguity" is refused in code.

No venue request is made here: the send boundary is hypothetical, and the local submission-attempt
marker is journaled exactly as the live path would journal it before a real send.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import UUID

from crypto_trader.execution import OrderLifecycleState
from crypto_trader.execution.binance_testnet import (
    TestnetOrder,
    deterministic_order_uuid,
    testnet_order_to_phase3_events,
)
from crypto_trader.persistence import EventJournal
from crypto_trader.recovery import (
    ReconciliationEngine,
    ReconciliationOutcome,
    ReconciliationPolicy,
    RestartRecovery,
    StaticVenueStateSource,
    reconstruct_order_states,
)

from .errors import DryRunError, MainnetConfigError

__all__ = [
    "IMMEDIATE_REJECTION_FAULTS",
    "POST_SEND_AMBIGUITY_FAULTS",
    "AmbiguityInjectionResult",
    "FaultClass",
    "SendFault",
    "classify_send_fault",
    "inject_post_send_ambiguity",
    "require_post_send_ambiguity",
]


class FaultClass(str, Enum):
    IMMEDIATE_REJECTION = "immediate_rejection"
    POST_SEND_AMBIGUITY = "post_send_ambiguity"


class SendFault(str, Enum):
    """Every injected fault, split explicitly by WHEN it happens."""

    PERMISSION_DENIED = "permission_denied"
    RISK_REJECTED = "risk_rejected"
    VALIDITY_REJECTED = "validity_rejected"
    BALANCE_INSUFFICIENT = "balance_insufficient"
    FILTER_REJECTED = "filter_rejected"
    TIMEOUT_AFTER_SEND = "timeout_after_send_boundary"
    CONNECTION_RESET_AFTER_SEND = "connection_reset_after_send_boundary"
    RESPONSE_LOST = "response_lost"
    UNKNOWN_OUTCOME = "unknown_outcome"


IMMEDIATE_REJECTION_FAULTS = frozenset(
    {
        SendFault.PERMISSION_DENIED,
        SendFault.RISK_REJECTED,
        SendFault.VALIDITY_REJECTED,
        SendFault.BALANCE_INSUFFICIENT,
        SendFault.FILTER_REJECTED,
    }
)

POST_SEND_AMBIGUITY_FAULTS = frozenset(
    {
        SendFault.TIMEOUT_AFTER_SEND,
        SendFault.CONNECTION_RESET_AFTER_SEND,
        SendFault.RESPONSE_LOST,
        SendFault.UNKNOWN_OUTCOME,
    }
)


def classify_send_fault(fault: SendFault | str) -> FaultClass:
    """Classify one fault; an unknown fault fails closed rather than being assumed ambiguous."""
    try:
        resolved = fault if isinstance(fault, SendFault) else SendFault(str(fault))
    except ValueError as exc:
        raise MainnetConfigError(f"unknown send fault {fault!r}; refusing to guess.") from exc
    if resolved in IMMEDIATE_REJECTION_FAULTS:
        return FaultClass.IMMEDIATE_REJECTION
    if resolved in POST_SEND_AMBIGUITY_FAULTS:
        return FaultClass.POST_SEND_AMBIGUITY
    raise MainnetConfigError(f"unclassified send fault {resolved!r}.")  # pragma: no cover


def require_post_send_ambiguity(fault: SendFault | str) -> SendFault:
    """Refuse to call a pre-submit rejection an ambiguity."""
    resolved = fault if isinstance(fault, SendFault) else SendFault(str(fault))
    if classify_send_fault(resolved) is not FaultClass.POST_SEND_AMBIGUITY:
        raise DryRunError(
            f"{resolved.value!r} is an immediate deterministic rejection decided before the send "
            "boundary; it must never be reported as post-send ambiguity."
        )
    return resolved


@dataclass(frozen=True)
class AmbiguityInjectionResult:
    """The evidence of one post-send ambiguity injection through the real recovery state machine."""

    fault: str
    order_reference: str
    outcome: str
    requires_halt_new_risk: bool
    journaled_event_ids: Sequence[UUID]
    reconciliation_state: str
    injected_at: datetime

    @property
    def is_unknown_pending_reconciliation(self) -> bool:
        return self.outcome == ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION.value

    def to_record(self) -> dict[str, object]:
        return {
            "fault": self.fault,
            "order_reference": self.order_reference,
            "outcome": self.outcome,
            "requires_halt_new_risk": self.requires_halt_new_risk,
            "reconciliation_state": self.reconciliation_state,
            "journaled_event_ids": [str(event_id) for event_id in self.journaled_event_ids],
            "injected_at": self.injected_at.isoformat(),
        }


def inject_post_send_ambiguity(
    *,
    journal: EventJournal,
    policy: ReconciliationPolicy,
    fault: SendFault | str,
    symbol: str,
    client_order_id: str,
    correlation_id: UUID,
    occurred_at: datetime,
    quantity: Decimal,
    producer: str = "application.mainnet.dryrun",
) -> AmbiguityInjectionResult:
    """Cross a hypothetical send boundary, lose the response, and reconcile for real."""
    resolved = require_post_send_ambiguity(fault)
    if not isinstance(quantity, Decimal) or quantity <= 0:
        raise DryRunError("an injected ambiguity requires a positive Decimal quantity.")
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise DryRunError("occurred_at must be timezone-aware.")

    # The submission-attempt marker is journaled BEFORE the (hypothetical) send, exactly as the live
    # path does, so a lost response leaves provable local evidence that a send may have occurred.
    submitted = TestnetOrder(
        symbol=symbol,
        client_order_id=client_order_id,
        venue_order_id=None,
        state=OrderLifecycleState.SUBMITTED,
        status="NEW",
        original_quantity=quantity,
    )
    for event in testnet_order_to_phase3_events(
        submitted, correlation_id=correlation_id, occurred_at=occurred_at
    ):
        journal.append(event)

    reconstructor, _ = reconstruct_order_states(journal)
    local_states = reconstructor.states()
    recovery = RestartRecovery(
        engine=ReconciliationEngine(policy),
        journal=journal,
        # The response was lost: there is NO venue evidence to reconcile against.
        venue_sources=[StaticVenueStateSource(entries=(), name="mainnet.response_lost")],
        producer=producer,
    )
    run = recovery.run(now=occurred_at, local_states=local_states, correlation_id=correlation_id)
    reference = str(deterministic_order_uuid(client_order_id))
    outcome = run.report.outcome_for(reference)
    if outcome is None:
        raise DryRunError(
            "the injected ambiguity did not produce a reconciliation outcome for the order; "
            "refusing to report success."
        )
    return AmbiguityInjectionResult(
        fault=resolved.value,
        order_reference=reference,
        outcome=outcome.value,
        requires_halt_new_risk=run.report.requires_halt_new_risk,
        journaled_event_ids=tuple(run.journaled_event_ids),
        reconciliation_state=run.recovery_state,
        injected_at=occurred_at,
    )
