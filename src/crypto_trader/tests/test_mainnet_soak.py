"""Phase 15: the durable soak state and the mechanical exit criteria."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from crypto_trader.mainnet import (
    MIN_CONTINUOUS_SECONDS,
    MIN_PIPELINE_CYCLES,
    SoakCounters,
    SoakRecorder,
    SoakState,
    SoakStateStore,
    evaluate_exit_criteria,
)
from crypto_trader.mainnet.errors import MainnetError
from crypto_trader.tests import mainnet_fixtures as fx


def _recorder(tmp_path: Path) -> SoakRecorder:
    return SoakRecorder(SoakStateStore(tmp_path / "soak.json"), clock=lambda: fx.BASE)


def test_the_reported_counters_start_at_zero_and_survive_restart(tmp_path):
    first = _recorder(tmp_path)
    state = first.state()

    assert state.soak_start_time == fx.BASE
    assert set(state.counters.to_record()) == {
        "completed_pipeline_cycles",
        "validity_rejections",
        "halt_exercises",
        "reconciliation_exercises",
        "restart_recovery_exercises",
        "stale_fence_rejections",
        "ambiguity_fault_injections",
        "secret_leaks",
        "mainnet_mutation_attempts",
        "manual_corrective_interventions",
    }
    assert all(value == 0 for value in state.counters.to_record().values())

    first.record_cycle(completed=True, validity_rejected=True)
    first.record_halt_exercise()
    # A brand new recorder over the same store sees the same durable counters.
    second = _recorder(tmp_path)
    reloaded = second.state()

    assert reloaded.counters.completed_pipeline_cycles == 1
    assert reloaded.counters.validity_rejections == 1
    assert reloaded.counters.halt_exercises == 1
    assert second.snapshot()["continuous_runtime"] >= 0


def test_an_unknown_counter_is_refused():
    with pytest.raises(Exception):
        SoakCounters().with_updates(not_a_counter=1)


def test_an_incomplete_soak_reports_every_unmet_criterion(tmp_path):
    recorder = _recorder(tmp_path)

    met, unmet = recorder.exit_criteria()

    assert met is False
    assert set(unmet) == {
        "continuous_runtime_below_72h",
        "completed_pipeline_cycles_below_50",
        "validity_rejection_not_observed",
        "halt_new_risk_not_exercised",
        "reconciliation_not_exercised",
        "restart_recovery_not_exercised",
        "stale_fence_rejection_not_exercised",
        "post_send_ambiguity_not_exercised",
        "journal_replay_determinism_not_confirmed",
    }


def test_the_exit_criteria_are_mechanical_and_require_every_item(tmp_path):
    complete = SoakState(
        soak_start_time=fx.BASE - timedelta(seconds=MIN_CONTINUOUS_SECONDS + 60),
        last_heartbeat=fx.BASE,
        journal_deterministic=True,
        counters=SoakCounters(
            completed_pipeline_cycles=MIN_PIPELINE_CYCLES,
            validity_rejections=1,
            halt_exercises=1,
            reconciliation_exercises=1,
            restart_recovery_exercises=1,
            stale_fence_rejections=1,
            ambiguity_fault_injections=1,
        ),
    )

    met, unmet = evaluate_exit_criteria(complete, fx.BASE)
    assert met is True
    assert unmet == ()

    # One missing item, one secret leak, one mutation attempt and one intervention each fail.
    for counter in ("secret_leaks", "mainnet_mutation_attempts", "manual_corrective_interventions"):
        broken = SoakState(
            soak_start_time=complete.soak_start_time,
            last_heartbeat=complete.last_heartbeat,
            journal_deterministic=True,
            counters=complete.counters.with_updates(**{counter: 1}),
        )
        met, unmet = evaluate_exit_criteria(broken, fx.BASE)
        assert met is False, counter
        assert unmet


def test_a_planned_restart_is_recorded_and_keeps_the_window(tmp_path):
    recorder = _recorder(tmp_path)
    before = recorder.state().soak_start_time

    after = recorder.resume(planned=True)

    assert after.soak_start_time == before
    assert after.planned_restarts == 1
    assert after.counters.restart_recovery_exercises == 1


def test_an_unplanned_interruption_resets_the_continuous_window(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.note_planned_restart()
    before = recorder.state()
    assert before.soak_start_time == fx.BASE

    after = recorder.resume(planned=False)

    assert after.counters.manual_corrective_interventions == 1
    assert after.soak_start_time == fx.BASE  # the clock is frozen in this test
    assert after.planned_restarts == before.planned_restarts


def test_the_reported_event_counters_each_increment_their_own_counter(tmp_path):
    recorder = _recorder(tmp_path)

    recorder.record_reconciliation_exercise()
    recorder.record_stale_fence_rejection()
    recorder.record_ambiguity_fault_injection()
    recorder.record_secret_leak()
    recorder.record_mutation_attempt()
    recorder.set_journal_deterministic(True)
    state = recorder.state()

    assert state.counters.reconciliation_exercises == 1
    assert state.counters.stale_fence_rejections == 1
    assert state.counters.ambiguity_fault_injections == 1
    assert state.counters.secret_leaks == 1
    assert state.counters.mainnet_mutation_attempts == 1
    assert state.journal_deterministic is True


def test_a_corrupt_soak_state_fails_closed(tmp_path):
    store = SoakStateStore(tmp_path / "soak.json")
    store.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(MainnetError):
        store.load()
