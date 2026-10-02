"""Phase 14: writer ownership/fencing at the production runtime decision boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from crypto_trader.breakglass import (
    BreakGlassActiveError,
    BinanceSpotTestnetEmergencyControl,
    OwnerKind,
    OwnershipStateError,
    PolicyOnlyVenueControl,
    StaleWriterFenceError,
)
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import (
    ProductionSafetyBlockedError,
    ProductionStartupError,
    ProductionStateError,
)
from crypto_trader.safety import SafetyMode
from crypto_trader.tests.production_fixtures import (
    BASE,
    INSTRUMENT,
    RecordingExecutionPort,
    build_runtime,
    control_for,
    intent,
    limits,
    operator_command,
    persist_safety_state,
)


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as active:
        yield active


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _started_runtime(journal, state_dir, **overrides):
    control = overrides.pop("control", None) or control_for(journal, state_dir)
    port = RecordingExecutionPort(INSTRUMENT)
    runtime = build_runtime(
        journal=journal,
        state_dir=state_dir,
        execution=port,
        control=control,
        **overrides,
    )
    return control, port, runtime


# --- 1/2/3: startup ownership, exclusivity and fail-closed leases -----------------

def test_the_normal_runtime_claims_the_writer_lease_at_startup(journal, state_dir):
    control, port, runtime = _started_runtime(journal, state_dir)

    report = runtime.start()

    assert report.fence_term == 1
    assert report.owner_id == "normal:production-runtime"
    assert report.safety_mode == "normal"
    assert report.reconciliation_state == "resolved"
    assert port.start_calls == 1
    record = control.ownership_store.require_fence()
    assert record.owner_kind is OwnerKind.NORMAL
    assert record.term == 1
    assert runtime.status()["new_risk_permitted"] is True


def test_a_second_normal_writer_cannot_be_authoritative_at_the_same_time(journal, state_dir):
    control, port, runtime = _started_runtime(journal, state_dir)
    runtime.start()
    preparation = runtime.prepare_intent(intent("exclusive-1"))
    assert preparation.approved is True

    control.claim_normal_writer(owner_id="normal:second-runtime")

    with pytest.raises(StaleWriterFenceError):
        runtime.submit_prepared(preparation)
    assert port.submit_count == 0
    assert len(runtime._pending) == 0
    assert runtime._issued_preparations == {}
    assert control.ownership_store.require_fence().owner_id == "normal:second-runtime"


def test_submit_prepared_rejects_forged_object_but_keeps_real_issued_preparation(
    journal, state_dir
):
    _control, port, runtime = _started_runtime(journal, state_dir)
    runtime.start()
    preparation = runtime.prepare_intent(intent("forged-preparation"))
    forged = replace(preparation)

    with pytest.raises(ProductionStateError, match="exact unused preparation"):
        runtime.submit_prepared(forged)

    assert port.submit_count == 0
    assert len(runtime._pending) == 1
    result = runtime.submit_prepared(preparation)
    assert result.intent_id == preparation.intent_id
    assert port.submit_count == 1
    assert len(runtime._pending) == 0


def test_submit_prepared_is_one_time_only(journal, state_dir):
    _control, port, runtime = _started_runtime(journal, state_dir)
    runtime.start()
    preparation = runtime.prepare_intent(intent("one-time-preparation"))

    runtime.submit_prepared(preparation)

    with pytest.raises(ProductionStateError, match="exact unused preparation"):
        runtime.submit_prepared(preparation)
    assert port.submit_count == 1
    assert len(runtime._pending) == 0


def test_expired_preparation_is_refused_and_releases_reservation(journal, state_dir):
    _control, port, runtime = _started_runtime(journal, state_dir)
    runtime.start()
    preparation = runtime.prepare_intent(intent("expired-preparation"))
    runtime._clock = lambda: BASE + timedelta(seconds=61)

    with pytest.raises(ProductionStateError, match="expired before submission"):
        runtime.submit_prepared(preparation)

    assert port.submit_count == 0
    assert len(runtime._pending) == 0
    assert runtime._issued_preparations == {}


def test_an_absent_writer_lease_refuses_writes_and_new_risk(journal, state_dir):
    control = control_for(journal, state_dir)

    with pytest.raises(OwnershipStateError):
        control.normal_writer_guard()
    assert control.status().new_risk_permitted is False
    assert control.ownership_store.read() is None


def test_a_corrupt_writer_lease_fails_closed(journal, state_dir):
    (state_dir / "ownership.json").write_text("{not json", encoding="utf-8")
    _control, port, runtime = _started_runtime(journal, state_dir)

    with pytest.raises(ProductionStateError):
        runtime.start()

    assert runtime.status()["last_refusal"] == "writer_lease_unavailable"
    assert port.submit_count == 0
    assert runtime.status()["runtime_state"] == "stopped"


# --- 4/5: persisted posture blocks new risk --------------------------------------

def test_a_persisted_degraded_posture_prevents_new_risk(journal, state_dir):
    persist_safety_state(state_dir, mode="degraded", kill_switch_engaged=True)
    control, port, runtime = _started_runtime(journal, state_dir)

    with pytest.raises(ProductionStartupError):
        runtime.start()

    assert control.load_safety_state().controller.mode is SafetyMode.DEGRADED
    assert runtime.status()["new_risk_permitted"] is False
    assert port.submit_count == 0


def test_a_persisted_recovery_required_posture_prevents_new_risk(journal, state_dir):
    persist_safety_state(state_dir, mode="recovery_required", kill_switch_engaged=True)
    control, port, runtime = _started_runtime(journal, state_dir)

    with pytest.raises(ProductionStartupError):
        runtime.start()

    assert control.load_safety_state().controller.mode is SafetyMode.RECOVERY_REQUIRED
    assert runtime.status()["new_risk_permitted"] is False
    assert port.submit_count == 0


def test_a_non_normal_halt_posture_also_refuses_startup(journal, state_dir):
    persist_safety_state(state_dir, mode="halt_new_risk", kill_switch_engaged=False)
    _control, port, runtime = _started_runtime(journal, state_dir)

    with pytest.raises(ProductionStartupError) as raised:
        runtime.start()

    assert "halt_new_risk" in str(raised.value)
    assert port.submit_count == 0


@pytest.mark.parametrize("kill_switch_engaged", [False, True])
def test_final_fence_distinguishes_normal_mode_kill_switch_state(
    journal, state_dir, kill_switch_engaged
):
    control, port, runtime = _started_runtime(journal, state_dir)
    report = runtime.start()
    preparation = runtime.prepare_intent(intent(f"normal-kill-switch-{kill_switch_engaged}"))

    assert preparation.approved is True
    assert port.submit_count == 0

    persist_safety_state(
        state_dir,
        mode="normal",
        kill_switch_engaged=kill_switch_engaged,
        sequence=2,
    )
    load = control.load_safety_state()
    assert load.source == "state_file"
    assert load.forced_fail_closed is False
    assert load.controller.mode is SafetyMode.NORMAL
    assert load.controller.kill_switch_engaged is kill_switch_engaged
    ownership = control.ownership_store.require_fence()
    assert ownership.owner_kind is OwnerKind.NORMAL
    assert ownership.owner_id == report.owner_id
    assert ownership.term == report.fence_term

    if kill_switch_engaged:
        with pytest.raises(ProductionSafetyBlockedError, match=r"^kill_switch_engaged:"):
            runtime.submit_prepared(preparation)
        assert port.submit_count == 0
    else:
        result = runtime.submit_prepared(preparation)
        assert result.fence.kill_switch_engaged is False
        assert result.fence.safety_mode == "normal"
        assert port.submit_count == 1


# --- 6/7/8: takeover invalidates a running writer --------------------------------

def test_break_glass_takeover_invalidates_an_already_running_normal_writer(journal, state_dir):
    control, port, runtime = _started_runtime(journal, state_dir)
    runtime.start()
    preparation = runtime.prepare_intent(intent("takeover-1"))
    assert preparation.approved is True

    outcome = control.handle(operator_command("halt_new_risk", seed="takeover-halt"))

    assert outcome.resulting_safety_state == "halt_new_risk"
    with pytest.raises(StaleWriterFenceError):
        runtime.submit_prepared(preparation)
    assert port.submit_count == 0
    record = control.ownership_store.require_fence()
    assert record.owner_kind is OwnerKind.EMERGENCY
    assert record.term == 2


def test_a_stale_writer_cannot_submit_a_new_risk_order(journal, state_dir):
    control, port, runtime_a = _started_runtime(journal, state_dir)
    runtime_a.start()
    preparation = runtime_a.prepare_intent(intent("stale-1"))

    port_b = RecordingExecutionPort(INSTRUMENT)
    runtime_b = build_runtime(
        journal=journal, state_dir=state_dir, execution=port_b, control=control
    )
    report_b = runtime_b.start()

    assert report_b.fence_term == 2
    with pytest.raises(StaleWriterFenceError):
        runtime_a.submit_prepared(preparation)
    assert port.submit_count == 0
    # The current lease holder can still trade.
    outcome = runtime_b.execute_intent(intent("stale-2"))
    assert outcome.submitted is True
    assert port_b.submit_count == 1


def test_an_emergency_takeover_during_an_in_flight_decision_prevents_the_venue_submit(
    journal, state_dir
):
    control, port, runtime = _started_runtime(journal, state_dir)
    runtime.start()

    # The decision path approves the intent...
    preparation = runtime.prepare_intent(intent("inflight-1"))
    assert preparation.approved is True
    assert prep_stage(preparation) == "safety:fence_deferred_to_submit"

    # ...then an operator break-glass takeover happens inside the TOCTOU window...
    control.handle(operator_command("cancel_open", seed="inflight-cancel"))

    # ...and the final fence refuses the submission.
    with pytest.raises(StaleWriterFenceError):
        runtime.submit_prepared(preparation)
    assert port.submit_count == 0

    # A fresh intent is blocked even earlier, by the persisted posture.
    outcome = runtime.execute_intent(intent("inflight-2"))
    assert outcome.submitted is False
    assert "trading_halted" in (outcome.blocked_reason_code or "")
    assert port.submit_count == 0


def prep_stage(preparation) -> str:
    return preparation.stages[-1]


# --- 9/10: restart and reconciliation gating -------------------------------------

def test_a_restart_preserves_fencing_and_safety_restrictions(journal, state_dir):
    control, _port, runtime_a = _started_runtime(journal, state_dir)
    runtime_a.start()
    control.handle(operator_command("halt_new_risk", seed="restart-halt"))
    runtime_a.stop()

    port_b = RecordingExecutionPort(INSTRUMENT)
    runtime_b = build_runtime(
        journal=journal, state_dir=state_dir, execution=port_b, control=control
    )

    with pytest.raises(ProductionStartupError):
        runtime_b.start()

    record = control.ownership_store.require_fence()
    assert record.term == 2
    assert record.owner_kind is OwnerKind.EMERGENCY
    assert runtime_b.status()["new_risk_permitted"] is False
    assert port_b.submit_count == 0


def test_reconciliation_alone_does_not_restore_normal_without_acknowledgement(
    journal, state_dir
):
    control, _port, runtime = _started_runtime(journal, state_dir)
    runtime.start()
    control.handle(operator_command("halt_new_risk", seed="gate-halt"))

    reconciliation = control.handle(operator_command("reconcile", seed="gate-reconcile"))
    assert reconciliation.reconciliation_status == "resolved"

    denied = control.handle(operator_command("restore_normal", seed="gate-restore-1"))
    assert denied.disposition == "rejected"
    assert denied.reason_code == "restore_denied_acknowledgement_missing"

    acknowledgement = control.handle(operator_command("acknowledge", seed="gate-ack"))
    assert acknowledgement.disposition == "accepted"
    restored = control.handle(operator_command("restore_normal", seed="gate-restore-2"))
    assert restored.disposition == "accepted"
    assert restored.resulting_safety_state == "normal"


# --- 11/20: AI cannot bypass the fence; no concurrent writer creates new risk -----

def test_an_ai_or_strategy_decision_cannot_change_posture_or_bypass_the_fence(
    journal, state_dir
):
    control, port, runtime = _started_runtime(journal, state_dir)
    runtime.start()

    with pytest.raises(BreakGlassActiveError):
        control.runtime_request_mode_change(source="strategy", mode=SafetyMode.NORMAL)

    # A saturated AI/strategy backlog cannot force a submission either: bounded capacity blocks it.
    backpressure = runtime.execute_intent(intent("ai-1"), capacity_available=False)
    assert backpressure.submitted is False
    assert backpressure.blocked_reason_code == "bounded_capacity_exhausted"
    assert port.submit_count == 0

    # An already-approved preparation is still refused after an operator halt.
    preparation = runtime.prepare_intent(intent("ai-2"))
    assert preparation.approved is True
    control.handle(operator_command("halt_new_risk", seed="ai-halt"))
    with pytest.raises(StaleWriterFenceError):
        runtime.submit_prepared(preparation)
    assert port.submit_count == 0


def test_no_concurrent_normal_and_emergency_writer_can_create_new_risk(journal, state_dir):
    control, port, runtime = _started_runtime(journal, state_dir)
    runtime.start()
    preparation = runtime.prepare_intent(intent("concurrent-1"))
    assert preparation.approved is True

    control.handle(operator_command("force_recovery_required", seed="concurrent-recovery"))

    with pytest.raises(StaleWriterFenceError):
        runtime.submit_prepared(preparation)
    assert port.submit_count == 0
    assert control.status().new_risk_permitted is False
    assert control.risk_limits(limits()).trading_halted is True

    # The emergency path exposes no order-creating capability of its own.
    for venue_class in (PolicyOnlyVenueControl, BinanceSpotTestnetEmergencyControl):
        assert not any(
            hasattr(venue_class, name)
            for name in ("submit", "place_order", "create_order", "new_order", "execute_trade")
        )
