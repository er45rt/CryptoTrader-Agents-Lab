"""Phase 15: the mainnet read-only dry-run pipeline and its hard no-submit guarantee."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from crypto_trader.breakglass import BreakGlassActiveError
from crypto_trader.execution import ExecutionTimeInForce
from crypto_trader.execution.binance_testnet import ScriptedTransport
from crypto_trader.mainnet import (
    DryRunDisposition,
    DryRunError,
    MainnetMutationForbiddenError,
    MainnetReadOnlyClient,
)
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.recovery import reconstruct_order_states
from crypto_trader.safety import SafetyMode
from crypto_trader.tests import mainnet_fixtures as fx
from crypto_trader.tests.production_fixtures import persist_safety_state


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as active:
        yield active


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _pipeline(journal, state_dir, tmp_path, **overrides):
    return fx.pipeline(
        journal=journal, state_dir=state_dir, tmp_path=tmp_path, **overrides
    )


# --- startup ----------------------------------------------------------------

def test_start_verifies_the_read_only_permission_profile_first(journal, state_dir, tmp_path):
    calls: list[str] = []

    def permission_check():
        calls.append("permissions")
        return fx.restrictions()

    pipeline = _pipeline(journal, state_dir, tmp_path, permission_check=permission_check)
    record = pipeline.start()

    assert calls == ["permissions"]
    assert record["mode"] == "mainnet_readonly_dry_run"
    assert record["permission_scope"] == "read_only"
    assert record["fence_term"] == 1
    assert record["safety_mode"] == "normal"
    assert record["no_submit_gate"] == "armed"
    assert pipeline.status()["mainnet_mutation_attempts"] == 0


def test_start_refuses_a_profile_that_is_not_read_only(journal, state_dir, tmp_path):
    pipeline = _pipeline(
        journal,
        state_dir,
        tmp_path,
        permission_check=lambda: SimpleNamespace(is_read_only=False),
    )

    with pytest.raises(DryRunError):
        pipeline.start()
    assert pipeline.is_ready() is False


def test_start_refuses_a_halted_persisted_posture(journal, state_dir, tmp_path):
    persist_safety_state(state_dir, mode="degraded", kill_switch_engaged=True)
    pipeline = _pipeline(journal, state_dir, tmp_path)

    with pytest.raises(DryRunError):
        pipeline.start()


def test_start_refuses_while_a_break_glass_epoch_is_open(journal, state_dir, tmp_path):
    control = fx.control_for(journal, state_dir)
    control.handle(fx.operator_command("halt_new_risk", seed="dryrun-halt-1"))
    pipeline = _pipeline(journal, state_dir, tmp_path, control=control)

    with pytest.raises(DryRunError):
        pipeline.start()
    assert control.status().new_risk_permitted is False


# --- the complete pipeline --------------------------------------------------

def test_a_complete_cycle_records_would_submit_without_any_mutation(
    journal, state_dir, tmp_path
):
    recorder = fx.soak(tmp_path)
    pipeline = _pipeline(journal, state_dir, tmp_path, soak_instance=recorder)
    pipeline.start()

    result = pipeline.run_cycle()
    record = result.to_record()

    assert result.disposition is DryRunDisposition.WOULD_SUBMIT
    assert record["disposition"] == "WOULD_SUBMIT"
    assert record["reason_code"] == "dry_run_would_submit"
    assert record["stages"] == [
        "market_snapshot",
        "decision",
        "validity:accepted",
        "risk:allowed",
        "permissions:read_only",
        "safety:fence_authorized",
        "filters:validated",
        "spread:admissible",
        "canary_entry:limit_fok",
        "oms:dry_run_representation",
        "no_submit_gate:would_submit",
    ]
    assert record["order"]["submitted"] is False
    assert record["order"]["time_in_force"] == "FOK"
    assert record["mainnet_mutation_requests"] == 0
    assert pipeline.gate.mutation_attempts == 0
    assert recorder.state().counters.completed_pipeline_cycles == 1


def test_a_validity_rejection_is_observed_and_counted(journal, state_dir, tmp_path):
    recorder = fx.soak(tmp_path)
    stale = fx.intent("stale-1", market_event_ts=fx.BASE.replace(year=2020))
    pipeline = _pipeline(
        journal,
        state_dir,
        tmp_path,
        decision_source=lambda snapshot: stale,
        soak_instance=recorder,
    )
    pipeline.start()

    result = pipeline.run_cycle()

    assert result.disposition is DryRunDisposition.WOULD_REJECT
    assert result.reason_code in {"max_age_exceeded", "valid_until_exceeded"}
    assert "validity:stale" in result.stages
    assert recorder.state().counters.validity_rejections == 1


def test_halt_new_risk_blocks_the_pipeline_and_is_counted(journal, state_dir, tmp_path):
    control = fx.control_for(journal, state_dir)
    recorder = fx.soak(tmp_path)
    pipeline = _pipeline(
        journal, state_dir, tmp_path, control=control, soak_instance=recorder
    )
    pipeline.start()
    control.handle(fx.operator_command("halt_new_risk", seed="dryrun-halt-2"))

    result = pipeline.run_cycle()

    assert result.disposition is DryRunDisposition.WOULD_REJECT
    assert result.reason_code == "trading_halted"
    assert pipeline.gate.mutation_attempts == 0
    assert recorder.state().counters.halt_exercises == 1


def test_an_emergency_takeover_invalidates_the_running_dry_run_writer(
    journal, state_dir, tmp_path
):
    control = fx.control_for(journal, state_dir)
    recorder = fx.soak(tmp_path)
    pipeline = _pipeline(
        journal, state_dir, tmp_path, control=control, soak_instance=recorder
    )
    pipeline.start()

    control.handle(fx.operator_command("force_recovery_required", seed="dryrun-takeover"))
    result = pipeline.run_cycle()

    assert result.disposition is DryRunDisposition.WOULD_REJECT
    assert result.reason_code == "trading_halted"
    assert recorder.state().counters.halt_exercises == 1
    assert control.status().new_risk_permitted is False


def test_a_competing_normal_writer_makes_the_dry_run_stale(journal, state_dir, tmp_path):
    control = fx.control_for(journal, state_dir)
    pipeline = _pipeline(journal, state_dir, tmp_path, control=control)
    pipeline.start()

    control.claim_normal_writer(owner_id="normal:other-runtime")
    result = pipeline.run_cycle()

    assert result.reason_code == "stale_writer_fence"


def test_an_old_market_snapshot_can_never_be_reused(journal, state_dir, tmp_path):
    issued = {"count": 0}

    def decision_source(snapshot):
        issued["count"] += 1
        return fx.intent(f"reuse-{issued['count']}")

    pipeline = _pipeline(journal, state_dir, tmp_path, decision_source=decision_source)
    pipeline.start()

    first = pipeline.run_cycle()
    second = pipeline.run_cycle()

    assert first.disposition is DryRunDisposition.WOULD_SUBMIT
    assert second.disposition is DryRunDisposition.WOULD_REJECT
    assert second.reason_code == "old_market_snapshot_reuse_refused"
    assert pipeline.gate.mutation_attempts == 0


@pytest.mark.parametrize("time_in_force", [ExecutionTimeInForce.IOC, ExecutionTimeInForce.GTC])
def test_a_non_fok_decision_is_refused_by_the_canary_policy(
    journal, state_dir, tmp_path, time_in_force
):
    pipeline = _pipeline(
        journal,
        state_dir,
        tmp_path,
        decision_source=lambda snapshot: fx.intent("non-fok", time_in_force=time_in_force),
    )
    pipeline.start()

    result = pipeline.run_cycle()

    assert result.disposition is DryRunDisposition.WOULD_REJECT
    assert result.reason_code == "canary_policy:CanaryPolicyError"


def test_a_too_wide_spread_is_rejected(journal, state_dir, tmp_path):
    pipeline = _pipeline(
        journal,
        state_dir,
        tmp_path,
        quote_provider=lambda symbol: (Decimal("26000.00"), Decimal("27000.00")),
    )
    pipeline.start()

    result = pipeline.run_cycle()

    assert result.disposition is DryRunDisposition.WOULD_REJECT
    assert result.reason_code == "canary_policy:CanaryPolicyError"


def test_a_permission_change_at_the_boundary_is_rejected(journal, state_dir, tmp_path):
    calls = {"count": 0}

    def permission_check():
        calls["count"] += 1
        if calls["count"] == 1:
            return fx.restrictions()
        return SimpleNamespace(is_read_only=False)

    pipeline = _pipeline(journal, state_dir, tmp_path, permission_check=permission_check)
    pipeline.start()

    result = pipeline.run_cycle()

    assert result.reason_code == "permission_profile_no_longer_read_only"
    assert "permissions:changed" in result.stages
    assert pipeline.gate.mutation_attempts == 0


# --- the hard no-submit guarantee -------------------------------------------

def test_a_venue_submission_attempt_is_unreachable(journal, state_dir, tmp_path):
    pipeline = _pipeline(journal, state_dir, tmp_path)
    pipeline.start()
    order = pipeline.run_cycle().order

    assert order is not None
    with pytest.raises(MainnetMutationForbiddenError):
        pipeline.try_to_submit(order)

    assert pipeline.gate.mutation_attempts == 1
    assert pipeline.status()["mainnet_mutation_attempts"] == 1


def test_a_client_backed_cycle_only_ever_sends_get(journal, state_dir, tmp_path):
    transport = ScriptedTransport(
        responses=[
            fx.response(200, fx.restrictions_body()),
            fx.response(200, fx.restrictions_body()),
            fx.response(200, fx.exchange_info_body()),
            fx.response(200, fx.book_ticker_body()),
        ]
    )
    client = fx.client(transport)
    pipeline = _pipeline(
        journal,
        state_dir,
        tmp_path,
        client_instance=client,
        permission_check=client.verify_read_only_permissions,
    )
    pipeline.start()

    result = pipeline.run_cycle()

    assert result.disposition is DryRunDisposition.WOULD_SUBMIT
    assert [request.method for request in transport.requests] == ["GET"] * 4
    assert not any(request.is_write for request in transport.requests)
    assert not any("order" in request.url.split("?")[0].lower() for request in transport.requests)


# --- restart / safety semantics ---------------------------------------------

def test_unresolved_reconciliation_blocks_a_restart(journal, state_dir, tmp_path):
    from crypto_trader.mainnet import SendFault, inject_post_send_ambiguity
    from uuid import UUID

    inject_post_send_ambiguity(
        journal=journal,
        policy=fx.recovery_policy(),
        fault=SendFault.RESPONSE_LOST,
        symbol=fx.SYMBOL,
        client_order_id="ct-phase15-restart-1",
        correlation_id=UUID("11111111-1111-4111-8111-111111111111"),
        occurred_at=fx.BASE,
        quantity=Decimal("0.01000"),
    )

    pipeline = _pipeline(journal, state_dir, tmp_path)
    with pytest.raises(DryRunError):
        pipeline.start()


def test_a_planned_restart_resumes_and_is_recorded(journal, state_dir, tmp_path):
    recorder = fx.soak(tmp_path)
    recorder.record_cycle(completed=True)

    resumed = recorder.resume(planned=True)

    assert resumed.planned_restarts == 1
    assert resumed.counters.restart_recovery_exercises == 1
    pipeline = _pipeline(journal, state_dir, tmp_path, soak_instance=recorder)
    pipeline.start()
    assert pipeline.run_cycle().disposition is DryRunDisposition.WOULD_SUBMIT


def test_operator_acknowledgement_semantics_remain_intact(journal, state_dir, tmp_path):
    control = fx.control_for(journal, state_dir)
    _pipeline(journal, state_dir, tmp_path, control=control).start()
    control.handle(fx.operator_command("halt_new_risk", seed="dryrun-ack-halt"))
    control.handle(fx.operator_command("reconcile", seed="dryrun-ack-reconcile"))

    denied = control.handle(fx.operator_command("restore_normal", seed="dryrun-ack-restore-1"))
    assert denied.disposition == "rejected"
    assert denied.reason_code == "restore_denied_acknowledgement_missing"

    control.handle(fx.operator_command("acknowledge", seed="dryrun-ack"))
    restored = control.handle(fx.operator_command("restore_normal", seed="dryrun-ack-restore-2"))
    assert restored.accepted
    assert restored.resulting_safety_state == "normal"


def test_an_ai_or_strategy_path_cannot_bypass_safety_or_execution(journal, state_dir, tmp_path):
    control = fx.control_for(journal, state_dir)
    pipeline = _pipeline(journal, state_dir, tmp_path, control=control)
    pipeline.start()
    control.handle(fx.operator_command("halt_new_risk", seed="dryrun-ai-halt"))

    with pytest.raises(BreakGlassActiveError):
        control.runtime_request_mode_change(source="strategy", mode=SafetyMode.NORMAL)

    assert pipeline.gate.is_read_only is True
    assert not any(
        hasattr(pipeline.gate, name)
        for name in ("enable", "disable", "enable_live", "set_live", "set_mode")
    )
    assert pipeline.run_cycle().disposition is DryRunDisposition.WOULD_REJECT


def test_the_journal_and_replay_stay_deterministic(tmp_path):
    def run(root: Path):
        root.mkdir(parents=True, exist_ok=True)
        with SQLiteEventJournal(root / "journal.db") as journal:
            pipeline = _pipeline(journal, root / "state", root)
            pipeline.start()
            pipeline.run_cycle()
            reconstructed, _ = reconstruct_order_states(journal)
            return (
                sorted(event.event_type for event in journal.replay()),
                [state.order_reference for state in reconstructed.states()],
            )

    assert run(tmp_path / "a") == run(tmp_path / "b")
