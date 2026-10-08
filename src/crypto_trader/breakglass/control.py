"""The operational break-glass control path (Phase 13).

One deterministic, engine-independent, LLM-free ingress for operator break-glass control:

    operator CLI / control channel -> OperatorCommand -> BreakGlassControl -> journal (+ caches)

Guarantees:

* deterministic: the same safety state, ownership record, epoch, venue outcome and command always
  produce the same disposition - no LLM, agent, timeout or wall-clock heuristic can influence it;
* auditable and idempotent: every command is recorded once through the additive
  ``safety.operator_command.recorded`` contract; re-delivering the same ``command_id`` produces one
  logical effect;
* fail-closed: a refused command changes nothing, an unconfirmed venue-side control escalates
  through ``degraded`` into ``recovery_required`` with ``UNKNOWN_PENDING_RECONCILIATION``, and a
  corrupt persisted state fails closed into ``recovery_required``;
* single-writer: emergency break-glass actions take the writer lease as the emergency owner
  and mint a new fencing term; ``reduce_only`` is an audited policy restriction that preserves
  the current normal lease while Hard Risk proves each reduction;
* no automatic return to ``normal``: emergency epochs require resolved reconciliation plus
  operator acknowledgement; a policy-only ``reduce_only`` posture still requires an explicit
  audited operator restore.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from crypto_trader.events import (
    OperatorCommandRecordPayload,
    OperatorCommandRecorded,
    SafetyStateChangePayload,
    SafetyStateChanged,
)
from crypto_trader.persistence import EventJournal
from crypto_trader.recovery import (
    LocalOrderState,
    ReconciliationEngine,
    ReconciliationPolicy,
    RestartRecovery,
    StaticVenueStateSource,
    VenueOrderReport,
    reconstruct_order_states,
)
from crypto_trader.safety import (
    SafetyController,
    SafetyMode,
    SafetyTransition,
    risk_limits_for_safety,
)
from crypto_trader.risk import RiskLimits

from .commands import (
    ACTION_TARGET_MODES,
    BREAK_GLASS_ACTIONS,
    OperatorCommand,
    OperatorCommandType,
)
from .epoch import (
    BreakGlassEpoch,
    BreakGlassLedger,
    RecordedOperatorCommand,
    derive_ledger,
    synthesise_epoch_from_safety_state,
)
from .errors import (
    BreakGlassActiveError,
    BreakGlassConfigError,
    OwnershipStateError,
)
from .ownership import OwnerKind, OwnershipRecord, OwnershipStore, WriterGuard
from .state import SafetyStateLoad, SafetyStateStore
from .venue import EmergencyVenuePort, PolicyOnlyVenueControl, VenueControlOutcome

__all__ = [
    "BREAK_GLASS_PRODUCER",
    "KILL_SWITCH_ACTIONS",
    "BreakGlassControl",
    "BreakGlassStatus",
    "OperatorCommandOutcome",
    "break_glass_state_path",
]

BREAK_GLASS_PRODUCER = "application.breakglass"

#: The actions that engage the kill switch. ``reduce_only`` is deliberately excluded: the mode
#: itself already forbids new exposure, while ``engage_kill_switch`` would force ``halt_new_risk``
#: first and the journaled ladder would then appear to move from ``halt_new_risk`` down to
#: ``reduce_only``. Keeping it out preserves a monotone escalation ladder.
KILL_SWITCH_ACTIONS = frozenset(
    {
        OperatorCommandType.HALT_NEW_RISK,
        OperatorCommandType.CANCEL_OPEN,
        OperatorCommandType.ENTER_DEGRADED,
        OperatorCommandType.FORCE_RECOVERY_REQUIRED,
    }
)


def break_glass_state_path(state_dir: str | Path, name: str) -> Path:
    """The deterministic path of one break-glass state file inside ``state_dir``."""
    if not name or Path(name).name != name:
        raise BreakGlassConfigError("a break-glass state file needs a plain file name.")
    return Path(state_dir) / name


def _mode_path(previous_mode: SafetyMode, transitions: Sequence[SafetyTransition]) -> list[str]:
    """The safety modes a command traversed, starting with the mode it started from."""
    path = [previous_mode.value]
    for transition in transitions:
        if path[-1] != transition.mode.value:
            path.append(transition.mode.value)
    return path


@dataclass(frozen=True)
class OperatorCommandOutcome:
    """The complete, deterministic, secret-free result of one operator command."""

    command_id: UUID
    command_type: str
    disposition: str
    reason_code: str
    previous_safety_state: str
    resulting_safety_state: str
    correlation_id: UUID | None
    causation_id: UUID | None
    delivered_at: datetime
    duplicate: bool = False
    epoch_opened: bool = False
    epoch_closed: bool = False
    epoch_session_id: UUID | None = None
    reconciliation_required: bool = False
    reconciliation_status: str = "not_required"
    reconciliation_result: str | None = None
    operator_acknowledgement_pending: bool = True
    new_risk_permitted: bool = False
    venue_control: str = "not_requested"
    venue_detail: str | None = None
    fence_term: int = 0
    emergency_owner: str | None = None
    safety_transitions: tuple[str, ...] = ()
    journaled_event_ids: tuple[UUID, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    safety_state_source: str = "state_file"

    @property
    def accepted(self) -> bool:
        return self.disposition == "accepted"

    def to_record(self) -> dict[str, Any]:
        """The structured audit evidence for this command (never contains credential material)."""
        return {
            "command_id": str(self.command_id),
            "command_type": self.command_type,
            "disposition": self.disposition,
            "reason_code": self.reason_code,
            "previous_safety_state": self.previous_safety_state,
            "resulting_safety_state": self.resulting_safety_state,
            "duplicate_delivery": self.duplicate,
            "correlation_id": None if self.correlation_id is None else str(self.correlation_id),
            "causation_id": None if self.causation_id is None else str(self.causation_id),
            "delivered_at": self.delivered_at.astimezone(timezone.utc).isoformat(),
            "epoch": {
                "session_id": (
                    None if self.epoch_session_id is None else str(self.epoch_session_id)
                ),
                "opened": self.epoch_opened,
                "closed": self.epoch_closed,
                "fence_term": self.fence_term,
                "emergency_owner": self.emergency_owner,
            },
            "venue_control": {
                "disposition": self.venue_control,
                "detail": self.venue_detail,
            },
            "reconciliation": {
                "required": self.reconciliation_required,
                "status": self.reconciliation_status,
                "result": self.reconciliation_result,
            },
            "operator_acknowledgement_pending": self.operator_acknowledgement_pending,
            "new_risk_permitted": self.new_risk_permitted,
            "safety_transitions": list(self.safety_transitions),
            "journaled_event_ids": [str(event_id) for event_id in self.journaled_event_ids],
            "safety_state_source": self.safety_state_source,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class BreakGlassStatus:
    """A read-only view of the operational break-glass posture (no writes, no repairs)."""

    safety_mode: str
    kill_switch_engaged: bool
    permits_live_execution: bool
    new_risk_permitted: bool
    safety_state_source: str
    safety_state_detail: str
    forced_fail_closed: bool
    ownership_owner: str | None
    ownership_kind: str | None
    ownership_term: int | None
    epoch_open: bool
    epoch_session_id: UUID | None
    epoch_provenance: str | None
    epoch_escalation_path: tuple[str, ...]
    epoch_actions: tuple[str, ...]
    reconciliation_status: str
    reconciliation_reason_code: str | None
    operator_acknowledgement_pending: bool
    operator_acknowledged_by: str | None
    restore_blockers: tuple[str, ...]
    recorded_commands: int
    last_command_id: UUID | None

    def to_record(self) -> dict[str, Any]:
        """A JSON-able, secret-free observability record."""
        return {
            "safety": {
                "mode": self.safety_mode,
                "kill_switch_engaged": self.kill_switch_engaged,
                "permits_live_execution": self.permits_live_execution,
                "new_risk_permitted": self.new_risk_permitted,
                "source": self.safety_state_source,
                "detail": self.safety_state_detail,
                "forced_fail_closed": self.forced_fail_closed,
            },
            "ownership": {
                "owner": self.ownership_owner,
                "kind": self.ownership_kind,
                "fence_term": self.ownership_term,
            },
            "epoch": {
                "open": self.epoch_open,
                "session_id": (
                    None if self.epoch_session_id is None else str(self.epoch_session_id)
                ),
                "provenance": self.epoch_provenance,
                "escalation_path": list(self.epoch_escalation_path),
                "actions": list(self.epoch_actions),
            },
            "reconciliation": {
                "status": self.reconciliation_status,
                "reason_code": self.reconciliation_reason_code,
            },
            "operator_acknowledgement_pending": self.operator_acknowledgement_pending,
            "operator_acknowledged_by": self.operator_acknowledged_by,
            "restore_blockers": list(self.restore_blockers),
            "recorded_commands": self.recorded_commands,
            "last_command_id": None if self.last_command_id is None else str(self.last_command_id),
        }


class BreakGlassControl:
    """The single operational break-glass ingress."""

    def __init__(
        self,
        *,
        journal: EventJournal,
        state_dir: str | Path,
        venue: EmergencyVenuePort | None = None,
        reconciliation_policy: ReconciliationPolicy | None = None,
        local_state_provider: Callable[[], Sequence[LocalOrderState]] | None = None,
        venue_state_provider: Callable[[], Sequence[VenueOrderReport]] | None = None,
        clock: Callable[[], datetime] | None = None,
        producer: str = BREAK_GLASS_PRODUCER,
    ) -> None:
        if journal is None:
            raise BreakGlassConfigError("the break-glass control requires the event journal.")
        if not str(state_dir):
            raise BreakGlassConfigError("the break-glass control requires a state directory.")
        self._journal = journal
        self._state_dir = Path(state_dir)
        self._venue = venue if venue is not None else PolicyOnlyVenueControl()
        self._reconciliation_policy = reconciliation_policy
        self._local_state_provider = local_state_provider
        self._venue_state_provider = venue_state_provider
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._producer = producer
        self._safety_store = SafetyStateStore(
            break_glass_state_path(self._state_dir, "safety_state.json")
        )
        self._ownership_store = OwnershipStore(
            break_glass_state_path(self._state_dir, "ownership.json")
        )

    # --- exposes (tests, CLI, operator tooling) -------------------------------

    @property
    def journal(self) -> EventJournal:
        return self._journal

    @property
    def safety_store(self) -> SafetyStateStore:
        return self._safety_store

    @property
    def ownership_store(self) -> OwnershipStore:
        return self._ownership_store

    @property
    def venue(self) -> EmergencyVenuePort:
        return self._venue

    # --- reads ----------------------------------------------------------------

    def load_safety_state(self) -> SafetyStateLoad:
        return self._safety_store.load(self._journal)

    def load_ledger(self) -> BreakGlassLedger:
        return derive_ledger(self._journal)

    def load_epoch(self) -> BreakGlassEpoch | None:
        return self._effective_epoch(self.load_safety_state().controller, self.load_ledger())

    def normal_writer_guard(self) -> WriterGuard:
        """The lease a normal writer must hold; a stale token is refused when it writes."""
        return WriterGuard(self._ownership_store, self._ownership_store.require_fence())

    def claim_normal_writer(
        self, *, owner_id: str, reason_code: str = "runtime_started"
    ) -> WriterGuard:
        """The runtime's explicit startup claim of the normal writer lease (a fresh fencing term).

        Refused while a break-glass epoch is open: after an emergency takeover the normal runtime may
        not simply help itself to the lease again.
        """
        with self._ownership_store.exclusive():
            epoch = self.load_epoch()
            if epoch is not None and epoch.is_open:
                raise BreakGlassActiveError(
                    f"a break-glass epoch is open ({epoch.session_id}); the normal writer may not claim "
                    "the trading-state lease until an operator restore completes."
                )
            record = self._ownership_store.takeover(
                owner_id=owner_id,
                owner_kind=OwnerKind.NORMAL,
                acquired_at=self._now(),
                reason_code=reason_code,
            )
            return WriterGuard(self._ownership_store, record.fence)

    def status(self) -> BreakGlassStatus:
        """Read-only posture. Never writes, never mutates, never silently repairs anything."""
        load = self._safety_store.load(self._journal)
        controller = load.controller
        ledger = derive_ledger(self._journal)
        epoch = self._effective_epoch(controller, ledger)
        ownership = self._read_ownership()
        acknowledgement = None if epoch is None else epoch.acknowledgement
        return BreakGlassStatus(
            safety_mode=controller.mode.value,
            kill_switch_engaged=controller.kill_switch_engaged,
            permits_live_execution=controller.permits_live_execution(),
            new_risk_permitted=self._new_risk_permitted(controller, epoch, ownership),
            safety_state_source=load.source,
            safety_state_detail=load.detail,
            forced_fail_closed=load.forced_fail_closed,
            ownership_owner=None if ownership is None else ownership.owner_id,
            ownership_kind=None if ownership is None else ownership.owner_kind.value,
            ownership_term=None if ownership is None else ownership.term,
            epoch_open=bool(epoch is not None and epoch.is_open),
            epoch_session_id=None if epoch is None else epoch.session_id,
            epoch_provenance=None if epoch is None else epoch.provenance,
            epoch_escalation_path=() if epoch is None else epoch.escalation_path,
            epoch_actions=() if epoch is None else epoch.actions,
            reconciliation_status="not_required" if epoch is None else epoch.reconciliation_status,
            reconciliation_reason_code=None if epoch is None else epoch.reconciliation_reason_code,
            operator_acknowledgement_pending=bool(epoch is not None and acknowledgement is None),
            operator_acknowledged_by=(
                None if acknowledgement is None else acknowledgement.operator_label
            ),
            restore_blockers=() if epoch is None else epoch.restore_blockers,
            recorded_commands=len(ledger.commands),
            last_command_id=(None if not ledger.records else ledger.records[-1].command_id),
        )

    def risk_limits(self, limits: RiskLimits) -> RiskLimits:
        """Derive the Phase 7 risk limits from the persisted safety mode (no parallel rule set)."""
        return risk_limits_for_safety(self._safety_store.load(self._journal).controller, limits)

    # --- the AI/strategy boundary ---------------------------------------------

    def runtime_request_mode_change(self, *, source: str, mode: SafetyMode) -> None:
        """Refuse any non-operator (runtime/strategy/AI) safety-state change.

        This is the mechanical statement of "AI/strategy decisions cannot re-enable trading": the
        only ingress that may change the safety state is a validated operator command, and while a
        break-glass epoch is open every runtime-sourced change is refused outright.
        """
        if source in ("operator", "break_glass", "cli"):
            raise BreakGlassActiveError(
                "runtime_request_mode_change is for runtime/strategy sources only; the operator "
                "path is the audited operator command channel."
            )
        controller = self._safety_store.load(self._journal).controller
        epoch = self._effective_epoch(controller, derive_ledger(self._journal))
        if epoch is not None and epoch.is_open:
            raise BreakGlassActiveError(
                f"a break-glass epoch is open ({epoch.session_id}); the {source!r}-sourced request "
                f"to move the safety state to {mode.value!r} is refused. Only an explicit, "
                "reconciled and acknowledged operator restore may return the system to normal."
            )
        raise BreakGlassActiveError(
            f"{source!r} may not change the operational safety state directly; use the audited "
            "operator break-glass control channel."
        )

    # --- the single ingress ---------------------------------------------------

    def handle(self, command: OperatorCommand) -> OperatorCommandOutcome:
        """Serialize the complete operator transaction against normal submission."""
        with self._ownership_store.exclusive():
            return self._handle_locked(command)

    def _handle_locked(self, command: OperatorCommand) -> OperatorCommandOutcome:
        if not isinstance(command, OperatorCommand):
            raise BreakGlassConfigError("handle() expects a validated OperatorCommand.")
        delivered_at = self._now()
        ledger = derive_ledger(self._journal)
        load = self._safety_store.load(self._journal)
        controller = load.controller

        prior = ledger.command(command.command_id)
        if prior is not None:
            return self._duplicate_outcome(command, prior, delivered_at, load, controller, ledger)

        epoch = self._effective_epoch(controller, ledger)
        if command.command_type in BREAK_GLASS_ACTIONS:
            return self._handle_action(command, controller, epoch, load, delivered_at)
        if command.command_type is OperatorCommandType.RECONCILE:
            return self._handle_reconcile(command, controller, epoch, load, delivered_at)
        if command.command_type is OperatorCommandType.ACKNOWLEDGE:
            return self._handle_acknowledge(command, controller, epoch, load, delivered_at)
        if command.command_type is OperatorCommandType.RESTORE_NORMAL:
            return self._handle_restore(command, controller, epoch, load, delivered_at)
        raise BreakGlassConfigError(
            f"unknown operator command type {command.command_type!r}; refusing to guess."
        )

    # --- break-glass actions --------------------------------------------------

    def _handle_action(
        self,
        command: OperatorCommand,
        controller: SafetyController,
        epoch: BreakGlassEpoch | None,
        load: SafetyStateLoad,
        delivered_at: datetime,
    ) -> OperatorCommandOutcome:
        target = ACTION_TARGET_MODES[command.command_type]
        previous_mode = controller.mode
        open_epoch = epoch if epoch is not None and epoch.is_open else None

        if target.severity < previous_mode.severity:
            return self._reject(
                command,
                controller,
                load,
                delivered_at,
                reason_code="downgrade_refused_monotonic_escalation",
                note=(
                    f"{command.command_type.value} would move the safety state from "
                    f"{previous_mode.value} to {target.value}, which is less restrictive; the "
                    "break-glass path never downgrades safety."
                ),
            )

        if command.command_type is OperatorCommandType.REDUCE_ONLY:
            if open_epoch is not None:
                return self._reject(
                    command,
                    controller,
                    load,
                    delivered_at,
                    reason_code="reduce_only_refused_break_glass_epoch_open",
                    note=(
                        "reduce-only cannot replace or weaken an active emergency epoch; "
                        "reconciliation, acknowledgement and explicit restore remain mandatory."
                    ),
                )
            ownership, fence_term, ownership_diagnostic = self._ownership_observation()
            if ownership is not None and ownership.owner_kind is OwnerKind.EMERGENCY:
                return self._reject(
                    command,
                    controller,
                    load,
                    delivered_at,
                    reason_code="reduce_only_refused_emergency_owner",
                    note=(
                        "reduce-only cannot restore trading authority while the emergency owner "
                        "holds the writer lease."
                    ),
                    ownership_observation=(ownership, fence_term, ownership_diagnostic),
                )
            correlation_id = command.correlation_id or command.command_id
            transitions = self._apply_posture(
                controller,
                target,
                command=command,
                engage_kill_switch=False,
            )
            recorded = self._append_records(
                command,
                controller,
                previous_mode=previous_mode,
                resulting_mode=controller.mode,
                disposition="accepted",
                reason_code=self._action_reason_code(command, controller, None),
                correlation_id=correlation_id,
                venue_control="not_requested",
                reconciliation_status="not_required",
                epoch_opened=False,
                epoch_closed=False,
                fence_term=fence_term,
                occurred_at=delivered_at,
                transitions=transitions,
            )
            self._persist_state_cache(controller)
            return OperatorCommandOutcome(
                command_id=command.command_id,
                command_type=command.command_type.value,
                disposition="accepted",
                reason_code=self._action_reason_code(command, controller, None),
                previous_safety_state=previous_mode.value,
                resulting_safety_state=controller.mode.value,
                correlation_id=correlation_id,
                causation_id=command.causation_id,
                delivered_at=delivered_at,
                reconciliation_required=False,
                reconciliation_status="not_required",
                operator_acknowledgement_pending=False,
                new_risk_permitted=False,
                venue_control="not_requested",
                fence_term=fence_term,
                safety_transitions=tuple(
                    f"{item.previous_mode.value}->{item.mode.value}" for item in transitions
                ),
                journaled_event_ids=tuple(recorded),
                notes=(
                    "reduce-only is an operator-audited policy restriction; it preserves the "
                    "current normal writer lease and delegates reduction proof to Hard Risk.",
                    load.detail,
                    *((ownership_diagnostic,) if ownership_diagnostic is not None else ()),
                ),
                safety_state_source=load.source,
            )

        reanchor = open_epoch is None or open_epoch.provenance == "provenance_missing"
        if reanchor:
            session_id = command.correlation_id or command.command_id
        else:
            session_id = command.correlation_id or open_epoch.session_id

        transitions = self._apply_posture(
            controller,
            target,
            command=command,
            engage_kill_switch=command.command_type in KILL_SWITCH_ACTIONS,
        )
        venue_outcome: VenueControlOutcome | None = None
        notes: list[str] = []
        if command.command_type is OperatorCommandType.CANCEL_OPEN:
            venue_outcome = self._venue.cancel_open_orders(
                correlation_id=session_id,
                occurred_at=delivered_at,
                safety=controller,
            )
            if venue_outcome.disposition == "ambiguous":
                notes.append(
                    "the venue cancel outcome is unknown (UNKNOWN_PENDING_RECONCILIATION); it was "
                    "NOT retried and reconciliation is mandatory"
                )
            if venue_outcome.disposition in ("ambiguous", "unavailable", "rejected"):
                transitions.extend(
                    self._escalate_to_recovery_required(controller, command, venue_outcome)
                )
                reconciliation_reason = (
                    "UNKNOWN_PENDING_RECONCILIATION"
                    if venue_outcome.disposition == "ambiguous"
                    else "venue_state_unconfirmed"
                )
                notes.append(
                    "the venue-side emergency action could not be confirmed, so the epoch escalated "
                    "through degraded into recovery_required (fail-closed progression)"
                )
            elif venue_outcome.disposition == "confirmed":
                reconciliation_reason = "reconciliation_required"
                notes.append("the venue confirmed every cancelled order through the safety path")
            else:
                reconciliation_reason = "reconciliation_required"
                notes.append(
                    "no venue-side control is attached; the deterministic policy mode alone was "
                    "applied and cancelling open orders remains an operator action"
                )
        else:
            reconciliation_reason = "reconciliation_required"

        fence_term = self._ownership_store.next_term()
        # Fencing takeover happens BEFORE the journal append on purpose: if the process dies in
        # between, the normal writer is already locked out (fail closed) even though the command was
        # not recorded yet, and the operator can simply re-deliver the same command_id.
        self._ownership_store.takeover(
            owner_id=f"emergency:{command.operator_label}",
            owner_kind=OwnerKind.EMERGENCY,
            acquired_at=delivered_at,
            reason_code=f"break_glass:{command.command_type.value}",
            session_id=session_id,
        )
        recorded = self._append_records(
            command,
            controller,
            previous_mode=previous_mode,
            resulting_mode=controller.mode,
            disposition="accepted",
            reason_code=self._action_reason_code(command, controller, venue_outcome),
            correlation_id=session_id,
            venue_control="not_requested" if venue_outcome is None else venue_outcome.disposition,
            reconciliation_status="pending",
            epoch_opened=reanchor,
            epoch_closed=False,
            fence_term=fence_term,
            occurred_at=delivered_at,
            transitions=transitions,
        )
        self._persist_state_cache(controller)
        resulting_epoch = self._reload_epoch(controller)
        return OperatorCommandOutcome(
            command_id=command.command_id,
            command_type=command.command_type.value,
            disposition="accepted",
            reason_code=self._action_reason_code(command, controller, venue_outcome),
            previous_safety_state=previous_mode.value,
            resulting_safety_state=controller.mode.value,
            correlation_id=session_id,
            causation_id=command.causation_id,
            delivered_at=delivered_at,
            epoch_opened=reanchor,
            epoch_session_id=None if resulting_epoch is None else resulting_epoch.session_id,
            reconciliation_required=True,
            reconciliation_status="pending",
            reconciliation_result=reconciliation_reason,
            operator_acknowledgement_pending=True,
            new_risk_permitted=False,
            venue_control="not_requested" if venue_outcome is None else venue_outcome.disposition,
            venue_detail=None if venue_outcome is None else venue_outcome.detail,
            fence_term=fence_term,
            emergency_owner=f"emergency:{command.operator_label}",
            safety_transitions=tuple(
                f"{item.previous_mode.value}->{item.mode.value}" for item in transitions
            ),
            journaled_event_ids=tuple(recorded),
            notes=tuple(notes) + (load.detail,),
            safety_state_source=load.source,
        )

    # --- lifecycle commands ---------------------------------------------------

    def _handle_reconcile(
        self,
        command: OperatorCommand,
        controller: SafetyController,
        epoch: BreakGlassEpoch | None,
        load: SafetyStateLoad,
        delivered_at: datetime,
    ) -> OperatorCommandOutcome:
        if epoch is None or not epoch.is_open:
            return self._reject(
                command,
                controller,
                load,
                delivered_at,
                reason_code="no_break_glass_epoch_open",
                note="there is no open break-glass epoch to reconcile.",
            )
        report, reconciliation_note = self._run_reconciliation(epoch)
        status = "resolved" if report.is_fully_resolved else "pending"
        reason_code = (
            "reconciliation_resolved" if status == "resolved" else "UNKNOWN_PENDING_RECONCILIATION"
        )
        ownership, fence_term, ownership_diagnostic = self._ownership_observation()
        event_ids = self._append_records(
            command,
            controller,
            previous_mode=controller.mode,
            resulting_mode=controller.mode,
            disposition="accepted",
            reason_code=reason_code,
            correlation_id=epoch.session_id,
            venue_control="not_requested",
            reconciliation_status=status,
            epoch_opened=False,
            epoch_closed=False,
            fence_term=fence_term,
            occurred_at=delivered_at,
            transitions=(),
        )
        resulting_epoch = self._reload_epoch(controller)
        return OperatorCommandOutcome(
            command_id=command.command_id,
            command_type=command.command_type.value,
            disposition="accepted",
            reason_code=reason_code,
            previous_safety_state=controller.mode.value,
            resulting_safety_state=controller.mode.value,
            correlation_id=epoch.session_id,
            causation_id=command.causation_id,
            delivered_at=delivered_at,
            epoch_session_id=epoch.session_id,
            reconciliation_required=True,
            reconciliation_status=status,
            reconciliation_result=reconciliation_note,
            operator_acknowledgement_pending=(
                resulting_epoch is None or not resulting_epoch.acknowledgement_satisfied
            ),
            new_risk_permitted=False,
            fence_term=fence_term,
            journaled_event_ids=tuple(event_ids),
            notes=(
                f"reconciliation ran through Phase 8 evidence: {reconciliation_note}",
                "unresolved orders stay UNKNOWN_PENDING_RECONCILIATION and are never retried",
                *((ownership_diagnostic,) if ownership_diagnostic is not None else ()),
            ),
            safety_state_source=load.source,
        )

    def _handle_acknowledge(
        self,
        command: OperatorCommand,
        controller: SafetyController,
        epoch: BreakGlassEpoch | None,
        load: SafetyStateLoad,
        delivered_at: datetime,
    ) -> OperatorCommandOutcome:
        if epoch is None or not epoch.is_open:
            return self._reject(
                command,
                controller,
                load,
                delivered_at,
                reason_code="no_break_glass_epoch_open",
                note="there is no open break-glass epoch to acknowledge.",
            )
        if not epoch.reconciliation_satisfied:
            return self._reject(
                command,
                controller,
                load,
                delivered_at,
                reason_code="acknowledgement_denied_reconciliation_pending",
                note=(
                    "an operator acknowledgement is only meaningful after a successful "
                    "reconciliation; this epoch is still pending reconciliation."
                ),
            )
        reason_code = (
            "operator_acknowledgement_replaced"
            if epoch.acknowledgement_satisfied
            else "operator_acknowledgement_recorded"
        )
        ownership, fence_term, ownership_diagnostic = self._ownership_observation()
        event_ids = self._append_records(
            command,
            controller,
            previous_mode=controller.mode,
            resulting_mode=controller.mode,
            disposition="accepted",
            reason_code=reason_code,
            correlation_id=epoch.session_id,
            venue_control="not_requested",
            reconciliation_status=epoch.reconciliation_status,
            epoch_opened=False,
            epoch_closed=False,
            fence_term=fence_term,
            occurred_at=delivered_at,
            transitions=(),
        )
        return OperatorCommandOutcome(
            command_id=command.command_id,
            command_type=command.command_type.value,
            disposition="accepted",
            reason_code=reason_code,
            previous_safety_state=controller.mode.value,
            resulting_safety_state=controller.mode.value,
            correlation_id=epoch.session_id,
            causation_id=command.causation_id,
            delivered_at=delivered_at,
            epoch_session_id=epoch.session_id,
            reconciliation_required=True,
            reconciliation_status=epoch.reconciliation_status,
            reconciliation_result=epoch.reconciliation_reason_code,
            operator_acknowledgement_pending=False,
            new_risk_permitted=False,
            fence_term=fence_term,
            journaled_event_ids=tuple(event_ids),
            notes=(
                "the acknowledgement is attributed to the operator label in the command record",
                *((ownership_diagnostic,) if ownership_diagnostic is not None else ()),
            ),
            safety_state_source=load.source,
        )

    def _handle_restore(
        self,
        command: OperatorCommand,
        controller: SafetyController,
        epoch: BreakGlassEpoch | None,
        load: SafetyStateLoad,
        delivered_at: datetime,
    ) -> OperatorCommandOutcome:
        if (
            (epoch is None or not epoch.is_open)
            and controller.mode is SafetyMode.REDUCE_ONLY
            and not controller.kill_switch_engaged
        ):
            ownership, fence_term, ownership_diagnostic = self._ownership_observation()
            if ownership_diagnostic == "ownership unreadable":
                return self._reject(
                    command,
                    controller,
                    load,
                    delivered_at,
                    reason_code="restore_denied_ownership_unreadable",
                    note=(
                        "normal cannot be restored from reduce-only while writer ownership "
                        "cannot be read authoritatively."
                    ),
                    ownership_observation=(ownership, fence_term, ownership_diagnostic),
                )
            if ownership is not None and ownership.owner_kind is OwnerKind.EMERGENCY:
                return self._reject(
                    command,
                    controller,
                    load,
                    delivered_at,
                    reason_code="restore_denied_emergency_owner",
                    note=(
                        "normal cannot be restored from reduce-only while the emergency owner "
                        "holds the writer lease."
                    ),
                    ownership_observation=(ownership, fence_term, ownership_diagnostic),
                )
            correlation_id = command.correlation_id or command.command_id
            transitions = [
                controller.set_mode(
                    SafetyMode.NORMAL,
                    reason_code="operator_restore_normal",
                    reason=(
                        f"operator {command.operator_label} restored normal from the "
                        "reduce-only policy posture"
                    ),
                    trigger="operator",
                )
            ]
            event_ids = self._append_records(
                command,
                controller,
                previous_mode=SafetyMode.REDUCE_ONLY,
                resulting_mode=controller.mode,
                disposition="accepted",
                reason_code="normal_restored_by_operator",
                correlation_id=correlation_id,
                venue_control="not_requested",
                reconciliation_status="not_required",
                epoch_opened=False,
                epoch_closed=False,
                fence_term=fence_term,
                occurred_at=delivered_at,
                transitions=tuple(transitions),
            )
            self._persist_state_cache(controller)
            return OperatorCommandOutcome(
                command_id=command.command_id,
                command_type=command.command_type.value,
                disposition="accepted",
                reason_code="normal_restored_by_operator",
                previous_safety_state=SafetyMode.REDUCE_ONLY.value,
                resulting_safety_state=controller.mode.value,
                correlation_id=correlation_id,
                causation_id=command.causation_id,
                delivered_at=delivered_at,
                reconciliation_required=False,
                reconciliation_status="not_required",
                operator_acknowledgement_pending=False,
                new_risk_permitted=self._new_risk_permitted(
                    controller, None, ownership
                ),
                fence_term=fence_term,
                safety_transitions=tuple(
                    f"{item.previous_mode.value}->{item.mode.value}"
                    for item in transitions
                ),
                journaled_event_ids=tuple(event_ids),
                notes=(
                    "normal was restored by an explicit operator command from reduce-only; "
                    "no emergency epoch or ownership takeover was involved.",
                    load.detail,
                    *((ownership_diagnostic,) if ownership_diagnostic is not None else ()),
                ),
                safety_state_source=load.source,
            )

        if epoch is None or not epoch.is_open:
            return self._reject(
                command,
                controller,
                load,
                delivered_at,
                reason_code="no_break_glass_epoch_open",
                note="there is no open break-glass epoch; normal is never restored by fiat.",
            )
        blockers = epoch.restore_blockers
        if "reconciliation_pending" in blockers:
            return self._reject(
                command,
                controller,
                load,
                delivered_at,
                reason_code="restore_denied_reconciliation_pending",
                note=(
                    "restoring normal is refused: reconciliation is still mandatory and has not "
                    "completed successfully."
                ),
            )
        if "operator_acknowledgement_missing" in blockers:
            return self._reject(
                command,
                controller,
                load,
                delivered_at,
                reason_code="restore_denied_acknowledgement_missing",
                note=(
                    "restoring normal is refused: an explicit operator acknowledgement of the "
                    "completed reconciliation is still missing."
                ),
            )

        # Fail-closed ordering: the mode moves to normal FIRST (a crash here leaves `normal` with
        # the kill switch still engaged, which continues to refuse new risk), then it is released.
        transitions = [
            controller.set_mode(
                SafetyMode.NORMAL,
                reason_code="operator_restore_normal",
                reason=f"operator {command.operator_label} restored normal after reconciliation and "
                "acknowledgement",
                trigger="operator",
            )
        ]
        if controller.kill_switch_engaged:
            transitions.append(
                controller.release_kill_switch(
                    reason_code="operator_restore_normal",
                    reason="kill switch released by the explicit operator restore",
                )
            )
        fence_term = self._ownership_store.next_term()
        self._ownership_store.takeover(
            owner_id=f"normal:{command.operator_label}",
            owner_kind=OwnerKind.NORMAL,
            acquired_at=delivered_at,
            reason_code="operator_restore_normal",
            session_id=epoch.session_id,
        )
        event_ids = self._append_records(
            command,
            controller,
            previous_mode=SafetyMode(epoch.current_mode),
            resulting_mode=controller.mode,
            disposition="accepted",
            reason_code="normal_restored_by_operator",
            correlation_id=epoch.session_id,
            venue_control="not_requested",
            reconciliation_status=epoch.reconciliation_status,
            epoch_opened=False,
            epoch_closed=True,
            fence_term=fence_term,
            occurred_at=delivered_at,
            transitions=tuple(transitions),
        )
        self._persist_state_cache(controller)
        return OperatorCommandOutcome(
            command_id=command.command_id,
            command_type=command.command_type.value,
            disposition="accepted",
            reason_code="normal_restored_by_operator",
            previous_safety_state=epoch.current_mode,
            resulting_safety_state=controller.mode.value,
            correlation_id=epoch.session_id,
            causation_id=command.causation_id,
            delivered_at=delivered_at,
            epoch_closed=True,
            epoch_session_id=epoch.session_id,
            reconciliation_required=True,
            reconciliation_status=epoch.reconciliation_status,
            reconciliation_result=epoch.reconciliation_reason_code,
            operator_acknowledgement_pending=False,
            new_risk_permitted=self._new_risk_permitted(
                controller, None, self._read_ownership()
            ),
            fence_term=fence_term,
            safety_transitions=tuple(
                f"{item.previous_mode.value}->{item.mode.value}" for item in transitions
            ),
            journaled_event_ids=tuple(event_ids),
            notes=(
                "normal was restored only by this explicit, reconciled and acknowledged operator "
                "command; the AI/strategy path can never do this",
            ),
            safety_state_source=load.source,
        )

    # --- internals ------------------------------------------------------------

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise BreakGlassConfigError("the break-glass clock must return timezone-aware time.")
        return now.astimezone(timezone.utc)

    def _effective_epoch(
        self, controller: SafetyController, ledger: BreakGlassLedger
    ) -> BreakGlassEpoch | None:
        """The epoch the control must honour, including a fail-closed synthetic one."""
        if ledger.open_epoch is not None:
            return ledger.open_epoch
        if ledger.epoch is not None:
            return ledger.epoch
        if controller.mode in (SafetyMode.DEGRADED, SafetyMode.RECOVERY_REQUIRED):
            return synthesise_epoch_from_safety_state(
                controller.mode, now=self._now(), operator_label="operator_unknown"
            )
        return None

    def _apply_posture(
        self,
        controller: SafetyController,
        target: SafetyMode,
        *,
        command: OperatorCommand,
        engage_kill_switch: bool,
    ) -> list[SafetyTransition]:
        """Apply the deterministic posture change (kill switch first, then the requested mode)."""
        transitions: list[SafetyTransition] = []
        if engage_kill_switch and not (
            controller.kill_switch_engaged and controller.mode is target
        ):
            transitions.append(
                controller.engage_kill_switch(
                    reason_code=f"breakglass:{command.command_type.value}",
                    reason=f"break-glass {command.command_type.value}: {command.reason}",
                    trigger="break_glass",
                )
            )
        if controller.mode is not target:
            transitions.append(
                controller.set_mode(
                    target,
                    reason_code=f"breakglass:{command.command_type.value}",
                    reason=f"break-glass {command.command_type.value}: {command.reason}",
                    trigger="break_glass",
                )
            )
        if not transitions:
            transitions.append(
                controller.set_mode(
                    target,
                    reason_code=f"breakglass:{command.command_type.value}",
                    reason=f"break-glass {command.command_type.value} (already in the requested "
                    "posture)",
                    trigger="break_glass",
                )
            )
        return transitions

    def _escalate_to_recovery_required(
        self,
        controller: SafetyController,
        command: OperatorCommand,
        venue_outcome: VenueControlOutcome,
    ) -> list[SafetyTransition]:
        """Force the canonical fail-closed progression ``degraded`` -> ``recovery_required``."""
        transitions: list[SafetyTransition] = []
        if controller.mode is not SafetyMode.DEGRADED:
            transitions.append(
                controller.set_mode(
                    SafetyMode.DEGRADED,
                    reason_code="breakglass_venue_control_unconfirmed",
                    reason=f"break-glass {command.command_type.value} could not be confirmed at the "
                    f"venue ({venue_outcome.disposition})",
                    trigger="break_glass",
                )
            )
        transitions.append(
            controller.set_mode(
                SafetyMode.RECOVERY_REQUIRED,
                reason_code="breakglass_recovery_required",
                reason=(
                    "break-glass venue control requires reconciliation "
                    f"({venue_outcome.reason_code})"
                ),
                trigger="break_glass",
            )
        )
        return transitions

    def _action_reason_code(
        self,
        command: OperatorCommand,
        controller: SafetyController,
        venue_outcome: VenueControlOutcome | None,
    ) -> str:
        if venue_outcome is not None and venue_outcome.ambiguous:
            return "break_glass_engaged_UNKNOWN_PENDING_RECONCILIATION"
        if controller.mode is SafetyMode.RECOVERY_REQUIRED:
            return "break_glass_engaged_recovery_required"
        return f"break_glass_engaged_{command.command_type.value}"

    def _ownership_observation(self) -> tuple[OwnershipRecord | None, int, str | None]:
        """Read ownership once and preserve why no usable fence record was available."""
        try:
            ownership = self._ownership_store.read()
        except OwnershipStateError:
            return None, 0, "ownership unreadable"
        if ownership is None:
            return None, 0, "ownership absent"
        return ownership, ownership.term, None

    def _current_term(self) -> int:
        return self._ownership_observation()[1]

    def _read_ownership(self) -> OwnershipRecord | None:
        return self._ownership_observation()[0]

    def _new_risk_permitted(
        self,
        controller: SafetyController,
        epoch: BreakGlassEpoch | None,
        ownership: OwnershipRecord | None,
    ) -> bool:
        if epoch is not None and epoch.is_open:
            return False
        if not controller.allow_new_risk():
            return False
        if ownership is None or ownership.owner_kind is not OwnerKind.NORMAL:
            return False
        return True

    def _run_reconciliation(self, epoch: BreakGlassEpoch):
        """Run the Phase 8 reconciliation through the injected evidence providers."""
        if self._reconciliation_policy is None:
            raise BreakGlassConfigError(
                "no reconciliation policy is configured; reconciliation cannot be invented."
            )
        local_states = self._local_states()
        venue_reports = list(
            self._venue_state_provider() if self._venue_state_provider is not None else ()
        )
        venue_reports.extend(self._venue.venue_reports())
        engine = ReconciliationEngine(self._reconciliation_policy)
        source = StaticVenueStateSource(entries=tuple(venue_reports), name="breakglass.venue")
        recovery = RestartRecovery(
            engine=engine,
            journal=self._journal,
            venue_sources=[source],
            producer=BREAK_GLASS_PRODUCER,
        )
        run = recovery.run(
            now=self._now(),
            local_states=local_states,
            correlation_id=epoch.session_id,
        )
        note = (
            "reconciliation_confirmed_by_evidence"
            if run.report.is_fully_resolved
            else "reconciliation_left_orders_unresolved"
        )
        return run.report, note

    def _local_states(self) -> tuple[LocalOrderState, ...]:
        if self._local_state_provider is not None:
            return tuple(self._local_state_provider())
        reconstructor, _ = reconstruct_order_states(self._journal)
        return reconstructor.states()

    def _append_records(
        self,
        command: OperatorCommand,
        controller: SafetyController,
        *,
        previous_mode: SafetyMode,
        resulting_mode: SafetyMode,
        disposition: str,
        reason_code: str,
        correlation_id: UUID,
        venue_control: str,
        reconciliation_status: str,
        epoch_opened: bool,
        epoch_closed: bool,
        fence_term: int,
        occurred_at: datetime,
        transitions: Sequence[SafetyTransition],
    ) -> list[UUID]:
        """Journal the safety transitions and the command record in ONE atomic transaction."""
        del controller
        # Deterministic intra-command ordering: several operator commands may legitimately share one
        # request timestamp (and therefore one journal `occurred_at`), so every event in this batch
        # carries an explicit, strictly increasing sequence number. Without it the journal's
        # tie-break would be the random event id and replay would not be reproducible.
        sequence_base = (len(derive_ledger(self._journal).commands) + 1) * 100
        events: list[Any] = []
        for index, transition in enumerate(transitions, start=1):
            # The Phase 9 contract and its "no event for an unchanged transition" rule are reused
            # unchanged; only the ordering hint is Phase 13 specific, so the event is built here.
            if not transition.changed:
                continue
            events.append(
                SafetyStateChanged(
                    event_id=uuid4(),
                    occurred_at=occurred_at.astimezone(timezone.utc),
                    correlation_id=correlation_id,
                    producer=self._producer,
                    sequence_number=sequence_base + index,
                    payload=SafetyStateChangePayload(
                        mode=transition.mode.value,
                        previous_mode=transition.previous_mode.value,
                        reason_code=transition.reason_code,
                        reason=transition.reason,
                        trigger=transition.trigger,
                        kill_switch_engaged=transition.kill_switch_engaged,
                    ),
                )
            )
        events.append(
            OperatorCommandRecorded(
                event_id=uuid4(),
                occurred_at=occurred_at.astimezone(timezone.utc),
                correlation_id=correlation_id,
                causation_id=command.causation_id,
                producer=self._producer,
                sequence_number=sequence_base + len(transitions) + 1,
                payload=OperatorCommandRecordPayload(
                    command_id=command.command_id,
                    command_type=command.command_type.value,
                    requested_at=command.requested_at,
                    operator_label=command.operator_label,
                    operator_identity=command.operator_identity,
                    reason=command.reason,
                    source=command.source,
                    previous_safety_state=previous_mode.value,
                    resulting_safety_state=resulting_mode.value,
                    disposition=disposition,
                    reason_code=reason_code,
                    command_digest=command.digest,
                    mode_path=_mode_path(previous_mode, transitions),
                    venue_control=venue_control,
                    reconciliation_status=reconciliation_status,
                    epoch_opened=epoch_opened,
                    epoch_closed=epoch_closed,
                    fence_term=fence_term,
                ),
            )
        )
        self._journal.append_batch(events)
        return [event.event_id for event in events]

    def _persist_state_cache(self, controller: SafetyController) -> None:
        """Cache the safety state AFTER the authoritative journal append (journal first, always).

        The epoch is deliberately NOT cached: it is derived from the journal on every load, so a
        stale cache can never contradict the authoritative command history.
        """
        self._safety_store.save(controller)

    def _reload_epoch(self, controller: SafetyController) -> BreakGlassEpoch | None:
        return self._effective_epoch(controller, derive_ledger(self._journal))

    def _reject(
        self,
        command: OperatorCommand,
        controller: SafetyController,
        load: SafetyStateLoad,
        delivered_at: datetime,
        *,
        reason_code: str,
        note: str,
        ownership_observation: tuple[OwnershipRecord | None, int, str | None] | None = None,
    ) -> OperatorCommandOutcome:
        """Record a refused command. A refusal never touches the safety state."""
        epoch = self._effective_epoch(controller, derive_ledger(self._journal))
        correlation_id = command.correlation_id or (
            command.command_id if epoch is None else epoch.session_id
        )
        if ownership_observation is None:
            ownership, fence_term, ownership_diagnostic = self._ownership_observation()
        else:
            ownership, fence_term, ownership_diagnostic = ownership_observation
        event_ids = self._append_records(
            command,
            controller,
            previous_mode=controller.mode,
            resulting_mode=controller.mode,
            disposition="rejected",
            reason_code=reason_code,
            correlation_id=correlation_id,
            venue_control="not_requested",
            reconciliation_status="not_required" if epoch is None else epoch.reconciliation_status,
            epoch_opened=False,
            epoch_closed=False,
            fence_term=fence_term,
            occurred_at=delivered_at,
            transitions=(),
        )
        notes = [note]
        if ownership_diagnostic is not None:
            notes.append(ownership_diagnostic)
        notes.append("a refused command changes no safety state (fail closed)")
        return OperatorCommandOutcome(
            command_id=command.command_id,
            command_type=command.command_type.value,
            disposition="rejected",
            reason_code=reason_code,
            previous_safety_state=controller.mode.value,
            resulting_safety_state=controller.mode.value,
            correlation_id=correlation_id,
            causation_id=command.causation_id,
            delivered_at=delivered_at,
            epoch_session_id=None if epoch is None else epoch.session_id,
            reconciliation_required=bool(epoch is not None and epoch.is_open),
            reconciliation_status="not_required" if epoch is None else epoch.reconciliation_status,
            reconciliation_result=None if epoch is None else epoch.reconciliation_reason_code,
            operator_acknowledgement_pending=bool(
                epoch is not None and not epoch.acknowledgement_satisfied
            ),
            new_risk_permitted=self._new_risk_permitted(controller, epoch, ownership),
            fence_term=fence_term,
            journaled_event_ids=tuple(event_ids),
            notes=tuple(notes),
            safety_state_source=load.source,
        )

    def _duplicate_outcome(
        self,
        command: OperatorCommand,
        prior: RecordedOperatorCommand,
        delivered_at: datetime,
        load: SafetyStateLoad,
        controller: SafetyController,
        ledger: BreakGlassLedger,
    ) -> OperatorCommandOutcome:
        """Re-delivery of a recorded command_id: one logical effect, reported as a duplicate."""
        epoch = self._effective_epoch(controller, ledger)
        if prior.command_digest != command.digest:
            return OperatorCommandOutcome(
                command_id=command.command_id,
                command_type=command.command_type.value,
                disposition="rejected",
                reason_code="command_id_conflict",
                previous_safety_state=prior.resulting_safety_state,
                resulting_safety_state=prior.resulting_safety_state,
                correlation_id=prior.correlation_id,
                causation_id=command.causation_id,
                delivered_at=delivered_at,
                duplicate=True,
                reconciliation_status=prior.reconciliation_status,
                new_risk_permitted=False,
                fence_term=prior.fence_term,
                notes=(
                    "this command_id was already used for a DIFFERENT command; the original record "
                    "stays authoritative and this delivery changed nothing.",
                ),
                safety_state_source=load.source,
            )
        return OperatorCommandOutcome(
            command_id=prior.command_id,
            command_type=prior.command_type,
            disposition=prior.disposition,
            reason_code=prior.reason_code,
            previous_safety_state=prior.previous_safety_state,
            resulting_safety_state=prior.resulting_safety_state,
            correlation_id=prior.correlation_id,
            causation_id=prior.causation_id,
            delivered_at=delivered_at,
            duplicate=True,
            epoch_opened=prior.epoch_opened,
            epoch_closed=prior.epoch_closed,
            epoch_session_id=None if epoch is None else epoch.session_id,
            reconciliation_required=bool(epoch is not None and epoch.is_open),
            reconciliation_status=prior.reconciliation_status,
            reconciliation_result=None if epoch is None else epoch.reconciliation_reason_code,
            operator_acknowledgement_pending=bool(
                epoch is not None and not epoch.acknowledgement_satisfied
            ),
            new_risk_permitted=self._new_risk_permitted(controller, epoch, self._read_ownership()),
            venue_control=prior.venue_control,
            fence_term=prior.fence_term,
            journaled_event_ids=(prior.event_id,),
            notes=(
                "idempotent re-delivery: the original command record is authoritative and no second "
                "effect was produced.",
            ),
            safety_state_source=load.source,
        )
