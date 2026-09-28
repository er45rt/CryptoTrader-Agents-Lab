"""Phase 14: writer ownership/fencing at the production runtime decision boundary."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
from crypto_trader.production import ProductionStartupError, ProductionStateError
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
    risk_state_provider,
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




@pytest.mark.parametrize("field", ["quantity", "reference_price", "limit_price"])
@pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN"])
def test_production_intent_rejects_non_finite_economics(field, bad):
    with pytest.raises(ProductionStateError):
        intent(**{field: Decimal(bad)})


def test_production_intent_rejects_non_enum_time_in_force():
    with pytest.raises(ProductionStateError, match="ExecutionTimeInForce"):
        intent(time_in_force="GTC")




@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan"), True, "5"])
def test_production_runtime_requires_explicit_finite_risk_state_freshness(
    journal, state_dir, bad
):
    with pytest.raises(ProductionStateError, match="risk_state_max_age_seconds"):
        build_runtime(
            journal=journal,
            state_dir=state_dir,
            execution=RecordingExecutionPort(INSTRUMENT),
            risk_state_max_age_seconds=bad,
        )


def test_stale_risk_snapshot_blocks_production_intent_before_submission(journal, state_dir):
    control, port, runtime = _started_runtime(
        journal,
        state_dir,
        risk_state_source=risk_state_provider(BASE - timedelta(seconds=6)),
        risk_state_max_age_seconds=5.0,
    )
    runtime.start()

    preparation = runtime.prepare_intent(intent("stale-risk-state"))

    assert preparation.approved is False
    assert "stale_risk_state" in (preparation.blocked_reason_code or "")
    assert port.submit_count == 0


def test_future_risk_snapshot_blocks_production_intent_before_submission(journal, state_dir):
    control, port, runtime = _started_runtime(
        journal,
        state_dir,
        risk_state_source=risk_state_provider(BASE + timedelta(seconds=1)),
        risk_state_max_age_seconds=5.0,
    )
    runtime.start()

    preparation = runtime.prepare_intent(intent("future-risk-state"))

    assert preparation.approved is False
    assert "risk_state_timestamp_in_future" in (preparation.blocked_reason_code or "")
    assert port.submit_count == 0


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
    assert control.ownership_store.require_fence().owner_id == "normal:second-runtime"


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

def test_takeover_cannot_land_between_final_fence_check_and_submit(journal, state_dir):
    class BlockingExecutionPort(RecordingExecutionPort):
        def __init__(self):
            super().__init__(INSTRUMENT)
            self.entered_submit = threading.Event()
            self.takeover_started = threading.Event()
            self.takeover_done = threading.Event()

        def submit(self, request):
            self.entered_submit.set()
            assert self.takeover_started.wait(timeout=5)
            # The takeover thread is now trying to mint a new term. While this send is inside the
            # submission guard it must remain blocked on the shared ownership exclusion.
            threading.Event().wait(0.1)
            assert self.takeover_done.is_set() is False
            return super().submit(request)

    control = control_for(journal, state_dir)
    port = BlockingExecutionPort()
    runtime = build_runtime(
        journal=journal,
        state_dir=state_dir,
        execution=port,
        control=control,
    )
    runtime.start()
    preparation = runtime.prepare_intent(intent("linearized-submit"))
    assert preparation.approved is True

    def takeover():
        assert port.entered_submit.wait(timeout=5)
        port.takeover_started.set()
        try:
            return control.ownership_store.takeover(
                owner_id="emergency:concurrency-test",
                owner_kind=OwnerKind.EMERGENCY,
                acquired_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                reason_code="concurrency-regression",
            )
        finally:
            port.takeover_done.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        takeover_future = pool.submit(takeover)
        submitted = runtime.submit_prepared(preparation)
        takeover_record = takeover_future.result(timeout=5)

    assert submitted.fence_term == 1
    assert port.submit_count == 1
    assert takeover_record.term == 2
    assert takeover_record.owner_kind is OwnerKind.EMERGENCY
    assert control.ownership_store.require_fence().term == 2