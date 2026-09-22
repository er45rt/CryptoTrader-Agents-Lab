"""Phase 13: the operational break-glass control path (deterministic, idempotent, fail-closed)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest

from crypto_trader.breakglass import (
    BreakGlassActiveError,
    BreakGlassControl,
    OperatorCommandType,
    OwnerKind,
    parse_operator_command,
)
from crypto_trader.events import OPERATOR_COMMAND_EVENT_TYPE, SAFETY_EVENT_TYPE
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.recovery import load_policy as load_recovery_policy
from crypto_trader.risk import HardRiskEngine, RiskRequest, RiskStateSnapshot, Side, load_limits
from crypto_trader.safety import SafetyMode

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NAMESPACE = UUID("4d9b6f2e-6b25-4b7f-9d3c-1f5a8e0c2b47")

LIMITS = {
    "limits_version": "risk-breakglass-test-v1",
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


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as active:
        yield active


@pytest.fixture
def control(tmp_path, journal) -> BreakGlassControl:
    return BreakGlassControl(
        journal=journal,
        state_dir=tmp_path / "state",
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )


def command(command_type, *, seed: str | None = None, requested_at: datetime = BASE, **overrides):
    payload = {
        "command_type": command_type,
        "command_id": str(uuid5(NAMESPACE, seed or command_type)),
        "operator_label": "jamal",
        "reason": "test reason",
        "source": "operator-cli",
        "requested_at": requested_at.isoformat(),
    }
    payload.update(overrides)
    return parse_operator_command(payload, delivered_at=requested_at)


# --- actions ---------------------------------------------------------------

def test_halt_engages_the_kill_switch_and_refuses_new_risk(control):
    outcome = control.handle(command("halt_new_risk"))
    status = control.status()

    assert outcome.accepted
    assert outcome.previous_safety_state == "normal"
    assert outcome.resulting_safety_state == "halt_new_risk"
    assert status.kill_switch_engaged is True
    assert status.new_risk_permitted is False
    assert status.epoch_open is True
    assert status.epoch_escalation_path == ("normal", "halt_new_risk")


def test_reduce_only_is_a_policy_mode_and_never_invents_an_exchange_flag(control):
    outcome = control.handle(command("reduce_only"))
    status = control.status()

    assert outcome.resulting_safety_state == "reduce_only"
    assert status.new_risk_permitted is False
    assert status.kill_switch_engaged is False
    assert outcome.safety_transitions == ("normal->reduce_only",)


def test_break_glass_actions_never_downgrade_the_safety_ladder(control):
    control.handle(command("force_recovery_required"))
    denied = control.handle(command("reduce_only", seed="downgrade"))
    status = control.status()

    assert denied.disposition == "rejected"
    assert denied.reason_code == "downgrade_refused_monotonic_escalation"
    assert status.safety_mode == "recovery_required"
    assert status.new_risk_permitted is False


def test_cancel_open_without_a_venue_is_policy_only_and_still_requires_reconciliation(control):
    outcome = control.handle(command("cancel_open"))

    assert outcome.accepted
    assert outcome.venue_control == "not_requested"
    assert outcome.reconciliation_status == "pending"
    assert outcome.reconciliation_required is True


def test_every_action_is_journaled_through_the_additive_contract(control, journal):
    outcome = control.handle(command("enter_degraded"))
    events = journal.read_range()
    safety_events = [event for event in events if event.event_type == SAFETY_EVENT_TYPE]
    command_events = [
        event for event in events if event.event_type == OPERATOR_COMMAND_EVENT_TYPE
    ]

    assert len(safety_events) >= 1
    assert len(command_events) == 1
    payload = command_events[0].payload
    assert payload.command_id == outcome.command_id
    assert payload.disposition == "accepted"
    assert payload.previous_safety_state == "normal"
    assert payload.resulting_safety_state == "degraded"
    assert payload.command_digest == command("enter_degraded").digest
    assert command_events[0].correlation_id == outcome.correlation_id
    assert command_events[0].causation_id is None


def test_restore_requires_reconciliation_and_acknowledgement(control):
    control.handle(command("halt_new_risk", seed="gates-halt"))
    early = control.handle(command("restore_normal", seed="gates-restore-1"))
    assert early.reason_code == "restore_denied_reconciliation_pending"

    reconcile = control.handle(command("reconcile", seed="gates-reconcile"))
    assert reconcile.reconciliation_status == "resolved"

    still_missing = control.handle(command("restore_normal", seed="gates-restore-2"))
    assert still_missing.reason_code == "restore_denied_acknowledgement_missing"

    acknowledgement = control.handle(command("acknowledge", seed="gates-ack"))
    assert acknowledgement.disposition == "accepted"

    restored = control.handle(command("restore_normal", seed="gates-restore-3"))
    assert restored.accepted
    assert restored.resulting_safety_state == "normal"
    assert restored.new_risk_permitted is True
    assert control.status().epoch_open is False
    assert control.status().kill_switch_engaged is False


def test_acknowledgement_before_reconciliation_is_refused(control):
    control.handle(command("halt_new_risk", seed="ack-halt"))
    outcome = control.handle(command("acknowledge", seed="ack-early"))

    assert outcome.disposition == "rejected"
    assert outcome.reason_code == "acknowledgement_denied_reconciliation_pending"
    assert control.status().operator_acknowledgement_pending is True


def test_lifecycle_commands_without_an_epoch_are_refused_and_change_nothing(control):
    for command_type in ("reconcile", "acknowledge", "restore_normal"):
        outcome = control.handle(command(command_type, seed=f"no-epoch-{command_type}"))
        assert outcome.disposition == "rejected"
        assert outcome.reason_code == "no_break_glass_epoch_open"
    assert control.status().safety_mode == "normal"
    assert control.status().epoch_open is False


# --- idempotency -----------------------------------------------------------

def test_repeated_delivery_of_a_command_id_produces_one_logical_effect(control, journal):
    command_id = str(uuid5(NAMESPACE, "duplicate"))
    first = control.handle(command("halt_new_risk", seed="duplicate", command_id=command_id))
    second = control.handle(
        command(
            "halt_new_risk",
            seed="duplicate",
            command_id=command_id,
            requested_at=BASE + timedelta(hours=1),
        )
    )
    command_events = [
        event for event in journal.read_range() if event.event_type == OPERATOR_COMMAND_EVENT_TYPE
    ]
    safety_events = [
        event for event in journal.read_range() if event.event_type == SAFETY_EVENT_TYPE
    ]

    assert first.accepted and second.duplicate and second.accepted
    assert second.reason_code == first.reason_code
    assert len(command_events) == 1
    assert len(safety_events) == 1
    assert control.status().safety_mode == "halt_new_risk"


def test_reusing_a_command_id_for_a_different_command_is_refused(control, journal):
    command_id = str(uuid5(NAMESPACE, "conflict"))
    control.handle(command("halt_new_risk", seed="conflict", command_id=command_id))
    conflict = control.handle(
        command("enter_degraded", seed="conflict", command_id=command_id, reason="different")
    )
    command_events = [
        event for event in journal.read_range() if event.event_type == OPERATOR_COMMAND_EVENT_TYPE
    ]

    assert conflict.disposition == "rejected"
    assert conflict.reason_code == "command_id_conflict"
    assert len(command_events) == 1
    assert control.status().safety_mode == "halt_new_risk"


# --- restart / persistence -------------------------------------------------

def test_restart_restores_the_restricted_posture_and_never_normal(tmp_path, journal, control):
    control.handle(command("force_recovery_required"))
    restarted = BreakGlassControl(
        journal=journal, state_dir=tmp_path / "state", clock=lambda: BASE + timedelta(days=1)
    )
    status = restarted.status()

    assert status.safety_mode == "recovery_required"
    assert status.kill_switch_engaged is True
    assert status.new_risk_permitted is False
    assert status.epoch_open is True
    assert status.restore_blockers


def test_safety_state_is_rebuilt_from_the_journal_when_the_cache_is_missing(tmp_path, journal, control):
    control.handle(command("cancel_open"))
    (tmp_path / "state" / "safety_state.json").unlink()
    restarted = BreakGlassControl(journal=journal, state_dir=tmp_path / "state", clock=lambda: BASE)
    status = restarted.status()

    assert status.safety_state_source == "journal_replay"
    assert status.safety_mode == "cancel_open"
    assert status.kill_switch_engaged is True


def test_a_corrupt_state_cache_fails_closed_and_is_never_silently_repaired(
    tmp_path, journal, control
):
    control.handle(command("halt_new_risk"))
    (tmp_path / "state" / "safety_state.json").write_text("{not json", encoding="utf-8")
    restarted = BreakGlassControl(journal=journal, state_dir=tmp_path / "state", clock=lambda: BASE)
    status = restarted.status()

    assert status.forced_fail_closed is True
    assert status.safety_mode == "recovery_required"
    assert status.new_risk_permitted is False


def test_state_cache_that_contradicts_the_journal_fails_closed(tmp_path, journal, control):
    control.handle(command("halt_new_risk"))
    state_file = tmp_path / "state" / "safety_state.json"
    payload = state_file.read_text(encoding="utf-8").replace("halt_new_risk", "normal")
    state_file.write_text(payload, encoding="utf-8")

    status = BreakGlassControl(journal=journal, state_dir=tmp_path / "state", clock=lambda: BASE).status()

    assert status.forced_fail_closed is True
    assert status.safety_mode == "recovery_required"


# --- the AI / strategy boundary -------------------------------------------

def test_ai_or_strategy_sourced_transitions_cannot_re_enable_trading(control):
    control.handle(command("halt_new_risk"))

    with pytest.raises(BreakGlassActiveError):
        control.runtime_request_mode_change(source="strategy", mode=SafetyMode.NORMAL)
    with pytest.raises(BreakGlassActiveError):
        control.runtime_request_mode_change(source="runtime", mode=SafetyMode.NORMAL)
    # Even with no epoch open, a non-operator source may never move the safety state directly.
    control.handle(command("reconcile", seed="ai-reconcile"))
    control.handle(command("acknowledge", seed="ai-ack"))
    control.handle(command("restore_normal", seed="ai-restore"))
    with pytest.raises(BreakGlassActiveError):
        control.runtime_request_mode_change(source="runtime", mode=SafetyMode.NORMAL)


def test_risk_limits_derivation_halts_new_risk_after_break_glass(control):
    control.handle(command("halt_new_risk"))
    limits = control.risk_limits(load_limits(LIMITS))
    request = RiskRequest(
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        side=Side.BUY,
        quantity=Decimal("0.01"),
        price=Decimal("2700"),
        strategy_id="test",
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
    decision = HardRiskEngine(limits).evaluate(request, state, now=BASE)

    assert limits.trading_halted is True
    assert decision.allowed is False
    assert "trading_halted" in decision.reason_codes


def test_reduce_only_mode_blocks_exposure_increases_only(control):
    control.handle(command("reduce_only"))
    limits = control.risk_limits(load_limits(LIMITS))

    assert limits.reduce_only_mode is True
    assert limits.trading_halted is False


# --- ownership -------------------------------------------------------------

def test_the_normal_writer_cannot_claim_the_lease_while_an_epoch_is_open(control):
    control.handle(command("halt_new_risk"))

    with pytest.raises(BreakGlassActiveError):
        control.claim_normal_writer(owner_id="normal:runtime")


def test_no_writer_means_no_trading_by_default(control):
    status = control.status()

    assert status.ownership_owner is None
    assert status.new_risk_permitted is False


def test_normal_writer_can_claim_the_lease_before_any_break_glass(control):
    guard = control.claim_normal_writer(owner_id="normal:runtime")

    assert guard.may_create_new_risk() is True
    assert control.status().new_risk_permitted is True
    assert control.ownership_store.require_fence().owner_kind is OwnerKind.NORMAL


# --- observability ---------------------------------------------------------

@pytest.mark.parametrize("command_type", ["halt_new_risk", "reduce_only", "cancel_open"])
def test_outcome_records_are_structured_secret_free_audit_evidence(control, command_type):
    outcome = control.handle(command(command_type, seed=f"obs-{command_type}"))
    record = outcome.to_record()

    assert record["command_id"] == str(outcome.command_id)
    assert record["disposition"] in {"accepted", "rejected"}
    assert record["safety_transitions"]
    assert record["reconciliation"]["required"] is True
    assert record["operator_acknowledgement_pending"] is True
    assert record["new_risk_permitted"] is False
    assert record["venue_control"]["disposition"] in {
        "not_requested",
        "confirmed",
        "ambiguous",
        "unavailable",
        "rejected",
    }
    rendered = str(record).lower()
    for token in ("api_key", "apikey", "api_secret", "x-mbx", "signature="):
        assert token not in rendered


def test_status_is_read_only(control, journal):
    before = journal.count()
    control.status()
    control.load_ledger()
    control.load_epoch()
    assert journal.count() == before
