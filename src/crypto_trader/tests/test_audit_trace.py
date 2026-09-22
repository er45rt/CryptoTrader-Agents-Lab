"""Phase 12: fault-injection scenario tests (each scenario is individually pinned)."""

from pathlib import Path

import pytest

from crypto_trader.audit import SCENARIOS, ScenarioResult, run_all_scenarios, run_scenario
from crypto_trader.audit import scenarios as audit_scenarios

EXPECTED_SCENARIO_IDS = {
    "restart_journal_replay",
    "duplicate_event_delivery",
    "stale_ai_response",
    "backpressure_exhaustion",
    "risk_breach_halts_new_risk",
    "unknown_order_result",
    "cancel_failure",
    "network_interruption",
    "partial_fill_then_restart",
    "reconciliation_mismatch",
    "kill_switch_while_loaded",
    "restart_while_halted",
    "corrupt_event_rejection",
    "paper_pipeline_end_to_end",
    "nautilus_queue_aware_execution",
}


def test_the_scenario_set_covers_every_required_fault_class():
    ids = {scenario.__name__.removeprefix("scenario_") for scenario in SCENARIOS}

    assert ids == EXPECTED_SCENARIO_IDS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda fn: fn.__name__)
def test_each_fault_injection_scenario_passes(scenario, tmp_path: Path):
    result = run_scenario(scenario, tmp_path)

    assert result.status == "pass", f"{result.scenario_id} failed: {result.detail}"
    assert result.evidence, "a passing scenario must carry evidence"


def test_all_scenarios_run_together(tmp_path: Path):
    results = run_all_scenarios(tmp_path)

    assert len(results) == len(SCENARIOS)
    assert all(result.passed for result in results)
    assert len({result.scenario_id for result in results}) == len(results)


def test_a_scenario_that_raises_is_reported_as_a_failure_not_skipped(tmp_path: Path):
    def exploding(_workspace: Path) -> ScenarioResult:
        raise RuntimeError("injected scenario crash")

    result = run_scenario(exploding, tmp_path)

    assert result.status == "fail"
    assert "injected scenario crash" in result.detail


def test_restart_replay_evidence_is_quantified(tmp_path: Path):
    result = audit_scenarios.scenario_restart_journal_replay(tmp_path)

    assert result.passed
    assert result.evidence["full_replay_events"] == 6
    assert result.evidence["tail_events"] == 3


def test_unknown_order_result_blocks_new_risk(tmp_path: Path):
    result = audit_scenarios.scenario_unknown_order_result(tmp_path)

    assert result.passed
    assert result.evidence["outcome"] == "unknown_pending_reconciliation"
    assert result.evidence["unresolved"] == ["ct-ambiguous"]


def test_kill_switch_evidence_shows_the_ai_backlog_was_bypassed(tmp_path: Path):
    result = audit_scenarios.scenario_kill_switch_while_loaded(tmp_path)

    assert result.passed
    assert result.evidence["safety_first"] is True
    assert result.evidence["normal_backlog"] == 3


def test_nautilus_scenario_proves_the_queue_aware_configuration(tmp_path: Path):
    result = audit_scenarios.scenario_nautilus_queue_aware_execution(tmp_path)

    assert result.passed
    assert result.evidence["book_type"] == "L2_MBP"
    assert result.evidence["engine_version"] == "1.231.0"


def test_scenario_records_are_json_ready():
    result = ScenarioResult(
        scenario_id="x", title="x", status="pass", detail="ok", evidence={"a": 1}
    )

    assert result.to_record() == {
        "scenario_id": "x",
        "title": "x",
        "status": "pass",
        "detail": "ok",
        "evidence": {"a": 1},
    }
