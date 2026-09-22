"""Phase 13: the break-glass audit matrix, its mechanical verdict and its limits."""

import json
from pathlib import Path

import pytest

from crypto_trader.breakglass.audit import (
    SCENARIOS,
    VERDICT_NOT_READY,
    VERDICT_READY,
    BreakGlassAuditReport,
    BreakGlassItem,
    BreakGlassScenarioResult,
    run_break_glass_audit,
)
from crypto_trader.breakglass.audit import matrix as audit_matrix


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> BreakGlassAuditReport:
    workspace = tmp_path_factory.mktemp("breakglass-audit")
    return run_break_glass_audit(workspace)


def test_the_verdict_is_ready_for_owner_review(report):
    assert report.verdict == VERDICT_READY
    assert report.is_ready is True
    assert report.blocking_failures == ()
    assert report.failed_scenarios == ()


def test_every_required_operational_scenario_is_present(report):
    ids = {scenario.scenario_id for scenario in report.scenarios}

    assert {
        "halt_new_risk_while_healthy",
        "halt_new_risk_while_ai_queue_blocked",
        "cancel_open_through_safety_path",
        "duplicate_break_glass_command",
        "restart_with_break_glass_active",
        "new_risk_order_rejected_after_break_glass",
        "ai_decision_cannot_override_safety",
        "reconciliation_after_emergency_action",
        "ambiguous_cancel_unknown_pending_reconciliation",
        "operator_acknowledgement_after_reconciliation",
        "restore_denied_before_reconciliation",
        "restore_denied_without_acknowledgement",
        "malformed_command_fails_closed",
        "journal_replay_of_safety_state",
        "primary_runtime_unavailable",
        "ai_queue_saturated",
        "journal_replay_after_crash",
        "venue_cancel_timeout",
        "venue_response_lost",
        "local_state_ahead_of_venue",
        "venue_state_ahead_of_local",
        "reconciliation_mismatch",
        "operator_acknowledgement_missing",
        "corrupted_breakglass_request",
        "restart_in_degraded",
        "restart_in_recovery_required",
        "no_concurrent_normal_and_emergency_writer",
        "testnet_endpoint_guard",
        "secret_leak_scan",
        "withdrawal_disabled_invariant",
        "mainnet_hostname_negative",
    } <= ids
    assert len(SCENARIOS) == len(report.scenarios)


def test_the_check_matrix_covers_every_review_area(report):
    areas = {item.area for item in report.items}

    assert {
        "Isolation",
        "Mainnet safety",
        "Secrets",
        "Operational control",
        "Event contracts",
        "Ownership",
        "Persistence",
        "Documentation",
        "Authorization",
        "Repository policy",
        "Known limitations",
    } <= areas


def test_limitations_are_recorded_as_informational_not_as_passes(report):
    informational = report.informational

    assert informational
    assert all(item.status == "informational" for item in informational)
    assert report.limitations == tuple(item.detail for item in informational)
    assert any("testnet" in item.item_id for item in informational)


def test_the_report_is_json_serializable_and_secret_free(report):
    payload = report.to_record()
    rendered = json.dumps(payload)

    assert payload["counts"]["blocking_failures"] == 0
    assert payload["counts"]["failed_scenarios"] == 0
    # The report may NAME the two environment variables whose absence it reports (that is not a
    # secret); a credential VALUE field or an authenticated header never appears.
    for token in ('"api_key"', '"apikey"', '"api_secret"', "x-mbx", "signature="):
        assert token not in rendered.lower()
    assert payload["notes"]


def test_the_markdown_matrix_is_reviewable_and_never_claims_live_authorization(report):
    markdown = report.to_markdown()

    assert VERDICT_READY in markdown
    assert "Break-glass checks" in markdown
    assert "Break-glass scenarios" in markdown
    assert "Retained limitations" in markdown
    assert "not live authorization" in markdown.lower()
    assert "live trading" not in report.verdict.lower()


def test_a_failing_required_item_forces_not_ready(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        audit_matrix,
        "run_break_glass_checks",
        lambda workspace: (
            BreakGlassItem("forced", "Test", True, "fail", "injected blocking failure"),
        ),
    )
    monkeypatch.setattr(audit_matrix, "run_all_scenarios", lambda workspace: ())

    forced = run_break_glass_audit(tmp_path)

    assert forced.verdict == VERDICT_NOT_READY
    assert forced.is_ready is False
    assert [item.item_id for item in forced.blocking_failures] == ["forced"]


def test_a_failing_scenario_forces_not_ready(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(audit_matrix, "run_break_glass_checks", lambda workspace: ())
    monkeypatch.setattr(
        audit_matrix,
        "run_all_scenarios",
        lambda workspace: (
            BreakGlassScenarioResult("forced", "forced", "fail", "injected scenario failure"),
        ),
    )

    forced = run_break_glass_audit(tmp_path)

    assert forced.verdict == VERDICT_NOT_READY
    assert [scenario.scenario_id for scenario in forced.failed_scenarios] == ["forced"]


def test_informational_items_never_block_the_verdict(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        audit_matrix,
        "run_break_glass_checks",
        lambda workspace: (BreakGlassItem("note", "Test", False, "informational", "not a blocker"),),
    )
    monkeypatch.setattr(audit_matrix, "run_all_scenarios", lambda workspace: ())

    assert run_break_glass_audit(tmp_path).verdict == VERDICT_READY
