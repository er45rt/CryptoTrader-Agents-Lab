"""Phase 9: deterministic safety-mode, kill-switch, persistence and break-glass tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.risk import HardRiskEngine, RiskRequest, RiskStateSnapshot, Side, load_limits
from crypto_trader.safety import (
    BreakGlassAction,
    BreakGlassRequest,
    SafetyConfigError,
    SafetyController,
    SafetyMode,
    SafetyStateError,
    SimulatedBreakGlass,
    record_safety_state,
    record_safety_transitions,
    restore_safety_controller,
    risk_limits_for_safety,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SESSION = UUID("33333333-3333-4333-8333-333333333333")

LIMIT_CONFIG = {
    "limits_version": "risk-v1",
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


def controller() -> SafetyController:
    return SafetyController(correlation_id=SESSION)


def risk_state(**overrides) -> RiskStateSnapshot:
    fields = {
        "equity_quote": Decimal("10000"),
        "peak_equity_quote": Decimal("10000"),
        "realized_pnl_quote": Decimal("0"),
        "gross_exposure_quote": Decimal("0"),
        "net_exposure_quote": Decimal("0"),
        "open_orders": 0,
        "open_positions": 0,
        "snapshot_ts": BASE,
    }
    fields.update(overrides)
    return RiskStateSnapshot(**fields)


def risk_request(side: Side = Side.BUY) -> RiskRequest:
    return RiskRequest(
        correlation_id=SESSION,
        symbol="ETHUSDT",
        side=side,
        quantity=Decimal("1"),
        price=Decimal("2700"),
        strategy_id="s1",
        intent_id=uuid4(),
    )


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as instance:
        yield instance


# --- the mode is the only state -------------------------------------------

def test_normal_mode_is_the_initial_state():
    state = controller()

    assert state.mode is SafetyMode.NORMAL
    assert state.kill_switch_engaged is False
    assert state.allow_new_risk() is True
    assert state.requires_cancel_open() is False
    assert state.permits_live_execution() is True


@pytest.mark.parametrize(
    "mode,allows_new_risk,requires_cancel_open,permits_live_execution",
    [
        (SafetyMode.NORMAL, True, False, True),
        (SafetyMode.REDUCE_ONLY, False, False, True),
        (SafetyMode.HALT_NEW_RISK, False, False, True),
        (SafetyMode.CANCEL_OPEN, False, True, True),
        (SafetyMode.DEGRADED, False, True, False),
        (SafetyMode.RECOVERY_REQUIRED, False, True, False),
    ],
)
def test_capability_matrix_is_a_pure_function_of_the_mode(
    mode, allows_new_risk, requires_cancel_open, permits_live_execution
):
    assert mode.allows_new_risk is allows_new_risk
    assert mode.requires_cancel_open is requires_cancel_open
    assert mode.permits_live_execution is permits_live_execution


def test_only_normal_allows_exposure_increase():
    for mode in SafetyMode:
        assert mode.allows_exposure_increase is (mode is SafetyMode.NORMAL)


def test_unknown_mode_value_is_rejected():
    with pytest.raises(ValueError):
        SafetyMode("halt")


# --- transitions and idempotency -----------------------------------------

def test_set_mode_records_the_transition_and_advances_the_sequence():
    state = controller()

    transition = state.set_mode(
        SafetyMode.HALT_NEW_RISK, reason_code="operator_halt", reason="manual halt"
    )

    assert transition.changed is True
    assert transition.previous_mode is SafetyMode.NORMAL
    assert transition.mode is SafetyMode.HALT_NEW_RISK
    assert transition.is_escalation is True
    assert state.sequence == 1
    assert state.allow_new_risk() is False


def test_repeating_the_same_mode_is_an_idempotent_no_op():
    state = controller()
    state.set_mode(SafetyMode.REDUCE_ONLY, reason_code="risk", reason="risk asked")

    repeat = state.set_mode(SafetyMode.REDUCE_ONLY, reason_code="risk", reason="risk asked again")

    assert repeat.changed is False
    assert state.sequence == 1


def test_transitions_require_a_reason_and_a_known_trigger():
    state = controller()

    with pytest.raises(SafetyStateError):
        state.set_mode(SafetyMode.DEGRADED, reason_code="", reason="x")
    with pytest.raises(SafetyStateError):
        state.set_mode(SafetyMode.DEGRADED, reason_code="x", reason="")
    with pytest.raises(SafetyStateError):
        state.set_mode(SafetyMode.DEGRADED, reason_code="x", reason="y", trigger="sneaky")
    with pytest.raises(SafetyStateError):
        state.set_mode("degraded", reason_code="x", reason="y")  # type: ignore[arg-type]


def test_transition_record_is_observable_and_secret_free():
    transition = controller().set_mode(
        SafetyMode.DEGRADED, reason_code="feed_lost", reason="market data feed lost"
    )

    record = transition.to_record()

    assert record["mode"] == "degraded"
    assert record["trigger"] == "operator"
    assert record["allow_new_risk"] is False
    assert record["requires_cancel_open"] is True
    assert not any("key" in name.lower() or "secret" in name.lower() for name in record)


# --- kill switch ----------------------------------------------------------

def test_kill_switch_halts_new_risk_and_is_idempotent():
    state = controller()

    first = state.engage_kill_switch(reason_code="kill", reason="operator kill switch")
    second = state.engage_kill_switch(reason_code="kill", reason="operator kill switch")

    assert first.changed is True
    assert second.changed is False
    assert state.kill_switch_engaged is True
    assert state.mode is SafetyMode.HALT_NEW_RISK
    assert state.allow_new_risk() is False
    assert state.requires_cancel_open() is True
    assert state.sequence == 1


def test_kill_switch_is_not_released_automatically():
    state = controller()
    state.engage_kill_switch(reason_code="kill", reason="operator kill switch")

    # Changing the mode does not silently clear the kill switch.
    state.set_mode(SafetyMode.CANCEL_OPEN, reason_code="c", reason="cancel open orders")

    assert state.kill_switch_engaged is True
    assert state.allow_new_risk() is False

    release = state.release_kill_switch(reason_code="cleared", reason="operator cleared it")
    assert release.changed is True
    assert state.kill_switch_engaged is False


# --- restart persistence / fail closed -----------------------------------

def test_persisted_state_round_trips_across_a_restart():
    original = controller()
    original.engage_kill_switch(reason_code="kill", reason="operator kill switch")
    persisted = original.to_persisted()

    restored = restore_safety_controller(persisted)

    assert restored.mode is SafetyMode.HALT_NEW_RISK
    assert restored.kill_switch_engaged is True
    assert restored.correlation_id == original.correlation_id
    assert restored.sequence == original.sequence
    assert restored.allow_new_risk() is False


def test_restart_while_halted_stays_halted():
    original = controller()
    original.set_mode(SafetyMode.HALT_NEW_RISK, reason_code="halt", reason="halted")

    restored = restore_safety_controller(original.to_persisted())

    assert restored.mode is SafetyMode.HALT_NEW_RISK
    assert restored.permits_live_execution() is True
    assert restored.allow_new_risk() is False


def test_missing_persisted_state_fails_closed():
    restored = restore_safety_controller(None)

    assert restored.mode is SafetyMode.RECOVERY_REQUIRED
    assert restored.kill_switch_engaged is True
    assert restored.reason_code == "safety_state_missing"


@pytest.mark.parametrize(
    "corrupt",
    [
        {"mode": "normal"},
        {"schema_version": 99, "mode": "normal", "kill_switch_engaged": False,
         "reason_code": "x", "reason": "y", "sequence": 0,
         "session_id": str(SESSION)},
        {"schema_version": 1, "mode": "not_a_mode", "kill_switch_engaged": False,
         "reason_code": "x", "reason": "y", "sequence": 0,
         "session_id": str(SESSION)},
        {"schema_version": 1, "mode": "normal", "kill_switch_engaged": "yes",
         "reason_code": "x", "reason": "y", "sequence": 0,
         "session_id": str(SESSION)},
        {"schema_version": 1, "mode": "recovery_required", "kill_switch_engaged": False,
         "reason_code": "x", "reason": "y", "sequence": 0,
         "session_id": str(SESSION)},
    ],
)
def test_corrupt_persisted_state_restores_fail_closed(corrupt):
    restored = restore_safety_controller(corrupt)

    assert restored.mode is SafetyMode.RECOVERY_REQUIRED
    assert restored.kill_switch_engaged is True
    assert restored.allow_new_risk() is False


def test_persisted_state_never_restores_into_normal_from_junk():
    restored = restore_safety_controller({"anything": "at all"})

    assert restored.mode is not SafetyMode.NORMAL
    assert restored.kill_switch_engaged is True


# --- halt blocks new risk through the deterministic risk engine -----------

def test_halt_blocks_new_risk_through_the_hard_risk_engine():
    state = controller()
    state.set_mode(SafetyMode.HALT_NEW_RISK, reason_code="halt", reason="halted")
    base = load_limits(LIMIT_CONFIG)
    assert state.allow_new_risk() is False

    # The caller derives the risk limits from the safety mode before evaluating anything.
    blocked_engine = HardRiskEngine(risk_limits_for_safety(state, base))
    blocked = blocked_engine.evaluate(risk_request(), risk_state(), now=BASE)
    unblocked = HardRiskEngine(base).evaluate(risk_request(), risk_state(), now=BASE)

    assert blocked.allowed is False
    assert "trading_halted" in blocked.reason_codes
    assert unblocked.allowed is True


def test_normal_mode_leaves_the_risk_limits_untouched():
    base = load_limits(LIMIT_CONFIG)

    assert risk_limits_for_safety(controller(), base) == base


def test_reduce_only_blocks_exposure_increase_but_permits_reduction():
    state = controller()
    state.set_mode(SafetyMode.REDUCE_ONLY, reason_code="risk", reason="reduce only")
    engine = HardRiskEngine(risk_limits_for_safety(state, load_limits(LIMIT_CONFIG)))

    increasing = engine.evaluate(risk_request(Side.BUY), risk_state(), now=BASE)
    reducing = engine.evaluate(
        risk_request(Side.SELL), risk_state(position_quantity={"ETHUSDT": Decimal("1")}), now=BASE
    )

    assert increasing.allowed is False
    assert "reduce_only_violation" in increasing.reason_codes
    assert reducing.allowed is True


def test_kill_switch_scales_the_limits_even_in_normal_mode():
    state = controller()
    state.engage_kill_switch(reason_code="kill", reason="kill")
    scaled = risk_limits_for_safety(state, load_limits(LIMIT_CONFIG))

    assert scaled.trading_halted is True


# --- break-glass ----------------------------------------------------------

def test_break_glass_forces_degraded_then_recovery_required():
    state = controller()

    transitions = state.engage_break_glass(reason_code="runtime_dead", reason="runtime unresponsive")

    assert [item.mode for item in transitions] == [
        SafetyMode.DEGRADED,
        SafetyMode.RECOVERY_REQUIRED,
    ]
    assert state.mode is SafetyMode.RECOVERY_REQUIRED
    assert state.kill_switch_engaged is True
    assert state.permits_live_execution() is False
    assert state.allow_new_risk() is False


def test_break_glass_is_idempotent_once_in_the_posture():
    state = controller()
    state.engage_break_glass(reason_code="runtime_dead", reason="runtime unresponsive")

    assert state.engage_break_glass(reason_code="again", reason="again") == ()
    assert state.mode is SafetyMode.RECOVERY_REQUIRED


def test_break_glass_requires_an_explicit_operator_return_to_normal():
    state = controller()
    state.engage_break_glass(reason_code="runtime_dead", reason="runtime unresponsive")
    state.release_kill_switch(reason_code="reconciled", reason="reconciliation complete")

    assert state.mode is SafetyMode.RECOVERY_REQUIRED
    assert state.allow_new_risk() is False

    state.set_mode(SafetyMode.NORMAL, reason_code="operator_resume", reason="operator resumed")
    assert state.allow_new_risk() is True


def test_simulated_break_glass_performs_no_io():
    simulated = SimulatedBreakGlass()
    request = BreakGlassRequest(
        action=BreakGlassAction.CANCEL_ALL, reason_code="runtime_dead", reason="runtime dead"
    )

    acknowledgement = simulated.execute_emergency_action(request)

    assert acknowledgement.startswith("simulated:cancel_all:")
    assert simulated.accepted == [request]


def test_break_glass_request_requires_a_reason():
    with pytest.raises(SafetyStateError):
        BreakGlassRequest(action=BreakGlassAction.HALT, reason_code="", reason="x")
    with pytest.raises(SafetyStateError):
        BreakGlassRequest(action="halt", reason_code="x", reason="y")  # type: ignore[arg-type]


# --- journaling ----------------------------------------------------------

def test_transitions_are_journaled_through_the_additive_contract(journal):
    state = controller()
    transition = state.set_mode(
        SafetyMode.CANCEL_OPEN, reason_code="feed_lost", reason="market data feed lost"
    )

    event_ids = record_safety_state(journal, transition, occurred_at=BASE)

    assert len(event_ids) == 1
    decoded = journal.read_by_id(event_ids[0])
    assert decoded.event_type == "safety.state.changed"
    assert decoded.payload.mode == "cancel_open"
    assert decoded.payload.previous_mode == "normal"
    assert decoded.payload.kill_switch_engaged is False
    assert decoded.correlation_id == SESSION


def test_idempotent_transition_is_not_journaled(journal):
    state = controller()
    state.set_mode(SafetyMode.REDUCE_ONLY, reason_code="risk", reason="reduce only")
    repeat = state.set_mode(SafetyMode.REDUCE_ONLY, reason_code="risk", reason="reduce only")

    event_ids = record_safety_state(journal, repeat, occurred_at=BASE)

    assert event_ids == []
    assert journal.count() == 0


def test_relivered_transition_event_is_idempotent_by_event_id(journal):
    state = controller()
    transition = state.set_mode(SafetyMode.HALT_NEW_RISK, reason_code="halt", reason="halted")
    event_id = uuid4()

    record_safety_state(journal, transition, occurred_at=BASE, event_id=event_id)
    record_safety_state(journal, transition, occurred_at=BASE, event_id=event_id)

    assert journal.count() == 1


def test_break_glass_posture_change_is_journaled_in_one_transaction(journal):
    state = controller()
    transitions = state.engage_break_glass(reason_code="runtime_dead", reason="runtime dead")

    event_ids = record_safety_transitions(journal, transitions, occurred_at=BASE)

    assert len(event_ids) == 2
    events = [journal.read_by_id(event_id) for event_id in event_ids]
    assert [event.payload.mode for event in events] == ["degraded", "recovery_required"]
    assert all(event.payload.kill_switch_engaged for event in events)


def test_journaling_requires_a_timezone_aware_timestamp(journal):
    state = controller()
    transition = state.set_mode(SafetyMode.HALT_NEW_RISK, reason_code="halt", reason="halted")

    with pytest.raises(SafetyConfigError):
        record_safety_state(journal, transition, occurred_at=datetime(2026, 1, 1))


def test_safety_controller_rejects_a_non_mode():
    with pytest.raises(SafetyConfigError):
        SafetyController(_mode="normal")  # type: ignore[arg-type]


def test_safety_state_requires_a_reason_at_construction():
    with pytest.raises(SafetyConfigError):
        SafetyController(_reason_code="", _reason="x")
    with pytest.raises(SafetyConfigError):
        SafetyController(_reason_code="x", _reason="")


def test_transition_history_is_available_for_observability():
    state = controller()
    state.set_mode(SafetyMode.REDUCE_ONLY, reason_code="risk", reason="reduce only")
    state.engage_kill_switch(reason_code="kill", reason="kill")

    assert [item.mode for item in state.history] == [
        SafetyMode.REDUCE_ONLY,
        SafetyMode.HALT_NEW_RISK,
    ]
    assert state.history[-1].trigger == "operator"
    assert state.history[-1].to_record()["sequence"] == 2


def test_de_escalation_is_explicit_and_flagged():
    state = controller()
    state.set_mode(SafetyMode.CANCEL_OPEN, reason_code="c", reason="cancel")

    transition = state.set_mode(
        SafetyMode.NORMAL, reason_code="operator_resume", reason="operator resumed"
    )

    assert transition.is_de_escalation is True
    assert transition.is_escalation is False


def test_safety_timestamps_are_normalized_to_utc(journal):
    shifted = timezone(timedelta(hours=3))
    state = controller()
    transition = state.set_mode(SafetyMode.HALT_NEW_RISK, reason_code="halt", reason="halted")

    event_ids = record_safety_state(
        journal, transition, occurred_at=BASE.astimezone(shifted)
    )
    decoded = journal.read_by_id(event_ids[0])

    assert decoded.occurred_at.utcoffset() == timedelta(0)
