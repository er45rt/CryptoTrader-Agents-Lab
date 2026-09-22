"""Phase 12: the readiness matrix, the mechanical verdict and its limits."""

import json
from pathlib import Path

import pytest

from crypto_trader.audit import (
    VERDICT_NOT_READY,
    VERDICT_READY,
    AuditReport,
    ReadinessItem,
    ScenarioResult,
    run_audit,
)
from crypto_trader.audit import matrix as audit_matrix


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> AuditReport:
    """One audit run shared by the read-only assertions in this module."""
    workspace = tmp_path_factory.mktemp("audit-readiness")
    return run_audit(workspace)


def test_the_verdict_is_ready_for_owner_review(report):
    assert report.verdict == VERDICT_READY
    assert report.is_ready is True


def test_every_required_readiness_item_passes(report):
    blocking = report.blocking_failures

    assert blocking == (), f"blocking readiness failures: {[i.item_id for i in blocking]}"
    assert len([item for item in report.items if item.required]) == 15


def test_every_fault_injection_scenario_passes(report):
    assert report.failed_scenarios == ()
    assert len(report.scenarios) == 15


def test_the_matrix_covers_every_review_area(report):
    areas = {item.area for item in report.items}

    assert {
        "Dependencies",
        "Domain isolation",
        "Mainnet safety",
        "Secrets",
        "Hard risk",
        "Documentation",
        "Known limitations",
    } <= areas


def test_limitations_are_recorded_as_informational_not_as_passes(report):
    informational = [item for item in report.items if not item.required]

    assert {item.item_id for item in informational} == {
        "informational_l2_limitations",
        "informational_live_gaps",
    }
    assert all(item.status == "informational" for item in informational)


def test_the_report_is_json_serializable_and_secret_free(report):
    payload = report.to_record()
    rendered = json.dumps(payload)

    assert payload["counts"]["blocking_failures"] == 0
    # No credential-shaped field: "Secrets" as an AREA NAME is fine, a value never is.
    for token in ("api_key", "apikey", "api_secret", "x-mbx", "signature="):
        assert token not in rendered.lower()
    assert payload["notes"]


def test_the_markdown_matrix_is_reviewable(report):
    markdown = report.to_markdown()

    assert VERDICT_READY in markdown
    assert "Readiness matrix" in markdown
    assert "Fault-injection scenarios" in markdown
    assert "Technical readiness is not live authorization." in markdown


def test_technical_readiness_is_not_live_authorization(report):
    assert any("not live authorization" in note for note in report.notes)
    assert "live" not in report.verdict.lower()


# --- the verdict is mechanical, not asserted ---------------------------------

def test_a_failing_required_item_forces_not_ready(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        audit_matrix,
        "run_static_checks",
        lambda: (
            ReadinessItem("forced", "Test", True, "fail", "injected blocking failure"),
        ),
    )
    monkeypatch.setattr(audit_matrix, "run_all_scenarios", lambda workspace: ())

    forced = run_audit(tmp_path)

    assert forced.verdict == VERDICT_NOT_READY
    assert forced.is_ready is False
    assert [item.item_id for item in forced.blocking_failures] == ["forced"]


def test_a_failing_scenario_forces_not_ready(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(audit_matrix, "run_static_checks", lambda: ())
    monkeypatch.setattr(
        audit_matrix,
        "run_all_scenarios",
        lambda workspace: (
            ScenarioResult("forced", "forced", "fail", "injected scenario failure"),
        ),
    )

    forced = run_audit(tmp_path)

    assert forced.verdict == VERDICT_NOT_READY
    assert [scenario.scenario_id for scenario in forced.failed_scenarios] == ["forced"]


def test_informational_items_never_block_the_verdict(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        audit_matrix,
        "run_static_checks",
        lambda: (
            ReadinessItem("note", "Test", False, "informational", "not a blocker"),
        ),
    )
    monkeypatch.setattr(audit_matrix, "run_all_scenarios", lambda workspace: ())

    assert run_audit(tmp_path).verdict == VERDICT_READY
