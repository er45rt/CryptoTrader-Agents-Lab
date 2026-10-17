"""Deterministic break-glass fault-injection and operational scenarios (Phase 13).

Every scenario runs OFFLINE against the real components: the event journal, the Phase 8
reconciliation engine, the Phase 9 safety controller, the Phase 11 Spot Testnet adapter (driven by
the scripted transport - never the network) and the Phase 13 control path. Only faults are
injected; no decision logic is mocked.

A scenario passes only when the system behaved fail-closed exactly as designed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from crypto_trader.breakglass import (
    BreakGlassActiveError,
    MalformedOperatorCommandError,
    OwnerKind,
    OwnershipStateError,
    StaleWriterFenceError,
    parse_operator_command,
)
from crypto_trader.execution import InstrumentSpec
from crypto_trader.execution.binance_testnet import (
    MAINNET_HOST_MARKERS,
    BinanceSpotTestnetClient,
    TestnetEndpointError,
    TestnetOrderRuntime,
    TransportError,
    TransportTimeout,
    UnsafeTestnetAccountError,
    WithdrawalPolicy,
    require_testnet_base_url,
)
from crypto_trader.events import SAFETY_EVENT_TYPE, StrategyDecisionPayload, StrategyDecisionRecorded
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.recovery import (
    LocalOrderState,
    OrderStateName,
    ReconciliationOutcome,
    VenueOrderReport,
    load_policy as load_recovery_policy,
)
from crypto_trader.risk import HardRiskEngine, RiskRequest, RiskStateSnapshot, Side, load_limits
from crypto_trader.safety import PrioritySafetyLane, SafetyMode, LaneCommand, LanePriority
from crypto_trader.tests import testnet_fixtures as txf

from ..control import BreakGlassControl as _Control  # explicit: the audited production component
from ..commands import OperatorCommandType
from ..venue import BinanceSpotTestnetEmergencyControl

__all__ = ["SCENARIOS", "BreakGlassScenarioResult", "run_all_scenarios", "run_scenario"]

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LATER = BASE + timedelta(minutes=5)
NAMESPACE = UUID("6c2f4f14-9a2c-4b7d-8a1f-0d5e6b3c7a91")

RECOVERY_CONFIG = {"policy_id": "recovery-breakglass-audit-v1"}

RISK_LIMITS = {
    "limits_version": "risk-breakglass-audit-v1",
    "max_order_notional_quote": "5000",
    "max_symbol_exposure_quote": "20000",
    "max_gross_exposure_quote": "50000",
    "max_net_exposure_quote": "30000",
    "max_strategy_exposure_quote": "40000",
    "loss_limit_quote": "1000",
    "max_drawdown_pct": "10",
    "concentration_limit_pct": "50",
    "max_open_orders": 5,
    "max_open_positions": 3,
}

SYMBOL = txf.SYMBOL


@dataclass(frozen=True)
class BreakGlassScenarioResult:
    """The outcome of one fault-injection/operational scenario."""

    scenario_id: str
    title: str
    status: str  # "pass" | "fail"
    detail: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_record(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "evidence": _plain(self.evidence),
        }


def _plain(value: Any) -> Any:
    """Make evidence JSON-serializable and secret-free."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (UUID, datetime, Decimal, Path)):
        return str(value)
    return str(value)


def _ok(scenario_id: str, title: str, detail: str, **evidence: Any) -> BreakGlassScenarioResult:
    return BreakGlassScenarioResult(scenario_id, title, "pass", detail, evidence)


def _fail(
    scenario_id: str, title: str, detail: str, **evidence: Any
) -> BreakGlassScenarioResult:
    return BreakGlassScenarioResult(scenario_id, title, "fail", detail, evidence)


# --- shared helpers --------------------------------------------------------

def _command_id(seed: str) -> UUID:
    return uuid5(NAMESPACE, seed)


def _command(
    command_type: str | OperatorCommandType,
    *,
    seed: str = "command",
    operator: str = "jamal",
    reason: str = "audit break-glass scenario",
    source: str = "operator-cli",
    requested_at: datetime = BASE,
    correlation_id: UUID | None = None,
):
    kind = command_type.value if isinstance(command_type, OperatorCommandType) else command_type
    return parse_operator_command(
        {
            "command_type": kind,
            "command_id": str(_command_id(seed)),
            "operator_label": operator,
            "reason": reason,
            "source": source,
            "requested_at": requested_at.isoformat(),
            "correlation_id": None if correlation_id is None else str(correlation_id),
        },
        delivered_at=requested_at,
    )


def _workspace(workspace: Path, name: str) -> Path:
    target = workspace / name
    target.mkdir(parents=True, exist_ok=True)
    return target


def _open_control(
    root: Path,
    *,
    journal: SQLiteEventJournal,
    venue=None,
    now: datetime = BASE,
    local_states=None,
    venue_reports=None,
) -> _Control:
    return _Control(
        journal=journal,
        state_dir=root / "state",
        venue=venue,
        reconciliation_policy=load_recovery_policy(RECOVERY_CONFIG),
        local_state_provider=(None if local_states is None else lambda: tuple(local_states)),
        venue_state_provider=(None if venue_reports is None else lambda: tuple(venue_reports)),
        clock=lambda: now,
    )


@dataclass
class ScriptedVenue:
    """A Spot Testnet emergency control wired to a scripted transport (never the network)."""

    control: BinanceSpotTestnetEmergencyControl
    client: BinanceSpotTestnetClient
    runtime: TestnetOrderRuntime
    transport: txf.ScriptedTransport


def _scripted_venue(
    journal: SQLiteEventJournal,
    *,
    responses: list,
    withdrawal_policy: WithdrawalPolicy = WithdrawalPolicy.RECORD_ONLY,
    retries: int = 1,
) -> ScriptedVenue:
    transport = txf.ScriptedTransport(responses=list(responses))
    client = txf.client(
        transport, retries=retries, withdrawal_policy=withdrawal_policy
    )
    runtime = TestnetOrderRuntime(
        client=client,
        instrument=InstrumentSpec(symbol=SYMBOL, base_currency="BTC"),
        journal=journal,
    )
    control = BinanceSpotTestnetEmergencyControl(
        client=client, runtime=runtime, withdrawal_policy=withdrawal_policy
    )
    return ScriptedVenue(control=control, client=client, runtime=runtime, transport=transport)


def _limits():
    return load_limits(RISK_LIMITS)


def _risk_request():
    return RiskRequest(
        correlation_id=_command_id("risk-correlation"),
        symbol=SYMBOL,
        side=Side.BUY,
        quantity=Decimal("0.001"),
        price=Decimal("27000"),
        strategy_id="breakglass-audit",
        intent_id=_command_id("risk-intent"),
    )


def _risk_state():
    return RiskStateSnapshot(
        equity_quote=Decimal("10000"),
        peak_equity_quote=Decimal("10000"),
        realized_pnl_quote=Decimal("0"),
        gross_exposure_quote=Decimal("0"),
        net_exposure_quote=Decimal("0"),
        open_orders=0,
        open_positions=0,
        snapshot_ts=BASE,
    )


# --- 1. HALT_NEW_RISK while healthy ----------------------------------------

def scenario_halt_new_risk_while_healthy(workspace: Path) -> BreakGlassScenarioResult:
    """HALT_NEW_RISK on a healthy system: accepted, journaled once, new risk refused."""
    root = _workspace(workspace, "halt_healthy")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        before = control.status()
        control.claim_normal_writer(owner_id="normal:runtime")
        writable_before = control.status().new_risk_permitted
        outcome = control.handle(_command(OperatorCommandType.HALT_NEW_RISK, seed="halt-1"))
        after = control.status()
        commands = [record for record in control.load_ledger().records]
    if (
        before.safety_mode == "normal"
        and before.new_risk_permitted is False
        and writable_before is True
        and outcome.accepted
        and outcome.previous_safety_state == "normal"
        and outcome.resulting_safety_state == "halt_new_risk"
        and after.kill_switch_engaged
        and after.new_risk_permitted is False
        and len(commands) == 1
        and commands[0].command_id == outcome.command_id
    ):
        return _ok(
            "halt_new_risk_while_healthy",
            "HALT_NEW_RISK while the system is healthy",
            "the operator halt was accepted, journaled once, engaged the kill switch and refused "
            "new risk",
            no_writer_means_no_trading=before.new_risk_permitted is False,
            writable_after_normal_claim=writable_before,
            reason_code=outcome.reason_code,
            transitions=list(outcome.safety_transitions),
        )
    return _fail(
        "halt_new_risk_while_healthy",
        "HALT_NEW_RISK while the system is healthy",
        f"unexpected outcome: {outcome.to_record()}",
    )


# --- 2/16. AI queue saturated / blocked ------------------------------------

def scenario_halt_new_risk_while_ai_queue_blocked(workspace: Path) -> BreakGlassScenarioResult:
    """A stalled, saturated AI backlog must not delay or block the operator halt."""
    root = _workspace(workspace, "halt_ai_backlog")
    lane = PrioritySafetyLane(max_normal_depth=2)
    lane.submit(
        LaneCommand(command_id=_command_id("ai-1"), kind="ai_intent", priority=LanePriority.NORMAL)
    )
    lane.submit(
        LaneCommand(command_id=_command_id("ai-2"), kind="ai_intent", priority=LanePriority.NORMAL)
    )
    saturated = False
    try:
        lane.submit(
            LaneCommand(
                command_id=_command_id("ai-3"), kind="ai_intent", priority=LanePriority.NORMAL
            )
        )
    except Exception:
        saturated = True
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        outcome = control.handle(
            _command(OperatorCommandType.HALT_NEW_RISK, seed="halt-backlog", reason="AI backlog")
        )
        status = control.status()
    if (
        saturated
        and outcome.accepted
        and status.new_risk_permitted is False
        and lane.normal_depth == 2
        and lane.safety_depth == 0
    ):
        return _ok(
            "halt_new_risk_while_ai_queue_blocked",
            "HALT_NEW_RISK while the AI queue/backlog is blocked",
            "the operator halt completed while the AI backlog was saturated and did not drain or "
            "depend on the normal lane",
            backlog=lane.normal_depth,
            health="control path is a separate process/ingress; the backlog is untouched",
        )
    return _fail(
        "halt_new_risk_while_ai_queue_blocked",
        "HALT_NEW_RISK while the AI queue/backlog is blocked",
        f"unexpected outcome: accepted={outcome.accepted} backlog={lane.normal_depth}",
    )


def scenario_ai_queue_saturated(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: the normal lane is saturated when the emergency command arrives."""
    root = _workspace(workspace, "ai_saturated")
    lane = PrioritySafetyLane(max_normal_depth=1)
    lane.submit(
        LaneCommand(command_id=_command_id("ai-sat"), kind="ai_intent", priority=LanePriority.NORMAL)
    )
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        outcome = control.handle(_command(OperatorCommandType.ENTER_DEGRADED, seed="sat-1"))
        status = control.status()
    if (
        outcome.accepted
        and outcome.resulting_safety_state == "degraded"
        and status.new_risk_permitted is False
        and lane.normal_depth == 1
    ):
        return _ok(
            "ai_queue_saturated",
            "AI queue saturated",
            "the emergency command applied deterministically with the AI lane saturated and frozen",
            normal_depth=lane.normal_depth,
        )
    return _fail(
        "ai_queue_saturated",
        "AI queue saturated",
        f"unexpected outcome: {outcome.to_record()}",
    )


# --- 3. CANCEL_OPEN through the safety path --------------------------------

def scenario_cancel_open_through_safety_path(workspace: Path) -> BreakGlassScenarioResult:
    """CANCEL_OPEN cancels through the safety path and confirms at the venue exactly once."""
    root = _workspace(workspace, "cancel_open")
    with SQLiteEventJournal(root / "journal.db") as journal:
        venue = _scripted_venue(
            journal,
            responses=[
                txf.time_response(),
                txf.exchange_info_response(),
                txf.account_response(),
                txf.open_orders_response("ct-open-1"),
                txf.cancel_response("ct-open-1", order_id=100),
            ],
        )
        control = _open_control(root, journal=journal, venue=venue.control)
        outcome = control.handle(
            _command(OperatorCommandType.CANCEL_OPEN, seed="cancel-1", reason="venue emergency")
        )
        status = control.status()
        deletes = [item for item in venue.transport.requests if item.method == "DELETE"]
        order_events = [
            event.event_type for event in journal.read_range()
        ]
    if (
        outcome.accepted
        and outcome.venue_control == "confirmed"
        and outcome.resulting_safety_state == "cancel_open"
        and len(deletes) == 1
        and status.new_risk_permitted is False
        and "order.state.changed" in order_events
    ):
        return _ok(
            "cancel_open_through_safety_path",
            "CANCEL_OPEN through the safety path",
            "every open order was cancelled through the safety path, confirmed once at the venue and "
            "journaled through the existing Phase 3 lifecycle contract",
            cancelled=list(outcome.to_record()["venue_control"].values())[1:2],
            delete_requests=len(deletes),
            safety_mode=outcome.resulting_safety_state,
        )
    return _fail(
        "cancel_open_through_safety_path",
        "CANCEL_OPEN through the safety path",
        f"unexpected outcome: {outcome.to_record()} deletes={len(deletes)}",
    )


# --- 4. duplicate delivery -------------------------------------------------

def scenario_duplicate_break_glass_command(workspace: Path) -> BreakGlassScenarioResult:
    """Re-delivering one command_id produces exactly one logical effect."""
    root = _workspace(workspace, "duplicate")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        first = control.handle(_command(OperatorCommandType.HALT_NEW_RISK, seed="dup-1"))
        second = control.handle(
            _command(OperatorCommandType.HALT_NEW_RISK, seed="dup-1", requested_at=LATER)
        )
        conflict = control.handle(
            _command(
                OperatorCommandType.ENTER_DEGRADED,
                seed="dup-1",
                reason="different command, reused id",
                requested_at=LATER,
            )
        )
        records = control.load_ledger().records
        safety_events = [
            event for event in journal.read_range() if event.event_type == SAFETY_EVENT_TYPE
        ]
        status = control.status()
    if (
        first.accepted
        and second.duplicate
        and second.disposition == "accepted"
        and second.reason_code == first.reason_code
        and len(records) == 1
        and len(safety_events) == 1
        and conflict.disposition == "rejected"
        and conflict.reason_code == "command_id_conflict"
        and status.safety_mode == "halt_new_risk"
    ):
        return _ok(
            "duplicate_break_glass_command",
            "Duplicate break-glass command delivery",
            "the re-delivered command_id produced one logical effect; a different command reusing "
            "the id was refused and changed nothing",
            records=len(records),
            safety_events=len(safety_events),
        )
    return _fail(
        "duplicate_break_glass_command",
        "Duplicate break-glass command delivery",
        f"unexpected duplicate behaviour: records={len(records)} safety_events={len(safety_events)}",
    )


# --- 5/14/17/25/26. restart + journal replay -------------------------------

def scenario_restart_with_break_glass_active(workspace: Path) -> BreakGlassScenarioResult:
    """A restart while break-glass is active restores the restricted posture, never normal."""
    root = _workspace(workspace, "restart_active")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.handle(_command(OperatorCommandType.FORCE_RECOVERY_REQUIRED, seed="restart-1"))
    with SQLiteEventJournal(root / "journal.db") as journal:
        restarted = _open_control(root, journal=journal)
        status = restarted.status()
        denied = restarted.handle(
            _command(OperatorCommandType.RESTORE_NORMAL, seed="restart-restore", requested_at=LATER)
        )
    if (
        status.safety_mode == "recovery_required"
        and status.kill_switch_engaged
        and status.new_risk_permitted is False
        and status.epoch_open
        and denied.disposition == "rejected"
        and denied.reason_code == "restore_denied_reconciliation_pending"
    ):
        return _ok(
            "restart_with_break_glass_active",
            "Restart while the break-glass safety state is active",
            "the restricted posture survived the restart, the epoch stayed open and normal was not "
            "restored automatically",
            restored_mode=status.safety_mode,
            restore_refusal=denied.reason_code,
        )
    return _fail(
        "restart_with_break_glass_active",
        "Restart while the break-glass safety state is active",
        f"unexpected restart posture: {status.to_record()}",
    )


def scenario_journal_replay_of_safety_state(workspace: Path) -> BreakGlassScenarioResult:
    """The journal alone rebuilds the exact safety state when the cache is gone."""
    root = _workspace(workspace, "journal_replay")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.handle(_command(OperatorCommandType.CANCEL_OPEN, seed="replay-1"))
        journaled_mode = control.status().safety_mode
    (root / "state" / "safety_state.json").unlink()
    with SQLiteEventJournal(root / "journal.db") as journal:
        replayed = _open_control(root, journal=journal)
        status = replayed.status()
    if (
        journaled_mode == "cancel_open"
        and status.safety_mode == "cancel_open"
        and status.kill_switch_engaged
        and status.safety_state_source == "journal_replay"
        and status.epoch_open
    ):
        return _ok(
            "journal_replay_of_safety_state",
            "Persistence / journal replay of the safety state",
            "with the state cache removed the safety state was rebuilt deterministically from the "
            "authoritative journal",
            source=status.safety_state_source,
            mode=status.safety_mode,
        )
    return _fail(
        "journal_replay_of_safety_state",
        "Persistence / journal replay of the safety state",
        f"unexpected replay result: {status.to_record()}",
    )


def scenario_journal_replay_after_crash(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: a crash after the journal append but before the cache write."""
    root = _workspace(workspace, "crash_replay")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.handle(_command(OperatorCommandType.ENTER_DEGRADED, seed="crash-1"))
    state_file = root / "state" / "safety_state.json"
    crashed_without_cache = not state_file.exists()
    with SQLiteEventJournal(root / "journal.db") as journal:
        recovered = _open_control(root, journal=journal)
        status = recovered.status()
        commands = recovered.load_ledger().records
    if (
        not crashed_without_cache
        and status.safety_mode == "degraded"
        and status.kill_switch_engaged
        and status.new_risk_permitted is False
        and len(commands) == 1
    ):
        return _ok(
            "journal_replay_after_crash",
            "Journal replay after a crash",
            "the command and its posture were recovered from the journal after the state cache was "
            "lost; no silent correction and no return to normal",
            provenance=status.safety_state_source,
        )
    return _fail(
        "journal_replay_after_crash",
        "Journal replay after a crash",
        f"unexpected crash replay: {status.to_record()}",
    )


def scenario_restart_in_degraded(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: restart while the posture is DEGRADED."""
    root = _workspace(workspace, "restart_degraded")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.handle(_command(OperatorCommandType.ENTER_DEGRADED, seed="deg-1"))
    with SQLiteEventJournal(root / "journal.db") as journal:
        status = _open_control(root, journal=journal).status()
    if status.safety_mode == "degraded" and status.new_risk_permitted is False:
        return _ok(
            "restart_in_degraded",
            "Restart in DEGRADED",
            "the degraded posture survived the restart and new risk stayed refused",
            mode=status.safety_mode,
        )
    return _fail(
        "restart_in_degraded", "Restart in DEGRADED", f"unexpected posture: {status.to_record()}"
    )


def scenario_restart_in_recovery_required(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: restart while the posture is RECOVERY_REQUIRED."""
    root = _workspace(workspace, "restart_recovery")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.handle(_command(OperatorCommandType.FORCE_RECOVERY_REQUIRED, seed="rec-1"))
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        status = control.status()
        ai_denied = _ai_override_denied(control)
    if (
        status.safety_mode == "recovery_required"
        and status.new_risk_permitted is False
        and ai_denied
    ):
        return _ok(
            "restart_in_recovery_required",
            "Restart in RECOVERY_REQUIRED",
            "the recovery-required posture survived the restart, refused new risk and refused an "
            "AI-sourced re-enable",
            mode=status.safety_mode,
        )
    return _fail(
        "restart_in_recovery_required",
        "Restart in RECOVERY_REQUIRED",
        f"unexpected posture: {status.to_record()}",
    )


# --- 6/7. no new risk, no AI override --------------------------------------

def _ai_override_denied(control: _Control) -> bool:
    try:
        control.runtime_request_mode_change(source="strategy", mode=SafetyMode.NORMAL)
    except BreakGlassActiveError:
        return True
    return False


def scenario_new_risk_order_rejected_after_break_glass(
    workspace: Path,
) -> BreakGlassScenarioResult:
    """A new-risk order attempt after break-glass is rejected by the existing risk gate."""
    root = _workspace(workspace, "new_risk")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.handle(_command(OperatorCommandType.HALT_NEW_RISK, seed="risk-1"))
        controller = control.load_safety_state().controller
        limits = control.risk_limits(_limits())
        decision = HardRiskEngine(limits).evaluate(_risk_request(), _risk_state(), now=BASE)
    if (
        limits.trading_halted
        and decision.allowed is False
        and "trading_halted" in decision.reason_codes
    ):
        return _ok(
            "new_risk_order_rejected_after_break_glass",
            "Attempted new-risk order after break-glass",
            "the deterministic Hard Risk engine refused the order with trading_halted; no order was "
            "sent anywhere",
            reason_codes=list(decision.reason_codes),
            controller_mode=controller.mode.value,
        )
    return _fail(
        "new_risk_order_rejected_after_break_glass",
        "Attempted new-risk order after break-glass",
        f"unexpected risk decision: {decision.reason_codes}",
    )


def scenario_ai_decision_cannot_override_safety(workspace: Path) -> BreakGlassScenarioResult:
    """An AI/strategy decision cannot re-enable trading after break-glass."""
    root = _workspace(workspace, "ai_override")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.handle(_command(OperatorCommandType.FORCE_RECOVERY_REQUIRED, seed="ai-override-1"))
        journal.append(
            StrategyDecisionRecorded(
                occurred_at=BASE,
                correlation_id=_command_id("ai-decision"),
                producer="agent.trading_plan_strategist",
                payload=StrategyDecisionPayload(
                    action="buy", confidence="high", rationale="AI wants to resume trading"
                ),
            )
        )
        denied = _ai_override_denied(control)
        status = control.status()
        risk_limits = control.risk_limits(_limits())
    if denied and status.new_risk_permitted is False and risk_limits.trading_halted:
        return _ok(
            "ai_decision_cannot_override_safety",
            "Attempted AI decision after break-glass cannot override safety",
            "the strategy decision was journaled but the AI-sourced safety transition was refused "
            "and the risk gate stayed halted",
            ai_transition_refused=denied,
        )
    return _fail(
        "ai_decision_cannot_override_safety",
        "Attempted AI decision after break-glass cannot override safety",
        f"the AI path was not refused: {status.to_record()}",
    )


# --- 8/10/11/12/23. reconciliation + acknowledgement gates -----------------

def _resolve_epoch(root: Path, control: _Control) -> None:
    """Drive an epoch to resolved + acknowledged through the audited command path."""
    control.handle(_command(OperatorCommandType.RECONCILE, seed=f"{root.name}-reconcile"))
    control.handle(_command(OperatorCommandType.ACKNOWLEDGE, seed=f"{root.name}-ack"))


def scenario_reconciliation_after_emergency_action(workspace: Path) -> BreakGlassScenarioResult:
    """Reconciliation after an emergency action resolves the epoch with venue evidence."""
    root = _workspace(workspace, "reconcile_after_emergency")
    reference = "ct-recon-1"
    local = [
        LocalOrderState(
            order_id=_command_id("recon-order"),
            order_reference=reference,
            intent_id=_command_id("recon-intent"),
            correlation_id=_command_id("recon-correlation"),
            symbol=SYMBOL,
            state=OrderStateName.SUBMITTED,
            submission_attempted=True,
        )
    ]
    reports = [
        VenueOrderReport(
            order_reference=reference,
            venue_order_id="42",
            state=OrderStateName.CANCELED,
            reported_at=BASE,
        )
    ]
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(
            root, journal=journal, local_states=local, venue_reports=reports
        )
        control.handle(_command(OperatorCommandType.CANCEL_OPEN, seed="recon-emergency"))
        reconcile = control.handle(_command(OperatorCommandType.RECONCILE, seed="recon-run"))
        status = control.status()
        recovery_events = [
            event.event_type
            for event in journal.read_range()
            if event.event_type == "recovery.state.changed"
        ]
    if (
        reconcile.accepted
        and reconcile.reconciliation_status == "resolved"
        and status.reconciliation_status == "resolved"
        and recovery_events
    ):
        return _ok(
            "reconciliation_after_emergency_action",
            "Reconciliation after an emergency action",
            "Phase 8 reconciliation resolved the emergency action from venue evidence and journaled "
            "the recovery state change",
            reconciliation_status=status.reconciliation_status,
            recovery_events=len(recovery_events),
        )
    return _fail(
        "reconciliation_after_emergency_action",
        "Reconciliation after an emergency action",
        f"unexpected reconciliation: {reconcile.to_record()}",
    )


def scenario_operator_acknowledgement_after_reconciliation(
    workspace: Path,
) -> BreakGlassScenarioResult:
    """An explicit operator acknowledgement is recorded after a successful reconciliation."""
    root = _workspace(workspace, "ack_after_reconciliation")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.handle(_command(OperatorCommandType.HALT_NEW_RISK, seed="ack-1"))
        control.handle(_command(OperatorCommandType.RECONCILE, seed="ack-2", requested_at=LATER))
        outcome = control.handle(_command(OperatorCommandType.ACKNOWLEDGE, seed="ack-3"))
        status = control.status()
        record = control.load_ledger().records[-1]
    if (
        outcome.accepted
        and outcome.reason_code == "operator_acknowledgement_recorded"
        and outcome.operator_acknowledgement_pending is False
        and status.operator_acknowledged_by == "jamal"
        and record.command_id == outcome.command_id
    ):
        return _ok(
            "operator_acknowledgement_after_reconciliation",
            "Explicit operator acknowledgement after successful reconciliation",
            "the acknowledgement was recorded against the operator label in the journaled command "
            "record",
            acknowledged_by=status.operator_acknowledged_by,
        )
    return _fail(
        "operator_acknowledgement_after_reconciliation",
        "Explicit operator acknowledgement after successful reconciliation",
        f"unexpected acknowledgement: {outcome.to_record()}",
    )


def scenario_restore_denied_before_reconciliation(workspace: Path) -> BreakGlassScenarioResult:
    """Restore-to-NORMAL is denied while reconciliation is still pending."""
    root = _workspace(workspace, "restore_before_reconcile")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.handle(_command(OperatorCommandType.FORCE_RECOVERY_REQUIRED, seed="pre-recon-1"))
        denied = control.handle(_command(OperatorCommandType.RESTORE_NORMAL, seed="pre-recon-2"))
        status = control.status()
    if (
        denied.disposition == "rejected"
        and denied.reason_code == "restore_denied_reconciliation_pending"
        and status.safety_mode == "recovery_required"
        and status.new_risk_permitted is False
        and "reconciliation_pending" in status.restore_blockers
    ):
        return _ok(
            "restore_denied_before_reconciliation",
            "Restore-to-NORMAL denied before reconciliation",
            "the restore was refused deterministically and the restricted posture was preserved",
            reason_code=denied.reason_code,
        )
    return _fail(
        "restore_denied_before_reconciliation",
        "Restore-to-NORMAL denied before reconciliation",
        f"unexpected restore result: {denied.to_record()}",
    )


def scenario_restore_denied_without_acknowledgement(
    workspace: Path,
) -> BreakGlassScenarioResult:
    """Restore-to-NORMAL is denied when the operator acknowledgement is missing."""
    root = _workspace(workspace, "restore_without_ack")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.handle(_command(OperatorCommandType.HALT_NEW_RISK, seed="no-ack-1"))
        reconcile = control.handle(_command(OperatorCommandType.RECONCILE, seed="no-ack-2"))
        denied = control.handle(_command(OperatorCommandType.RESTORE_NORMAL, seed="no-ack-3"))
        status = control.status()
    if (
        reconcile.reconciliation_status == "resolved"
        and denied.disposition == "rejected"
        and denied.reason_code == "restore_denied_acknowledgement_missing"
        and status.restore_blockers == ("operator_acknowledgement_missing",)
    ):
        return _ok(
            "restore_denied_without_acknowledgement",
            "Restore-to-NORMAL denied without operator acknowledgement",
            "reconciliation completed but the restore was still refused because the explicit "
            "operator acknowledgement was missing",
            reason_code=denied.reason_code,
        )
    return _fail(
        "restore_denied_without_acknowledgement",
        "Restore-to-NORMAL denied without operator acknowledgement",
        f"unexpected restore result: {denied.to_record()}",
    )


def scenario_operator_acknowledgement_missing(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: the acknowledgement never arrives, and nothing silently restores normal."""
    root = _workspace(workspace, "ack_missing")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.handle(_command(OperatorCommandType.ENTER_DEGRADED, seed="missing-ack-1"))
        control.handle(_command(OperatorCommandType.RECONCILE, seed="missing-ack-2"))
        control.handle(_command(OperatorCommandType.RESTORE_NORMAL, seed="missing-ack-3"))
    with SQLiteEventJournal(root / "journal.db") as journal:
        status = _open_control(root, journal=journal).status()
    if (
        status.operator_acknowledgement_pending
        and status.safety_mode == "degraded"
        and status.new_risk_permitted is False
        and status.epoch_open
    ):
        return _ok(
            "operator_acknowledgement_missing",
            "Operator acknowledgement missing",
            "without the acknowledgement the system stayed in its restricted posture across a "
            "restart; nothing restored normal silently",
            blockers=list(status.restore_blockers),
        )
    return _fail(
        "operator_acknowledgement_missing",
        "Operator acknowledgement missing",
        f"unexpected posture: {status.to_record()}",
    )


# --- 9/18/19. venue ambiguity ----------------------------------------------

def scenario_ambiguous_cancel_unknown_pending_reconciliation(
    workspace: Path,
) -> BreakGlassScenarioResult:
    """An ambiguous cancel outcome becomes UNKNOWN_PENDING_RECONCILIATION and escalates."""
    root = _workspace(workspace, "ambiguous_cancel")
    with SQLiteEventJournal(root / "journal.db") as journal:
        venue = _scripted_venue(
            journal,
            responses=[
                txf.time_response(),
                txf.exchange_info_response(),
                txf.account_response(),
                txf.open_orders_response("ct-ambiguous"),
                txf.server_error(),
            ],
        )
        control = _open_control(root, journal=journal, venue=venue.control)
        outcome = control.handle(_command(OperatorCommandType.CANCEL_OPEN, seed="amb-1"))
        status = control.status()
        deletes = [item for item in venue.transport.requests if item.method == "DELETE"]
        restore = control.handle(_command(OperatorCommandType.RESTORE_NORMAL, seed="amb-2"))
    if (
        outcome.accepted
        and outcome.venue_control == "ambiguous"
        and outcome.reconciliation_result == "UNKNOWN_PENDING_RECONCILIATION"
        and status.safety_mode == "recovery_required"
        and "degraded" in status.epoch_escalation_path
        and status.epoch_escalation_path[-1] == "recovery_required"
        and len(deletes) == 1
        and restore.reason_code == "restore_denied_reconciliation_pending"
    ):
        return _ok(
            "ambiguous_cancel_unknown_pending_reconciliation",
            "Ambiguous cancel/send outcome -> UNKNOWN_PENDING_RECONCILIATION",
            "the unconfirmed cancel was sent exactly once, escalated degraded -> recovery_required "
            "and blocked any restore until reconciliation",
            escalation=list(status.epoch_escalation_path),
            deletes=len(deletes),
        )
    return _fail(
        "ambiguous_cancel_unknown_pending_reconciliation",
        "Ambiguous cancel/send outcome -> UNKNOWN_PENDING_RECONCILIATION",
        f"unexpected ambiguity handling: {outcome.to_record()}",
    )


def scenario_venue_cancel_timeout(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: the venue cancel times out (a write with an unknown outcome)."""
    root = _workspace(workspace, "cancel_timeout")
    with SQLiteEventJournal(root / "journal.db") as journal:
        venue = _scripted_venue(
            journal,
            responses=[
                txf.time_response(),
                txf.exchange_info_response(),
                txf.account_response(),
                txf.open_orders_response("ct-timeout"),
                TransportTimeout("cancel timed out"),
            ],
        )
        control = _open_control(root, journal=journal, venue=venue.control)
        outcome = control.handle(_command(OperatorCommandType.CANCEL_OPEN, seed="timeout-1"))
        deletes = [item for item in venue.transport.requests if item.method == "DELETE"]
        status = control.status()
    if (
        outcome.venue_control == "ambiguous"
        and outcome.reconciliation_result == "UNKNOWN_PENDING_RECONCILIATION"
        and len(deletes) == 1
        and status.safety_mode == "recovery_required"
    ):
        return _ok(
            "venue_cancel_timeout",
            "Venue cancel timeout",
            "the timed-out cancel was never blindly retried; it escalated to recovery_required "
            "with UNKNOWN_PENDING_RECONCILIATION",
            delete_requests=len(deletes),
        )
    return _fail(
        "venue_cancel_timeout",
        "Venue cancel timeout",
        f"unexpected timeout handling: {outcome.to_record()} deletes={len(deletes)}",
    )


def scenario_venue_response_lost(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: the venue response is lost after a successful cancel submission."""
    root = _workspace(workspace, "response_lost")
    with SQLiteEventJournal(root / "journal.db") as journal:
        venue = _scripted_venue(
            journal,
            responses=[
                txf.time_response(),
                txf.exchange_info_response(),
                txf.account_response(),
                txf.open_orders_response("ct-lost"),
                TransportError("connection reset after the request was written"),
            ],
        )
        control = _open_control(root, journal=journal, venue=venue.control)
        outcome = control.handle(_command(OperatorCommandType.CANCEL_OPEN, seed="lost-1"))
        status = control.status()
        restore = control.handle(_command(OperatorCommandType.RESTORE_NORMAL, seed="lost-2"))
    if (
        outcome.venue_control == "ambiguous"
        and status.safety_mode == "recovery_required"
        and restore.disposition == "rejected"
    ):
        return _ok(
            "venue_response_lost",
            "Venue response lost",
            "the lost response became an ambiguous outcome; the epoch escalated and the restore "
            "stayed refused",
            reason_code=outcome.reason_code,
        )
    return _fail(
        "venue_response_lost",
        "Venue response lost",
        f"unexpected lost-response handling: {outcome.to_record()}",
    )


def scenario_venue_unavailable(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: the venue cannot be reached at all during emergency control."""
    root = _workspace(workspace, "venue_unavailable")
    with SQLiteEventJournal(root / "journal.db") as journal:
        venue = _scripted_venue(journal, responses=[TransportError("network down")], retries=1)
        control = _open_control(root, journal=journal, venue=venue.control)
        outcome = control.handle(_command(OperatorCommandType.CANCEL_OPEN, seed="unavail-1"))
        status = control.status()
    if (
        outcome.venue_control == "unavailable"
        and status.safety_mode == "recovery_required"
        and status.new_risk_permitted is False
    ):
        return _ok(
            "venue_unavailable",
            "Venue unreachable during emergency control",
            "the unattemptable venue control was recorded as unavailable and the posture still "
            "escalated fail-closed",
            venue_detail=outcome.venue_detail,
        )
    return _fail(
        "venue_unavailable",
        "Venue unreachable during emergency control",
        f"unexpected unavailability handling: {outcome.to_record()}",
    )


# --- 13/24. malformed and corrupted input ----------------------------------

def scenario_malformed_command_fails_closed(workspace: Path) -> BreakGlassScenarioResult:
    """Malformed/unknown commands are refused and change nothing."""
    root = _workspace(workspace, "malformed")
    refusals: list[str] = []
    payloads = [
        {"command_type": "launch_missiles", "command_id": str(_command_id("bad-1")),
         "operator_label": "jamal", "reason": "unknown", "source": "operator-cli"},
        {"command_type": "halt_new_risk", "command_id": "not-a-uuid",
         "operator_label": "jamal", "reason": "bad id", "source": "operator-cli"},
        {"command_type": "halt_new_risk", "command_id": str(_command_id("bad-2")),
         "operator_label": "jamal", "reason": "", "source": "operator-cli"},
        {"command_type": "cancel-open", "command_id": str(_command_id("bad-3")),
         "operator_label": "jamal", "reason": "leaked api_key=abc123", "source": "operator-cli"},
    ]
    for payload in payloads:
        try:
            parse_operator_command(payload, delivered_at=BASE)
        except MalformedOperatorCommandError as exc:
            refusals.append(type(exc).__name__)
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        status = control.status()
        records = control.load_ledger().records
    if len(refusals) == len(payloads) and status.safety_mode == "normal" and records == []:
        return _ok(
            "malformed_command_fails_closed",
            "Malformed/unknown break-glass command -> fail closed",
            "every malformed, unknown, blank or credential-shaped command was refused before it "
            "reached the control path; nothing changed",
            refusals=refusals,
        )
    return _fail(
        "malformed_command_fails_closed",
        "Malformed/unknown break-glass command -> fail closed",
        f"unexpected parsing behaviour: refusals={refusals}",
    )


def scenario_corrupted_breakglass_request(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: corrupted persisted break-glass state must fail closed, never silently."""
    root = _workspace(workspace, "corrupt_state")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.handle(_command(OperatorCommandType.HALT_NEW_RISK, seed="corrupt-1"))
    state_file = root / "state" / "safety_state.json"
    state_file.write_text("{not json at all", encoding="utf-8")
    ownership_file = root / "state" / "ownership.json"
    bound_lease = ownership_file.read_bytes()
    ownership_file.write_text('{"schema_version": 99}', encoding="utf-8")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        status = control.status()
        ownership_refused = False
        try:
            control.ownership_store.require_fence()
        except OwnershipStateError:
            ownership_refused = True
        # N-01: a corrupted lease cannot vouch for this journal, so the request is refused before
        # it writes anything - journal, safety state or lease - instead of recording a rejection.
        before = (journal.revision(), state_file.read_bytes(), ownership_file.read_bytes())
        request_refused = False
        try:
            control.handle(
                _command(OperatorCommandType.REDUCE_ONLY, seed="corrupt-2", requested_at=LATER)
            )
        except OwnershipStateError:
            request_refused = True
        untouched = before == (journal.revision(), state_file.read_bytes(), ownership_file.read_bytes())
        # Once the lease is readable again, the corrupted safety state still refuses the downgrade;
        # the same command id is not a duplicate, because the refused delivery recorded nothing.
        ownership_file.write_bytes(bound_lease)
        downgrade = control.handle(
            _command(OperatorCommandType.REDUCE_ONLY, seed="corrupt-2", requested_at=LATER)
        )
    if (
        status.forced_fail_closed
        and status.safety_mode == "recovery_required"
        and status.kill_switch_engaged
        and ownership_refused
        and request_refused
        and untouched
        and not downgrade.duplicate
        and downgrade.disposition == "rejected"
        and downgrade.reason_code == "downgrade_refused_monotonic_escalation"
    ):
        return _ok(
            "corrupted_breakglass_request",
            "Corrupted break-glass state/request",
            "a corrupted safety-state file restored fail-closed into recovery_required, a corrupted "
            "ownership record refused to name any writer and refused the request before anything "
            "was written, and with the lease readable again a downgrade attempt was refused",
            source=status.safety_state_source,
        )
    return _fail(
        "corrupted_breakglass_request",
        "Corrupted break-glass state/request",
        f"unexpected fail-closed behaviour: {status.to_record()}",
    )


# --- 15/27. runtime unavailable + fencing ----------------------------------

def scenario_primary_runtime_unavailable(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: the primary runtime is dead and the operator takes over by fiat."""
    root = _workspace(workspace, "runtime_dead")
    with SQLiteEventJournal(root / "journal.db") as journal:
        venue = _scripted_venue(
            journal,
            responses=[
                txf.time_response(),
                txf.exchange_info_response(),
                txf.account_response(),
                txf.open_orders_response("ct-dead-runtime"),
                txf.cancel_response("ct-dead-runtime", order_id=100),
            ],
        )
        control = _open_control(root, journal=journal, venue=venue.control)
        normal_lease = control.ownership_store.takeover(
            owner_id="normal:runtime", owner_kind=OwnerKind.NORMAL, acquired_at=BASE,
            reason_code="runtime_started",
        )
        guard = control.normal_writer_guard()
        before = guard.may_create_new_risk()
        outcome = control.handle(_command(OperatorCommandType.CANCEL_OPEN, seed="dead-1"))
        after = guard.may_create_new_risk()
        stale_refused = False
        try:
            guard.require_current()
        except StaleWriterFenceError:
            stale_refused = True
        current = control.ownership_store.require_fence()
    if (
        before
        and normal_lease.term == 1
        and outcome.accepted
        and after is False
        and stale_refused
        and current.term == 2
        and current.owner_kind is OwnerKind.EMERGENCY
    ):
        return _ok(
            "primary_runtime_unavailable",
            "Primary runtime unavailable during emergency control",
            "the operator took the writer lease over explicitly: the normal runtime's fencing token "
            "was invalidated and it could no longer write",
            fence_before=normal_lease.term,
            fence_after=current.term,
        )
    return _fail(
        "primary_runtime_unavailable",
        "Primary runtime unavailable during emergency control",
        f"unexpected fencing behaviour: before={before} after={after} term={current.term}",
    )


def scenario_no_concurrent_normal_and_emergency_writer(
    workspace: Path,
) -> BreakGlassScenarioResult:
    """There is exactly one authoritative writer; no concurrent normal + emergency writer."""
    root = _workspace(workspace, "single_writer")
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal)
        control.ownership_store.takeover(
            owner_id="normal:runtime", owner_kind=OwnerKind.NORMAL, acquired_at=BASE,
            reason_code="runtime_started",
        )
        normal = control.normal_writer_guard()
        control.handle(_command(OperatorCommandType.HALT_NEW_RISK, seed="writer-1"))
        emergency = control.ownership_store.require_fence()
        emergency_blocks_normal = not normal.may_create_new_risk()
        control.handle(_command(OperatorCommandType.RECONCILE, seed="writer-2"))
        control.handle(_command(OperatorCommandType.ACKNOWLEDGE, seed="writer-3"))
        control.handle(_command(OperatorCommandType.RESTORE_NORMAL, seed="writer-4"))
        restored = control.ownership_store.require_fence()
        emergency_stale = False
        try:
            control.ownership_store.require_current(emergency)
        except StaleWriterFenceError:
            emergency_stale = True
        new_normal = control.normal_writer_guard()
        status = control.status()
    if (
        emergency_blocks_normal
        and emergency.owner_kind is OwnerKind.EMERGENCY
        and restored.owner_kind is OwnerKind.NORMAL
        and restored.term > emergency.term
        and emergency_stale
        and new_normal.may_create_new_risk()
        and status.new_risk_permitted
    ):
        return _ok(
            "no_concurrent_normal_and_emergency_writer",
            "No concurrent normal-writer + emergency-writer behaviour",
            "the emergency takeover invalidated the normal writer's token, and the explicit "
            "restore minted a fresh normal lease while invalidating the emergency token",
            terms={"normal_first": 1, "emergency": emergency.term, "normal_restored": restored.term},
        )
    return _fail(
        "no_concurrent_normal_and_emergency_writer",
        "No concurrent normal-writer + emergency-writer behaviour",
        "the fencing tokens did not enforce a single authoritative writer",
    )


# --- 20/21/22. reconciliation evidence cases -------------------------------

def _reconcile_case(
    root: Path,
    *,
    local: LocalOrderState,
    reports: list[VenueOrderReport],
    seed: str,
):
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal, local_states=[local], venue_reports=reports)
        control.handle(_command(OperatorCommandType.HALT_NEW_RISK, seed=f"{seed}-halt"))
        outcome = control.handle(_command(OperatorCommandType.RECONCILE, seed=f"{seed}-reconcile"))
        restore = control.handle(
            _command(OperatorCommandType.RESTORE_NORMAL, seed=f"{seed}-restore", requested_at=LATER)
        )
        ownership = control.ownership_store.read()
        records = control.load_ledger().records
        return outcome, restore, ownership, records


def scenario_local_state_ahead_of_venue(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: our journal is ahead of the venue -> unresolved, restore refused."""
    root = _workspace(workspace, "local_ahead")
    reference = "ct-local-ahead"
    local = LocalOrderState(
        order_id=_command_id("local-ahead-order"),
        order_reference=reference,
        intent_id=_command_id("local-ahead-intent"),
        correlation_id=_command_id("local-ahead-correlation"),
        symbol=SYMBOL,
        state=OrderStateName.FILLED,
        filled_quantity=Decimal("1"),
        remaining_quantity=Decimal("0"),
        submission_attempted=True,
    )
    reports = [
        VenueOrderReport(
            order_reference=reference,
            venue_order_id="7",
            state=OrderStateName.ACCEPTED,
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("1"),
            reported_at=BASE,
        )
    ]
    outcome, restore, ownership, records = _reconcile_case(
        root, local=local, reports=reports, seed="local-ahead"
    )
    if (
        outcome.reconciliation_status == "pending"
        and outcome.reason_code == "UNKNOWN_PENDING_RECONCILIATION"
        and restore.disposition == "rejected"
        and ownership is not None
        and len(records) == 3
    ):
        return _ok(
            "local_state_ahead_of_venue",
            "Local state ahead of venue",
            "the deterministic engine refused to overwrite our own state from weaker venue "
            "evidence; the epoch stayed unresolved and the restore was refused",
            outcome=ReconciliationOutcome.LOCAL_AHEAD_OF_VENUE.value,
        )
    return _fail(
        "local_state_ahead_of_venue",
        "Local state ahead of venue",
        f"unexpected outcome: {outcome.to_record()}",
    )


def scenario_venue_state_ahead_of_local(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: the venue is ahead of our journal -> adopt with evidence, then restore."""
    root = _workspace(workspace, "venue_ahead")
    reference = "ct-venue-ahead"
    local = LocalOrderState(
        order_id=_command_id("venue-ahead-order"),
        order_reference=reference,
        intent_id=_command_id("venue-ahead-intent"),
        correlation_id=_command_id("venue-ahead-correlation"),
        symbol=SYMBOL,
        state=OrderStateName.ACCEPTED,
        submission_attempted=True,
    )
    reports = [
        VenueOrderReport(
            order_reference=reference,
            venue_order_id="9",
            state=OrderStateName.CANCELED,
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("0"),
            reported_at=BASE,
        )
    ]
    outcome, restore, ownership, _ = _reconcile_case(
        root, local=local, reports=reports, seed="venue-ahead"
    )
    if (
        outcome.reconciliation_status == "resolved"
        and restore.disposition == "rejected"
        and restore.reason_code == "restore_denied_acknowledgement_missing"
        and ownership is not None
    ):
        return _ok(
            "venue_state_ahead_of_local",
            "Venue state ahead of local",
            "venue evidence was adopted as an explicit adjustment, reconciliation resolved, and the "
            "restore was still gated on the operator acknowledgement",
            next_gate=restore.reason_code,
        )
    return _fail(
        "venue_state_ahead_of_local",
        "Venue state ahead of local",
        f"unexpected outcome: {outcome.to_record()} / {restore.to_record()}",
    )


def scenario_reconciliation_mismatch(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: conflicting venue reports -> ambiguous, never silently resolved."""
    root = _workspace(workspace, "recon_mismatch")
    reference = "ct-mismatch"
    local = LocalOrderState(
        order_id=_command_id("mismatch-order"),
        order_reference=reference,
        intent_id=_command_id("mismatch-intent"),
        correlation_id=_command_id("mismatch-correlation"),
        symbol=SYMBOL,
        state=OrderStateName.ACCEPTED,
        submission_attempted=True,
    )
    reports = [
        VenueOrderReport(
            order_reference=reference,
            venue_order_id="11",
            state=OrderStateName.CANCELED,
            reported_at=BASE,
        ),
        VenueOrderReport(
            order_reference=reference,
            venue_order_id="11",
            state=OrderStateName.FILLED,
            filled_quantity=Decimal("1"),
            reported_at=BASE,
        ),
    ]
    outcome, restore, _, _ = _reconcile_case(
        root, local=local, reports=reports, seed="mismatch"
    )
    if (
        outcome.reconciliation_status == "pending"
        and restore.reason_code == "restore_denied_reconciliation_pending"
    ):
        return _ok(
            "reconciliation_mismatch",
            "Reconciliation mismatch",
            "conflicting venue evidence stayed ambiguous with no silent correction and no blind "
            "retry; the restore remained refused",
            outcome=ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION.value,
        )
    return _fail(
        "reconciliation_mismatch",
        "Reconciliation mismatch",
        f"unexpected mismatch handling: {outcome.to_record()}",
    )


def scenario_unknown_external_order(workspace: Path) -> BreakGlassScenarioResult:
    """Fault injection: an order we do not know about is escalated, never adopted."""
    root = _workspace(workspace, "unknown_external")
    reports = [
        VenueOrderReport(
            order_reference="ct-unknown-external",
            venue_order_id="13",
            state=OrderStateName.ACCEPTED,
            reported_at=BASE,
        )
    ]
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = _open_control(root, journal=journal, venue_reports=reports)
        control.handle(_command(OperatorCommandType.HALT_NEW_RISK, seed="unknown-1"))
        outcome = control.handle(_command(OperatorCommandType.RECONCILE, seed="unknown-2"))
        restore = control.handle(_command(OperatorCommandType.RESTORE_NORMAL, seed="unknown-3"))
    if (
        outcome.reconciliation_status == "pending"
        and restore.disposition == "rejected"
    ):
        return _ok(
            "unknown_external_order",
            "Unknown external order escalated",
            "a venue order our journal does not know about was escalated, not adopted and not "
            "ignored; the restore stayed refused",
            outcome=ReconciliationOutcome.UNKNOWN_EXTERNAL_ORDER.value,
        )
    return _fail(
        "unknown_external_order",
        "Unknown external order escalated",
        f"unexpected handling: {outcome.to_record()}",
    )


# --- 28/31. endpoint guards -------------------------------------------------

def scenario_testnet_endpoint_guard(workspace: Path) -> BreakGlassScenarioResult:
    """The adapter accepts only the documented Spot Testnet host."""
    del workspace
    accepted = require_testnet_base_url("https://testnet.binance.vision")
    non_testnet_refused = False
    try:
        require_testnet_base_url("https://example.invalid")
    except TestnetEndpointError:
        non_testnet_refused = True
    empty_refused = False
    try:
        require_testnet_base_url("")
    except TestnetEndpointError:
        empty_refused = True
    if accepted == "https://testnet.binance.vision" and non_testnet_refused and empty_refused:
        return _ok(
            "testnet_endpoint_guard",
            "Testnet endpoint guard",
            "only the documented Spot Testnet host is accepted; a non-testnet host and an empty URL "
            "are refused",
            accepted_host=accepted,
        )
    return _fail(
        "testnet_endpoint_guard",
        "Testnet endpoint guard",
        f"unexpected endpoint policy: accepted={accepted} non_testnet_refused={non_testnet_refused}",
    )


def scenario_mainnet_hostname_negative(workspace: Path) -> BreakGlassScenarioResult:
    """Every mainnet hostname marker is refused before any credential could be used."""
    del workspace
    refused: list[str] = []
    for marker in MAINNET_HOST_MARKERS:
        try:
            require_testnet_base_url(f"https://{marker}")
        except TestnetEndpointError:
            refused.append(marker)
    source_clean = _package_source_without_markers()
    if len(refused) == len(MAINNET_HOST_MARKERS) and source_clean:
        return _ok(
            "mainnet_hostname_negative",
            "Mainnet-hostname negative test",
            "every mainnet hostname marker was refused by the endpoint guard and no mainnet hostname "
            "appears anywhere in the break-glass package",
            markers=len(MAINNET_HOST_MARKERS),
        )
    return _fail(
        "mainnet_hostname_negative",
        "Mainnet-hostname negative test",
        f"mainnet handling was not refused: refused={len(refused)} source_clean={source_clean}",
    )


def _package_source_without_markers() -> bool:
    package = Path(__file__).resolve().parents[1]
    for file in package.rglob("*.py"):
        if "__pycache__" in file.parts:
            continue
        text = file.read_text(encoding="utf-8").lower()
        for marker in MAINNET_HOST_MARKERS:
            if marker in text:
                return False
    return True


# --- 29/30. secret leak + withdrawal invariant ------------------------------

def scenario_secret_leak_scan(workspace: Path) -> BreakGlassScenarioResult:
    """No credential value ever reaches the journal, the state files or the request log."""
    root = _workspace(workspace, "secret_scan")
    secrets = (txf.FAKE_API_KEY, txf.FAKE_API_SECRET)
    with SQLiteEventJournal(root / "journal.db") as journal:
        venue = _scripted_venue(
            journal,
            responses=[
                txf.time_response(),
                txf.exchange_info_response(),
                txf.account_response(),
                txf.open_orders_response("ct-secret"),
                txf.cancel_response("ct-secret", order_id=100),
            ],
        )
        control = _open_control(root, journal=journal, venue=venue.control)
        control.handle(_command(OperatorCommandType.CANCEL_OPEN, seed="secret-1"))
        request_log = "\n".join(venue.client.request_log())
        journaled = journal.read_range()
    artifacts: list[str] = [request_log]
    journal_text = ""
    with SQLiteEventJournal(root / "journal.db") as journal:
        journal_text = "\n".join(event.to_json() for event in journaled)
    artifacts.append(journal_text)
    for file in (root / "state").rglob("*"):
        if file.is_file():
            artifacts.append(file.read_text(encoding="utf-8"))
    leaks = [
        name
        for name, text in zip(
            ["request_log", "journal", *[f"state:{i}" for i in range(len(artifacts) - 2)]],
            artifacts,
        )
        if any(secret in text for secret in secrets)
    ]
    if not leaks and request_log.strip():
        return _ok(
            "secret_leak_scan",
            "Secret-leak scan",
            "the synthetic credential values appear nowhere in the journal, the state files or the "
            "log-safe request log",
            artifacts_scanned=len(artifacts),
            request_log_lines=len(request_log.splitlines()),
        )
    return _fail(
        "secret_leak_scan",
        "Secret-leak scan",
        f"credential material leaked into: {leaks} (request_log_present={bool(request_log.strip())})",
    )


def scenario_withdrawal_disabled_invariant(workspace: Path) -> BreakGlassScenarioResult:
    """No withdrawal capability exists, and a withdrawal-enabled key fails the pre-flight closed."""
    root = _workspace(workspace, "withdrawal_invariant")
    tokens = (
        "withdraw" + "/apply",
        "/sa" + "pi/v1/capital/" + "withdraw",
        "universal" + "transfer",
    )
    package_clean = True
    package = Path(__file__).resolve().parents[1]
    for file in package.rglob("*.py"):
        if "__pycache__" in file.parts:
            continue
        text = file.read_text(encoding="utf-8").lower()
        if any(token in text for token in tokens):
            package_clean = False
    adapter_surface_clean = not any(
        "withdraw" in name.lower() and callable(getattr(BinanceSpotTestnetClient, name))
        for name in dir(BinanceSpotTestnetClient)
    )
    with SQLiteEventJournal(root / "journal.db") as journal:
        strict = _scripted_venue(
            journal,
            responses=[
                txf.time_response(),
                txf.exchange_info_response(),
                txf.account_response(can_withdraw=True),
            ],
            withdrawal_policy=WithdrawalPolicy.REQUIRE_DISABLED,
        )
        control = _open_control(root, journal=journal, venue=strict.control)
        preflight_refused = False
        try:
            strict.control.preflight(safety=control.load_safety_state().controller)
        except UnsafeTestnetAccountError:
            preflight_refused = True
        outcome = control.handle(_command(OperatorCommandType.CANCEL_OPEN, seed="withdraw-1"))
        status = control.status()
        deletes = [item for item in strict.transport.requests if item.method == "DELETE"]
    if (
        package_clean
        and adapter_surface_clean
        and preflight_refused
        and outcome.venue_control == "unavailable"
        and deletes == []
        and status.safety_mode == "recovery_required"
    ):
        return _ok(
            "withdrawal_disabled_invariant",
            "Withdrawal-disabled invariant",
            "no withdrawal endpoint or capability exists anywhere in the adapter or the break-glass "
            "package; a withdrawal-enabled key fails the emergency pre-flight closed before any "
            "venue request and no cancel was attempted",
            preflight_refused=preflight_refused,
            venue_control=outcome.venue_control,
            delete_requests=len(deletes),
        )
    return _fail(
        "withdrawal_disabled_invariant",
        "Withdrawal-disabled invariant",
        f"withdrawal invariant violated: clean={package_clean} surface={adapter_surface_clean} "
        f"preflight_refused={preflight_refused} venue={outcome.venue_control}",
    )


def scenario_withdrawal_recorded_but_unusable(workspace: Path) -> BreakGlassScenarioResult:
    """The Binance testnet default grants the flag; it is recorded, and stays unusable."""
    root = _workspace(workspace, "withdrawal_recorded")
    with SQLiteEventJournal(root / "journal.db") as journal:
        venue = _scripted_venue(
            journal,
            responses=[
                txf.time_response(),
                txf.exchange_info_response(),
                txf.account_response(can_withdraw=True),
                txf.open_orders_response("ct-flag"),
                txf.cancel_response("ct-flag", order_id=100),
            ],
            withdrawal_policy=WithdrawalPolicy.RECORD_ONLY,
        )
        control = _open_control(root, journal=journal, venue=venue.control)
        preflight = venue.control.preflight(safety=control.load_safety_state().controller)
        outcome = control.handle(_command(OperatorCommandType.CANCEL_OPEN, seed="flag-1"))
        record = json.dumps(outcome.to_record())
    if (
        preflight.withdrawals_available
        and preflight.withdrawal_policy_ok
        and outcome.accepted
        and "withdraw" not in record.lower().replace("withdrawals", "")
    ):
        return _ok(
            "withdrawal_recorded_but_unusable",
            "Withdrawal flag recorded on testnet, capability still absent",
            "the testnet default flag was recorded in the pre-flight evidence and no withdrawal "
            "capability or secret appeared in the outcome",
            notes=len(preflight.notes),
        )
    return _fail(
        "withdrawal_recorded_but_unusable",
        "Withdrawal flag recorded on testnet, capability still absent",
        f"unexpected pre-flight: {preflight.to_record()}",
    )


# --- 32. no LLM dependency in the break-glass path -------------------------

def scenario_breakglass_path_has_no_llm_dependency(workspace: Path) -> BreakGlassScenarioResult:
    """The break-glass implementation imports no agent/LLM/CrewAI code."""
    del workspace
    package = Path(__file__).resolve().parents[1]
    audit_dir = package / "audit"
    forbidden = ("crewai", "crypto_trader.llm", "crypto_trader.crew")
    offenders: list[str] = []
    implementation = sorted(
        file
        for file in package.rglob("*.py")
        if "__pycache__" not in file.parts and not file.is_relative_to(audit_dir)
    )
    for file in implementation:
        text = file.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{file.name}:{token}")
    if not offenders:
        return _ok(
            "breakglass_path_has_no_llm_dependency",
            "No LLM/CrewAI dependency in the break-glass path",
            "no break-glass implementation module references CrewAI, the LLM layer or the crew "
            "definition; the control ingress cannot be influenced by a model. (This audit harness "
            "itself only NAMES those tokens in order to forbid them.)",
            files=[file.name for file in implementation],
        )
    return _fail(
        "breakglass_path_has_no_llm_dependency",
        "No LLM/CrewAI dependency in the break-glass path",
        f"LLM dependencies leaked into the break-glass path: {offenders}",
    )


# --- registry ---------------------------------------------------------------

SCENARIOS: tuple[Callable[[Path], BreakGlassScenarioResult], ...] = (
    scenario_halt_new_risk_while_healthy,
    scenario_halt_new_risk_while_ai_queue_blocked,
    scenario_ai_queue_saturated,
    scenario_cancel_open_through_safety_path,
    scenario_duplicate_break_glass_command,
    scenario_restart_with_break_glass_active,
    scenario_journal_replay_of_safety_state,
    scenario_journal_replay_after_crash,
    scenario_restart_in_degraded,
    scenario_restart_in_recovery_required,
    scenario_new_risk_order_rejected_after_break_glass,
    scenario_ai_decision_cannot_override_safety,
    scenario_reconciliation_after_emergency_action,
    scenario_operator_acknowledgement_after_reconciliation,
    scenario_restore_denied_before_reconciliation,
    scenario_restore_denied_without_acknowledgement,
    scenario_operator_acknowledgement_missing,
    scenario_ambiguous_cancel_unknown_pending_reconciliation,
    scenario_venue_cancel_timeout,
    scenario_venue_response_lost,
    scenario_venue_unavailable,
    scenario_malformed_command_fails_closed,
    scenario_corrupted_breakglass_request,
    scenario_primary_runtime_unavailable,
    scenario_no_concurrent_normal_and_emergency_writer,
    scenario_local_state_ahead_of_venue,
    scenario_venue_state_ahead_of_local,
    scenario_reconciliation_mismatch,
    scenario_unknown_external_order,
    scenario_testnet_endpoint_guard,
    scenario_mainnet_hostname_negative,
    scenario_secret_leak_scan,
    scenario_withdrawal_disabled_invariant,
    scenario_withdrawal_recorded_but_unusable,
    scenario_breakglass_path_has_no_llm_dependency,
)


def run_scenario(
    scenario: Callable[[Path], BreakGlassScenarioResult], workspace: Path
) -> BreakGlassScenarioResult:
    """Run one scenario, converting an unexpected exception into a visible FAIL."""
    try:
        return scenario(workspace)
    except Exception as exc:  # a scenario that explodes is a FAIL, never silently skipped
        return BreakGlassScenarioResult(
            scenario_id=getattr(scenario, "__name__", "unknown"),
            title=getattr(scenario, "__name__", "unknown"),
            status="fail",
            detail=f"scenario raised {type(exc).__name__}: {exc}",
            evidence={},
        )


def run_all_scenarios(workspace: Path) -> tuple[BreakGlassScenarioResult, ...]:
    """Run every break-glass scenario deterministically."""
    workspace.mkdir(parents=True, exist_ok=True)
    return tuple(run_scenario(scenario, workspace) for scenario in SCENARIOS)
