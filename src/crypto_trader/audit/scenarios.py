"""Deterministic fault-injection scenarios for the live-readiness audit (Phase 12).

Every scenario runs OFFLINE against the real components (journal, validity, risk, safety, recovery,
paper session and the testnet adapter with a scripted transport). No mainnet, no real capital, no
network, and no mocking of our own decision logic - a scenario only injects the fault.

Each scenario returns a :class:`ScenarioResult` whose ``status`` is ``pass`` only when the system
behaved fail-closed exactly as designed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from crypto_trader.events import (
    EventRegistry,
    ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE,
    StrategyDecisionPayload,
    StrategyDecisionRecorded,
    UnsupportedSchemaVersionError,
    UnknownEventTypeError,
    build_default_registry,
    decode_event,
)
from crypto_trader.execution import (
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionSide,
    ExecutionTimeInForce,
    InstrumentSpec,
)
from crypto_trader.execution.binance_testnet import (
    AmbiguousOrderError,
    ScriptedTransport,
    TransportTimeout,
)
from crypto_trader.paper import ScriptedSimulationRunner, TradingMode
from crypto_trader.persistence import (
    EventIdConflictError,
    IdempotentConsumer,
    SQLiteEventJournal,
    SnapshotRecord,
    rebuild_state,
)
from crypto_trader.recovery import (
    LocalOrderState,
    OrderStateName,
    ReconciliationEngine,
    ReconciliationOutcome,
    VenueOrderReport,
    load_policy as load_recovery_policy,
)
from crypto_trader.risk import HardRiskEngine, RiskRequest, RiskStateSnapshot, Side, load_limits
from crypto_trader.safety import (
    LaneCommand,
    LanePriority,
    PrioritySafetyLane,
    SafetyController,
    SafetyMode,
    record_safety_state,
    restore_safety_controller,
)
from crypto_trader.tests import paper_fixtures as fx
from crypto_trader.tests import testnet_fixtures as txf
from crypto_trader.validity import (
    BoundedPendingIntents,
    DecisionValidityGate,
    load_policy as load_validity_policy,
)

from .errors import ScenarioError

__all__ = ["ScenarioResult", "SCENARIOS", "run_all_scenarios", "run_scenario"]

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
CORRELATION = fx.SESSION_ID

LIMITS = {
    "limits_version": "risk-audit-v1",
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

VALIDITY = {"policy_id": "validity-audit-v1", "max_age_seconds": 30, "max_market_move_bps": "50"}
RECOVERY = {"policy_id": "recovery-audit-v1"}


@dataclass(frozen=True)
class ScenarioResult:
    """The outcome of one fault-injection scenario."""

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
            "evidence": dict(self.evidence),
        }


def _ok(scenario_id: str, title: str, detail: str, **evidence: Any) -> ScenarioResult:
    return ScenarioResult(scenario_id, title, "pass", detail, evidence)


def _fail(scenario_id: str, title: str, detail: str, **evidence: Any) -> ScenarioResult:
    return ScenarioResult(scenario_id, title, "fail", detail, evidence)


# --- 1. process restart + journal replay -----------------------------------

def scenario_restart_journal_replay(workspace: Path) -> ScenarioResult:
    """A restart must rebuild identical state from snapshot + journal tail."""
    with SQLiteEventJournal(workspace / "restart.db") as journal:
        events = [
            StrategyDecisionRecorded(
                occurred_at=BASE + timedelta(seconds=index),
                correlation_id=CORRELATION,
                producer="agent.trading_plan_strategist",
                symbol="ETHUSDT",
                payload=StrategyDecisionPayload(
                    action="buy", confidence="medium", rationale=f"audit event {index}"
                ),
            )
            for index in range(6)
        ]
        journal.append_batch(events)

        class Counting(IdempotentConsumer):
            def __init__(self) -> None:
                super().__init__()
                self.count = 0

            def apply(self, event) -> None:
                self.count += 1

        first = Counting()
        rebuild_state(journal, first)
        snapshot = SnapshotRecord(
            name="audit",
            snapshot_schema=1,
            state={},
            last_event_id=events[2].event_id,
            last_occurred_at_ns=int(events[2].occurred_at.timestamp() * 1_000_000_000),
            last_sequence_number=None,
            created_at_iso=BASE.isoformat(),
        )
        second = Counting()
        summary = rebuild_state(journal, second, snapshot=snapshot)

    if first.count == 6 and summary.events_seen == 3 and second.count == 3:
        return _ok(
            "restart_journal_replay",
            "Process restart + journal replay",
            "full replay applied 6 events; snapshot + tail replayed exactly the 3-event tail",
            full_replay_events=first.count,
            tail_events=summary.events_seen,
            tail_applied=second.count,
        )
    return _fail(
        "restart_journal_replay",
        "Process restart + journal replay",
        f"unexpected replay counts: full={first.count} tail_seen={summary.events_seen}",
    )


# --- 2. duplicate event delivery -------------------------------------------

def scenario_duplicate_event_delivery(workspace: Path) -> ScenarioResult:
    """At-least-once delivery must not double-apply effects."""
    with SQLiteEventJournal(workspace / "duplicates.db") as journal:
        event = StrategyDecisionRecorded(
            occurred_at=BASE,
            correlation_id=CORRELATION,
            producer="agent.trading_plan_strategist",
            symbol="ETHUSDT",
            payload=StrategyDecisionPayload(action="buy", confidence="low", rationale="dup"),
        )
        first_insert = journal.append(event)
        second_insert = journal.append(event)
        conflict_raised = False
        try:
            journal.append(
                StrategyDecisionRecorded(
                    event_id=event.event_id,
                    occurred_at=BASE,
                    correlation_id=CORRELATION,
                    producer="agent.trading_plan_strategist",
                    symbol="ETHUSDT",
                    payload=StrategyDecisionPayload(action="sell", confidence="low", rationale="x"),
                )
            )
        except EventIdConflictError:
            conflict_raised = True
        stored = journal.count()

    if first_insert and not second_insert and stored == 1 and conflict_raised:
        return _ok(
            "duplicate_event_delivery",
            "Duplicate event delivery",
            "identical re-delivery was a no-op; different content under the same id failed closed",
            stored_rows=stored,
            conflicting_content_rejected=conflict_raised,
        )
    return _fail(
        "duplicate_event_delivery",
        "Duplicate event delivery",
        f"unexpected idempotency behaviour (stored={stored}, conflict={conflict_raised})",
    )


# --- 3. stale AI response --------------------------------------------------

def scenario_stale_ai_response(workspace: Path) -> ScenarioResult:
    """A decision that arrives after the freshness window must be STALE, never executed."""
    del workspace
    gate = DecisionValidityGate(load_validity_policy(VALIDITY))
    market_state = fx.validity_state(fx.feed("2700.00"))
    late = BASE + timedelta(seconds=120)
    decision = gate.evaluate(
        fx.validity_request(decided_at=late),
        market_state=market_state,
        now=late,
    )
    if decision.disposition.value == "stale":
        return _ok(
            "stale_ai_response",
            "Stale AI response",
            "a decision 120s past the original observation was STALE and did not proceed",
            disposition=decision.disposition.value,
            reason_code=decision.reason_code,
            decision_age_seconds=decision.decision_age_seconds,
        )
    return _fail(
        "stale_ai_response",
        "Stale AI response",
        f"expected stale, got {decision.disposition.value} ({decision.reason_code})",
    )


# --- 4/5. slow AI worker + backpressure ------------------------------------

def scenario_backpressure_exhaustion(workspace: Path) -> ScenarioResult:
    """A bounded queue must reject rather than let a stale intent wait indefinitely."""
    del workspace
    policy = load_validity_policy(VALIDITY)
    queue = BoundedPendingIntents(max_size=2)
    accepted = [queue.offer(fx.validity_request(), enqueued_at=BASE) for _ in range(2)]
    rejected = queue.offer(fx.validity_request(), enqueued_at=BASE)
    evicted = queue.evict_expired(now=BASE + timedelta(seconds=600), policy=policy)
    if (
        all(flag for flag, _ in accepted)
        and rejected == (False, "bounded_capacity_exhausted")
        and len(evicted) == 2
        and len(queue) == 0
    ):
        return _ok(
            "backpressure_exhaustion",
            "Backpressure exhaustion (slow AI worker)",
            "bounded queue rejected overflow and evicted intents older than the policy",
            capacity=queue.max_size,
            rejected_reason=rejected[1],
            evicted=int(len(evicted)),
        )
    return _fail(
        "backpressure_exhaustion",
        "Backpressure exhaustion (slow AI worker)",
        f"unexpected queue behaviour: accepted={accepted} rejected={rejected} evicted={len(evicted)}",
    )


# --- 6. risk breach --------------------------------------------------------

def scenario_risk_breach_halts_new_risk(workspace: Path) -> ScenarioResult:
    """A limit breach must reject the intent and request halt_new_risk only (never liquidation)."""
    del workspace
    engine = HardRiskEngine(load_limits(LIMITS))
    request = RiskRequest(
        correlation_id=CORRELATION,
        symbol="ETHUSDT",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("2700"),
        strategy_id="audit",
        intent_id=uuid4(),
    )
    state = RiskStateSnapshot(
        equity_quote=Decimal("10000"),
        peak_equity_quote=Decimal("10000"),
        realized_pnl_quote=Decimal("0"),
        gross_exposure_quote=Decimal("0"),
        net_exposure_quote=Decimal("0"),
        open_orders=0,
        open_positions=0,
        snapshot_ts=BASE,
    )
    decision = engine.evaluate(request, state, now=BASE)
    loss_state = RiskStateSnapshot(
        equity_quote=Decimal("8000"),
        peak_equity_quote=Decimal("10000"),
        realized_pnl_quote=Decimal("-1500"),
        gross_exposure_quote=Decimal("0"),
        net_exposure_quote=Decimal("0"),
        open_orders=0,
        open_positions=0,
        snapshot_ts=BASE,
    )
    loss_decision = engine.evaluate(request, loss_state, now=BASE)
    if (
        decision.allowed is False
        and "max_order_notional_exceeded" in decision.reason_codes
        and loss_decision.risk_actions == ["halt_new_risk"]
        and not any("liquidat" in code for code in loss_decision.reason_codes)
    ):
        return _ok(
            "risk_breach",
            "Deterministic risk breach",
            "notional breach rejected; loss breach requested halt_new_risk only (no liquidation)",
            reason_codes=list(decision.reason_codes),
            loss_actions=list(loss_decision.risk_actions),
        )
    return _fail(
        "risk_breach",
        "Deterministic risk breach",
        f"unexpected risk outcome: {decision.reason_codes} / {loss_decision.risk_actions}",
    )


# --- 7. unknown order result (ambiguous submission) ------------------------

def scenario_unknown_order_result(workspace: Path) -> ScenarioResult:
    """An ambiguous submit must become UNKNOWN_PENDING_RECONCILIATION and block new risk."""
    engine = ReconciliationEngine(load_recovery_policy(RECOVERY))
    local = LocalOrderState(
        order_id=uuid4(),
        order_reference="ct-ambiguous",
        intent_id=uuid4(),
        correlation_id=CORRELATION,
        symbol="ETHUSDT",
        state=OrderStateName.SUBMITTED,
        submission_attempted=True,
    )
    report = engine.reconcile([local], [])
    unresolved = report.unresolved_orders
    del workspace
    if (
        report.outcome_for("ct-ambiguous") is ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION
        and report.requires_halt_new_risk
        and report.adjustments == []
        and unresolved == ["ct-ambiguous"]
    ):
        return _ok(
            "unknown_order_result",
            "Unknown order result (ambiguous submit)",
            "ambiguous order stayed unresolved, blocked new risk and was NOT marked failed",
            outcome=report.outcome_for("ct-ambiguous").value,
            unresolved=unresolved,
        )
    return _fail(
        "unknown_order_result",
        "Unknown order result (ambiguous submit)",
        f"unexpected reconciliation outcome: {report.outcomes}",
    )


# --- 8. cancel failure -----------------------------------------------------

def _audit_runtime_instrument() -> InstrumentSpec:
    """The execution instrument the audit scenarios drive through the production runtime."""
    return InstrumentSpec(
        symbol=txf.SYMBOL, base_currency="BTC", starting_balance="100000 USDT"
    )


def _audit_execution_request(intent_id) -> ExecutionRequest:
    """The order the audit scenarios submit, identical to the previous fixture request."""
    return ExecutionRequest.create(
        correlation_id=uuid4(),
        symbol=txf.SYMBOL,
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("0.00100000"),
        limit_price=Decimal("27000.00"),
        time_in_force=ExecutionTimeInForce.GTC,
        intent_id=intent_id,
    )


def scenario_cancel_failure(workspace: Path) -> ScenarioResult:
    """A cancel that cannot be confirmed must stay ambiguous, never be retried blindly."""
    from crypto_trader.execution.binance_testnet import (
        BinanceSpotTestnetClient,
        TestnetClock,
        TestnetOrderRuntime,
        WithdrawalPolicy,
    )

    workspace.mkdir(parents=True, exist_ok=True)
    intent_id = uuid4()
    client_order_id = f"ct-{intent_id.hex[:20]}"
    transport = ScriptedTransport(
        responses=[
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.order_response(client_order_id, status="NEW"),
            TransportTimeout("cancel response lost"),
        ]
    )
    client = BinanceSpotTestnetClient(
        credentials=txf.credentials(),
        transport=transport,
        clock=TestnetClock(time_source=lambda: txf.SERVER_TIME_MS / 1000),
        symbol=txf.SYMBOL,
        withdrawal_policy=WithdrawalPolicy.RECORD_ONLY,
    )
    # Stage 2: the fresh order is created through the production runtime path (prepare -> durable
    # attempt marker -> one send) with a temporary journal inside the scenario workspace. The
    # audit never authorizes a prepared order or calls the client's write boundary directly.
    with SQLiteEventJournal(workspace / "cancel_failure_journal.db") as journal:
        runtime = TestnetOrderRuntime(
            client=client, instrument=_audit_runtime_instrument(), journal=journal
        )
        runtime.start()
        runtime.submit(_audit_execution_request(intent_id))
        markers = [
            event
            for event in journal.read_range()
            if event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE
        ]
        attempt_marker_present = (
            len(markers) == 1 and markers[0].payload.venue_reference == client_order_id
        )
    ambiguous = False
    try:
        client.cancel_order(txf.SYMBOL, client_order_id)
    except AmbiguousOrderError:
        ambiguous = True
    deletes = [item for item in transport.requests if item.method == "DELETE"]
    posts = [item for item in transport.requests if item.method == "POST"]
    if ambiguous and len(deletes) == 1 and attempt_marker_present and len(posts) == 1:
        return _ok(
            "cancel_failure",
            "Cancel failure",
            "the order was created through the runtime (durable attempt marker, one POST) and the "
            "unconfirmable cancel raised AmbiguousOrderError and was sent exactly once",
            delete_requests=len(deletes),
            posts=len(posts),
            attempt_marker=attempt_marker_present,
        )
    return _fail(
        "cancel_failure",
        "Cancel failure",
        f"unexpected cancel behaviour: ambiguous={ambiguous} deletes={len(deletes)} "
        f"posts={len(posts)} attempt_marker={attempt_marker_present}",
    )


# --- 9. network interruption ----------------------------------------------

def scenario_network_interruption(workspace: Path) -> ScenarioResult:
    """A network interruption on a WRITE must never be retried; on a READ it may retry."""
    from crypto_trader.execution.binance_testnet import (
        BinanceSpotTestnetClient,
        TestnetClock,
        TestnetOrderRuntime,
        TransportError,
        WithdrawalPolicy,
    )

    workspace.mkdir(parents=True, exist_ok=True)
    intent_id = uuid4()
    write_transport = ScriptedTransport(
        responses=[
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            TransportTimeout("link down"),
        ]
    )
    write_client = BinanceSpotTestnetClient(
        credentials=txf.credentials(),
        transport=write_transport,
        clock=TestnetClock(time_source=lambda: txf.SERVER_TIME_MS / 1000),
        symbol=txf.SYMBOL,
        withdrawal_policy=WithdrawalPolicy.RECORD_ONLY,
    )
    write_ambiguous = False
    # Stage 2: the interrupted write traverses the SAME runtime -> client -> transport.send path.
    with SQLiteEventJournal(workspace / "network_interruption_journal.db") as journal:
        write_runtime = TestnetOrderRuntime(
            client=write_client, instrument=_audit_runtime_instrument(), journal=journal
        )
        write_runtime.start()
        try:
            write_runtime.submit(_audit_execution_request(intent_id))
        except AmbiguousOrderError:
            write_ambiguous = True
        attempt_marker_present = any(
            event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE
            for event in journal.read_range()
        )
    write_posts = [item for item in write_transport.requests if item.method == "POST"]

    read_transport = ScriptedTransport(
        responses=[TransportError("link down"), txf.price_response()]
    )
    read_client = BinanceSpotTestnetClient(
        credentials=txf.credentials(),
        transport=read_transport,
        clock=TestnetClock(time_source=lambda: txf.SERVER_TIME_MS / 1000),
        symbol=txf.SYMBOL,
        withdrawal_policy=WithdrawalPolicy.RECORD_ONLY,
    )
    price = read_client.price()
    if (
        write_ambiguous
        and len(write_posts) == 1
        and attempt_marker_present
        and price == Decimal("27000.00")
    ):
        return _ok(
            "network_interruption",
            "Network interruption simulation",
            "write interruption was ambiguous, sent once and left a durable attempt marker; read "
            "interruption retried and recovered",
            write_posts=len(write_posts),
            read_attempts=len(read_transport.requests),
            attempt_marker=attempt_marker_present,
        )
    return _fail(
        "network_interruption",
        "Network interruption simulation",
        f"unexpected interruption behaviour: ambiguous={write_ambiguous} posts={len(write_posts)}",
    )


# --- 10. partial fill + restart -------------------------------------------

def scenario_partial_fill_then_restart(workspace: Path) -> ScenarioResult:
    """A partial fill that happened during downtime must be recovered from venue evidence."""
    del workspace
    engine = ReconciliationEngine(load_recovery_policy(RECOVERY))
    local = [
        LocalOrderState(
            order_id=uuid4(),
            order_reference="ct-partial",
            intent_id=uuid4(),
            correlation_id=CORRELATION,
            symbol="ETHUSDT",
            state=OrderStateName.ACCEPTED,
            remaining_quantity=Decimal("1"),
            submission_attempted=True,
        )
    ]
    venue = [
        VenueOrderReport(
            order_reference="ct-partial",
            venue_order_id="V-1",
            state=OrderStateName.PARTIALLY_FILLED,
            filled_quantity=Decimal("0.4"),
            remaining_quantity=Decimal("0.6"),
            reported_at=BASE,
        )
    ]
    report = engine.reconcile(local, venue)
    adjustment = report.adjustments[0] if report.adjustments else None
    if (
        report.outcome_for("ct-partial") is ReconciliationOutcome.RESOLVED_FROM_VENUE
        and adjustment is not None
        and adjustment.after.filled_quantity == Decimal("0.4")
        and adjustment.source.value == "venue_report"
        and not report.requires_halt_new_risk
    ):
        return _ok(
            "partial_fill_then_restart",
            "Partial fill during downtime + restart",
            "local view was corrected from venue evidence with explicit provenance",
            filled_after=str(adjustment.after.filled_quantity),
            source=adjustment.source.value,
        )
    return _fail(
        "partial_fill_then_restart",
        "Partial fill during downtime + restart",
        f"unexpected reconciliation: {report.outcomes}",
    )


# --- 11. reconciliation mismatch ------------------------------------------

def scenario_reconciliation_mismatch(workspace: Path) -> ScenarioResult:
    """Local state ahead of the venue must block new risk and never overwrite silently."""
    del workspace
    engine = ReconciliationEngine(load_recovery_policy(RECOVERY))
    local = [
        LocalOrderState(
            order_id=uuid4(),
            order_reference="ct-mismatch",
            intent_id=uuid4(),
            correlation_id=CORRELATION,
            symbol="ETHUSDT",
            state=OrderStateName.FILLED,
            filled_quantity=Decimal("1"),
            submission_attempted=True,
        )
    ]
    venue = [
        VenueOrderReport(
            order_reference="ct-mismatch",
            venue_order_id="V-2",
            state=OrderStateName.ACCEPTED,
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("1"),
            reported_at=BASE,
        )
    ]
    report = engine.reconcile(local, venue)
    if (
        report.outcome_for("ct-mismatch") is ReconciliationOutcome.LOCAL_AHEAD_OF_VENUE
        and report.requires_halt_new_risk
        and report.adjustments == []
    ):
        return _ok(
            "reconciliation_mismatch",
            "Reconciliation mismatch (local ahead)",
            "mismatch kept the local state intact, produced no adjustment and blocked new risk",
            unresolved=report.unresolved_orders,
        )
    return _fail(
        "reconciliation_mismatch",
        "Reconciliation mismatch (local ahead)",
        f"unexpected mismatch handling: {report.outcomes}",
    )


# --- 12. kill switch while loaded -----------------------------------------

def scenario_kill_switch_while_loaded(workspace: Path) -> ScenarioResult:
    """A kill switch must traverse the priority lane ahead of a saturated AI backlog."""
    del workspace
    lane = PrioritySafetyLane(max_normal_depth=3)
    for index in range(3):
        lane.submit(
            LaneCommand(command_id=uuid4(), kind=f"ai_intent_{index}", priority=LanePriority.NORMAL)
        )
    controller = SafetyController(correlation_id=CORRELATION)
    transition = controller.engage_kill_switch(
        reason_code="audit_kill", reason="audit kill switch under load"
    )
    cancel = LaneCommand(command_id=uuid4(), kind="cancel_all", priority=LanePriority.SAFETY)
    lane.submit(cancel)
    first = lane.next_command()
    if (
        first is cancel
        and controller.allow_new_risk() is False
        and transition.changed
        and lane.normal_depth == 3
    ):
        return _ok(
            "kill_switch_while_loaded",
            "Kill switch while the AI queue is loaded",
            "the safety command drained before the saturated AI backlog and new risk was refused",
            safety_first=True,
            normal_backlog=3,
        )
    return _fail(
        "kill_switch_while_loaded",
        "Kill switch while the AI queue is loaded",
        "the safety command did not take priority over the AI backlog",
    )


# --- 13. shutdown/restart while halted ------------------------------------

def scenario_restart_while_halted(workspace: Path) -> ScenarioResult:
    """A halted system must come back halted, and the transition must be journaled."""
    with SQLiteEventJournal(workspace / "halted.db") as journal:
        controller = SafetyController(correlation_id=CORRELATION)
        transition = controller.engage_kill_switch(
            reason_code="audit_kill", reason="audit halt before restart"
        )
        record_safety_state(journal, transition, occurred_at=BASE)
        restored = restore_safety_controller(controller.to_persisted())
        journaled = [event.payload.mode for event in journal.read_range()]
        corrupt = restore_safety_controller({"unexpected": "shape"})

    if (
        restored.mode is SafetyMode.HALT_NEW_RISK
        and restored.kill_switch_engaged
        and not restored.allow_new_risk()
        and journaled == ["halt_new_risk"]
        and corrupt.mode is SafetyMode.RECOVERY_REQUIRED
    ):
        return _ok(
            "shutdown_restart_while_halted",
            "Shutdown/restart while halted",
            "halt survived the restart, was journaled, and corrupt state restored fail-closed",
            journaled_modes=journaled,
            corrupt_restore_mode=corrupt.mode.value,
        )
    return _fail(
        "shutdown_restart_while_halted",
        "Shutdown/restart while halted",
        f"unexpected safety restore: {restored.mode} journaled={journaled}",
    )


# --- 14. corrupt / invalid event rejection --------------------------------

def scenario_corrupt_event_rejection(workspace: Path) -> ScenarioResult:
    """Malformed payloads must be rejected visibly, never guessed at."""
    del workspace
    registry: EventRegistry = build_default_registry()
    corrupt_payload = (
        '{"event_id":"11111111-1111-4111-8111-111111111111","event_type":"order.state.changed",'
        '"schema_version":1,"occurred_at":"2026-01-01T12:00:00Z",'
        '"correlation_id":"22222222-2222-4222-8222-222222222222",'
        '"producer":"infrastructure.binance_testnet","symbol":"ETHUSDT","payload":{'
        '"state":"definitely_not_a_state"}}'
    )
    rejected = False
    try:
        decode_event(corrupt_payload, registry=registry)
    except Exception as exc:  # EventValidationError from pydantic validation
        rejected = type(exc).__name__ == "EventValidationError"
    unsupported = False
    try:
        decode_event(
            '{"event_id":"11111111-1111-4111-8111-111111111111",'
            '"event_type":"strategy.decision.recorded","schema_version":99,'
            '"occurred_at":"2026-01-01T12:00:00Z",'
            '"correlation_id":"22222222-2222-4222-8222-222222222222",'
            '"producer":"agent.trading_plan_strategist","payload":{}}',
            registry=registry,
        )
    except UnsupportedSchemaVersionError:
        unsupported = True
    unknown = False
    try:
        decode_event(
            '{"event_id":"11111111-1111-4111-8111-111111111111",'
            '"event_type":"not.a.contract","schema_version":1,'
            '"occurred_at":"2026-01-01T12:00:00Z",'
            '"correlation_id":"22222222-2222-4222-8222-222222222222",'
            '"producer":"agent.trading_plan_strategist","payload":{}}',
            registry=registry,
        )
    except UnknownEventTypeError:
        unknown = True
    if rejected and unsupported and unknown:
        return _ok(
            "corrupt_event_rejection",
            "Corrupt / invalid event rejection",
            "invalid state rejected; unsupported schema version rejected; unknown type rejected",
            validation_rejected=rejected,
            unknown_schema_rejected=unsupported,
            unknown_type_rejected=unknown,
        )
    return _fail(
        "corrupt_event_rejection",
        "Corrupt / invalid event rejection",
        f"rejections incomplete: validation={rejected} schema={unsupported} type={unknown}",
    )


# --- 15. paper + shadow end-to-end ---------------------------------------

def scenario_paper_pipeline_end_to_end(workspace: Path) -> ScenarioResult:
    """The full paper pipeline must produce a deterministic, journaled, secret-free report."""
    with SQLiteEventJournal(workspace / "paper.db") as journal:
        session = fx.session(
            journal,
            mode=TradingMode.PAPER,
            decisions=fx.scripted(fx.buy_decision(quantity="1"), fx.hold_decision()),
            data_feed=fx.feed("2700.00", "2701.00"),
            simulator=ScriptedSimulationRunner(),
        )
        report = session.run()
        types = {event.event_type for event in journal.read_range()}
        payload = report.to_json()

    required = {
        "strategy.decision.recorded",
        "trade.intent.created",
        "validity.disposition.recorded",
        "risk.decision.recorded",
        "risk.evaluation.recorded",
        "order.state.changed",
        "execution.fill.recorded",
    }
    if (
        required <= types
        and report.observations_processed == 2
        and len(report.orders) == 1
        and len(report.fills) == 1
        and "api_key" not in payload.lower()
    ):
        return _ok(
            "paper_pipeline_end_to_end",
            "Paper pipeline end to end",
            "decisions -> validity -> risk -> simulated execution -> fills -> report, fully journaled",
            event_types=sorted(types),
            fills=len(report.fills),
            semantic_hash=report.semantic_hash(),
        )
    return _fail(
        "paper_pipeline_end_to_end",
        "Paper pipeline end to end",
        f"pipeline incomplete: types={sorted(types)}",
    )


# --- 16. Nautilus queue-aware simulated execution (offline engine path) ----

def scenario_nautilus_queue_aware_execution(workspace: Path) -> ScenarioResult:
    """The real engine path must run queue-aware and produce a deterministic fill, offline."""
    from decimal import Decimal as _Decimal

    from crypto_trader.execution import (
        BookLevel,
        InstrumentSpec,
        MarketBookUpdate,
        MarketTrade,
    )
    from crypto_trader.execution.nautilus import NautilusSimulationRunner, QueueAwareSettings

    price = _Decimal("2700.00")
    base_ns = 1_767_225_600_000_000_000
    ms = 1_000_000
    book = MarketBookUpdate(
        levels=[
            BookLevel(side="BUY", price=price, size=_Decimal("100.00000000"), order_id=1),
            BookLevel(
                side="SELL",
                price=_Decimal("2701.00"),
                size=_Decimal("100.00000000"),
                order_id=2,
            ),
        ],
        ts_ns=base_ns,
        sequence=1,
    )
    trades = (
        MarketTrade(
            price=price,
            size=_Decimal("100.00000000"),
            aggressor="SELLER",
            ts_ns=base_ns + 10 * ms,
            trade_id="AUD-1",
        ),
        MarketTrade(
            price=price,
            size=_Decimal("1.00000000"),
            aggressor="SELLER",
            ts_ns=base_ns + 20 * ms,
            trade_id="AUD-2",
        ),
    )
    instrument = InstrumentSpec(symbol="ETHUSDT")
    runner = NautilusSimulationRunner(
        instrument=instrument,
        book_updates=(book,),
        trades=trades,
        queue_aware=True,
        queue_settings=QueueAwareSettings(True, True, "L2_MBP"),
    )
    request = ExecutionRequest.create(
        correlation_id=CORRELATION,
        symbol="ETHUSDT",
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=_Decimal("1.00000000"),
        limit_price=price,
    )
    outcome = runner.simulate((request,))
    fills = [item for item in outcome.observations if item.last_fill_quantity is not None]
    if (
        outcome.queue_aware
        and outcome.book_type == "L2_MBP"
        and outcome.engine_version == "1.231.0"
        and fills
        and fills[-1].remaining_quantity == _Decimal("0.00000000")
        and all(item.correlation_id == CORRELATION for item in outcome.observations)
    ):
        return _ok(
            "nautilus_queue_aware_execution",
            "Nautilus queue-aware simulated execution",
            "offline queue-aware run (queue_position+liquidity_consumption+L2_MBP) filled the order",
            engine_version=outcome.engine_version,
            book_type=outcome.book_type,
            fills=len(fills),
        )
    return _fail(
        "nautilus_queue_aware_execution",
        "Nautilus queue-aware simulated execution",
        f"unexpected engine outcome: queue_aware={outcome.queue_aware} fills={len(fills)}",
    )


SCENARIOS: tuple[Callable[[Path], ScenarioResult], ...] = (
    scenario_restart_journal_replay,
    scenario_duplicate_event_delivery,
    scenario_stale_ai_response,
    scenario_backpressure_exhaustion,
    scenario_risk_breach_halts_new_risk,
    scenario_unknown_order_result,
    scenario_cancel_failure,
    scenario_network_interruption,
    scenario_partial_fill_then_restart,
    scenario_reconciliation_mismatch,
    scenario_kill_switch_while_loaded,
    scenario_restart_while_halted,
    scenario_corrupt_event_rejection,
    scenario_paper_pipeline_end_to_end,
    scenario_nautilus_queue_aware_execution,
)


def run_scenario(scenario: Callable[[Path], ScenarioResult], workspace: Path) -> ScenarioResult:
    """Run one scenario, converting an unexpected exception into a visible FAIL."""
    try:
        return scenario(workspace)
    except Exception as exc:  # a scenario that explodes is a FAIL, never silently skipped
        return ScenarioResult(
            scenario_id=getattr(scenario, "__name__", "unknown"),
            title=getattr(scenario, "__name__", "unknown"),
            status="fail",
            detail=f"scenario raised {type(exc).__name__}: {exc}",
            evidence={},
        )


def run_all_scenarios(workspace: Path) -> tuple[ScenarioResult, ...]:
    """Run every fault-injection scenario deterministically."""
    workspace.mkdir(parents=True, exist_ok=True)
    return tuple(run_scenario(scenario, workspace) for scenario in SCENARIOS)
