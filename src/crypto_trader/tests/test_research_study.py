"""P5-01: synthetic end-to-end research dry run over the closed P4 pipeline (research only)."""
from __future__ import annotations

import copy
import json
import socket
import subprocess
from dataclasses import replace
from decimal import Decimal

import pytest

from crypto_trader import research_gate as gate
from crypto_trader import research_study as study
from crypto_trader.validation.research_path import AdmissibleResult, run_registered_trial
from crypto_trader.validation.walk_forward import WalkForward


def run(tmp_path, name="s", **kw):
    path = tmp_path / name
    return study.run_synthetic_study(path, confirm_synthetic_holdout=True, **kw)


@pytest.fixture(scope="module")
def signal(tmp_path_factory):
    return study.run_synthetic_study(tmp_path_factory.mktemp("signal") / "run", scenario="signal",
                                     confirm_synthetic_holdout=True)


# ---------------------------------------------------------------- end-to-end (AC-1..AC-5, AC-10)
def test_signal_scenario_runs_the_whole_pipeline(signal):
    o = signal
    assert o.label == "SYNTHETIC_DRY_RUN" and o.scenario == "signal"
    assert [r["name"] for r in o.ranking] == list(study.FAMILY)
    assert o.gate_report.decision in ("RESEARCH_APPROVED", "RESEARCH_REJECTED", "INSUFFICIENT_EVIDENCE")
    assert o.gate_report.measures["completed_trials"] == 4
    assert o.gate_report.selected_trial_id == o.selected_trial_id
    assert o.evidence["gate_report_sha256"] == o.gate_report.report_sha256
    assert set(o.evidence) >= {"preregistration_sha256", "used_rows_sha256", "holdout_result",
                               "gate_report_sha256", "registry_head", "ledger_head"}
    assert o.permits_promotion is False and o.permits_paper_or_trading is False
    assert o.operational_authorization == "NONE"


def test_preregistration_declares_family_rule_generator_holdout_and_policy(signal):
    doc = signal.preregistration
    assert doc["label"] == "SYNTHETIC_DRY_RUN"
    assert doc["family"] == list(study.FAMILY)
    assert doc["selection_rule"] == study.SELECTION_RULE
    assert doc["generator"]["seed"] == study.SCENARIOS["signal"]["seed"]
    assert set(doc) >= {"holdout", "holdout_commitment", "approval_policy", "walk_forward_plan"}
    assert gate.parse_approval_policy(doc)["min_dsr"] == Decimal("0.95")


def test_selection_follows_the_registered_rule(signal):
    """Control 1 / owner note: the gate alone does not check the candidate is the top Sharpe."""
    ranked = [r for r in signal.ranking if r["sharpe"] is not None]
    best = max(Decimal(r["sharpe"]) for r in ranked)
    expected = next(r for r in signal.ranking if r["sharpe"] is not None and Decimal(r["sharpe"]) == best)
    assert signal.selected_name == expected["name"] and signal.selected_trial_id == expected["trial_id"]
    check = study.reverify_study(signal)
    assert check.selection_ok and check.reasons == ()


@pytest.mark.parametrize("rows, expected", [
    ([("a", "0.5"), ("b", "0.7"), ("c", "0.7"), ("d", None)], "b"),       # tie: earliest in family order
    ([("a", None), ("b", "-0.1"), ("c", "-0.2"), ("d", None)], "b"),      # missing Sharpe ranked last
    ([("a", "0.1"), ("b", "0.1"), ("c", "0.1"), ("d", "0.1")], "a"),
])
def test_selection_rule_ties_and_missing_sharpe(rows, expected):
    ranking = [{"name": n, "trial_id": n, "sharpe": s} for n, s in rows]
    assert study.select_by_rule(study.SELECTION_RULE, ranking)["name"] == expected


def test_holdout_is_opened_only_after_every_trial_verified(signal):
    events = signal.ledger_events
    assert [k for k, _ in events] == ["ACCESS_REQUESTED", "GRANTED", "DISCLOSED", "RETIRED"]
    assert events[0][1]["candidate"]["trial_id"] == signal.selected_trial_id
    assert signal.trials_verified_before_holdout == 4


# ---------------------------------------------------------------- re-run comparability (control 2, AC-6)
def test_rerun_gives_the_same_results_and_decision(signal, tmp_path):
    again = run(tmp_path, scenario="signal")
    assert study.comparable(again) == study.comparable(signal)
    assert again.selected_trial_id != signal.selected_trial_id           # identities differ
    assert again.evidence["registry_head"] != signal.evidence["registry_head"]


def test_comparable_view_excludes_ids_times_and_dependent_hashes(signal):
    view = study.comparable(signal)
    text = repr(view)
    assert signal.selected_trial_id not in text and signal.evidence["registry_head"] not in text
    assert signal.gate_report.report_sha256 not in text
    assert view["gate"]["decision"] == signal.gate_report.decision
    assert view["selected_name"] == signal.selected_name


def _with_holdout(outcome, edit):
    metrics = copy.deepcopy(outcome.holdout_report.metrics)
    edit(metrics)
    return replace(outcome, holdout_report=replace(outcome.holdout_report, metrics=metrics))


def _with_walk_forward(outcome, index, edit):
    result = outcome.results[index]
    metrics = json.loads(result.metrics_json)
    edit(metrics)
    forged = AdmissibleResult(result.trial_id, result.study_id, result.binding_hash,
                              json.dumps(metrics), result.output_hash)
    return replace(outcome, results=outcome.results[:index] + (forged,) + outcome.results[index + 1:])


def _set(path, value):
    def edit(metrics):
        node = metrics
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return edit


@pytest.mark.parametrize("label, make", [
    # Astra's six single edits
    ("wf position", lambda o: _with_walk_forward(o, 0, _set(("folds", 0, "positions", 0), "0.5"))),
    ("holdout costs[0]", lambda o: _with_holdout(o, _set(("costs", 0), "9"))),
    ("holdout net[0]", lambda o: _with_holdout(o, _set(("net_excess_returns", 0), "9"))),
    ("holdout gross[0]", lambda o: _with_holdout(o, _set(("gross_excess_returns", 0), "9"))),
    ("holdout turnover", lambda o: _with_holdout(o, _set(("turnover",), "9"))),
    ("holdout total_cost", lambda o: _with_holdout(o, _set(("total_cost",), "9"))),
    # fold and aggregate statistics, windows, settings
    ("wf fold mean", lambda o: _with_walk_forward(o, 1, _set(("folds", 2, "mean"), "9"))),
    ("wf fold sharpe", lambda o: _with_walk_forward(o, 2, _set(("folds", 1, "sharpe"), "9"))),
    ("wf fold compounded", lambda o: _with_walk_forward(o, 3, _set(("folds", 0, "compounded_net_excess_return"), "9"))),
    ("wf fold costs", lambda o: _with_walk_forward(o, 1, _set(("folds", 1, "costs", 5), "9"))),
    ("wf fold gross", lambda o: _with_walk_forward(o, 1, _set(("folds", 1, "gross_excess_returns", 5), "9"))),
    ("wf fold net", lambda o: _with_walk_forward(o, 1, _set(("folds", 1, "net_excess_returns", 5), "9"))),
    ("wf fold turnover", lambda o: _with_walk_forward(o, 1, _set(("folds", 1, "turnover"), "9"))),
    ("wf fold total_cost", lambda o: _with_walk_forward(o, 1, _set(("folds", 1, "total_cost"), "9"))),
    ("wf fold window", lambda o: _with_walk_forward(o, 0, _set(("folds", 0, "oos_start_utc_ns"), "1"))),
    ("wf fold cutoff", lambda o: _with_walk_forward(o, 0, _set(("folds", 0, "train_cutoff_utc_ns"), "1"))),
    ("wf aggregate sharpe", lambda o: _with_walk_forward(o, 0, _set(("aggregate", "sharpe"), "9"))),
    ("wf aggregate total_cost", lambda o: _with_walk_forward(o, 0, _set(("aggregate", "total_cost"), "9"))),
    ("wf frozen fee", lambda o: _with_walk_forward(o, 0, _set(("config", "walk_forward", "fee_bps"), "0"))),
    ("wf strategy config", lambda o: _with_walk_forward(o, 0, _set(("config", "strategy", "name"), "x"))),
    ("wf used rows", lambda o: _with_walk_forward(o, 0, _set(("data", "used_rows_sha256"), "0" * 64))),
    ("holdout positions", lambda o: _with_holdout(o, _set(("positions", 3), "0.5"))),
    ("holdout mean", lambda o: _with_holdout(o, _set(("mean",), "9"))),
    ("holdout sharpe", lambda o: _with_holdout(o, _set(("sharpe",), "9"))),
    ("holdout compounded", lambda o: _with_holdout(o, _set(("compounded_net_excess_return",), "9"))),
    ("holdout cost rate", lambda o: _with_holdout(o, _set(("cost_rate",), "0"))),
    ("holdout window", lambda o: _with_holdout(o, _set(("holdout", "start_utc_ns"), "1"))),
    ("holdout cutoff", lambda o: _with_holdout(o, _set(("train_cutoff_utc_ns",), "1"))),
    ("holdout rows digest", lambda o: _with_holdout(o, _set(("data", "holdout_rows_sha256"), "0" * 64))),
    ("holdout frozen config", lambda o: _with_holdout(o, _set(("candidate", "config_sha256"), "0" * 64))),
])
def test_comparable_detects_every_single_computational_change(signal, label, make):
    assert study.comparable(make(signal)) != study.comparable(signal), label


@pytest.mark.parametrize("make", [
    lambda o: _with_holdout(o, _set(("candidate", "trial_id"), "00000000-0000-4000-8000-000000000000")),
    lambda o: _with_holdout(o, _set(("candidate", "output_hash"), "0" * 64)),
    lambda o: replace(o, selected_trial_id="00000000-0000-4000-8000-000000000000"),
    lambda o: replace(o, evidence={**o.evidence, "registry_head": "0" * 64, "ledger_head": "0" * 64,
                                   "gate_report_sha256": "0" * 64, "holdout_result": "x",
                                   "selected_output": "x"}),
], ids=["holdout trial_id", "holdout output_hash", "selected trial_id", "run evidence"])
def test_only_run_generated_identities_and_dependent_hashes_are_excluded(signal, make):
    assert study.comparable(make(signal)) == study.comparable(signal)


def test_comparable_keeps_the_full_results_in_family_order(signal):
    view = study.comparable(signal)
    assert [c["name"] for c in view["walk_forward"]] == list(study.FAMILY)
    assert all(len(c["metrics"]["folds"]) == 3 for c in view["walk_forward"])
    assert view["holdout_metrics"]["positions"] == signal.holdout_report.metrics["positions"]
    assert view["gate"]["checks"] == [dict(c) for c in signal.gate_report.checks]
    assert view["preregistration"] == signal.preregistration


def test_a_missing_projection_path_fails_loudly(signal):
    broken = _with_holdout(signal, lambda m: m["candidate"].pop("trial_id"))
    with pytest.raises(study.StudyError, match="comparable_path_missing"):
        study.comparable(broken)


# ---------------------------------------------------------------- noise control (control 3)
def test_noise_scenario_completes_without_an_assumed_outcome(tmp_path):
    o = run(tmp_path, scenario="noise")
    assert o.preregistration["generator"]["drift"] == "0"
    assert o.gate_report.decision in ("RESEARCH_APPROVED", "RESEARCH_REJECTED", "INSUFFICIENT_EVIDENCE")
    assert study.reverify_study(o).selection_ok


# ---------------------------------------------------------------- synthetic-only confirmation (control 4)
@pytest.mark.parametrize("confirm", [False, None, "yes", 1])
def test_holdout_confirmation_must_be_explicit_and_is_synthetic_only(tmp_path, confirm):
    with pytest.raises(study.StudyError, match="synthetic_holdout_not_confirmed"):
        study.run_synthetic_study(tmp_path / "x", scenario="signal", confirm_synthetic_holdout=confirm)
    assert not (tmp_path / "x" / "holdout.sqlite").exists()


def test_only_registered_synthetic_scenarios_exist(tmp_path):
    with pytest.raises(study.StudyError, match="unknown_scenario"):
        run(tmp_path, scenario="real")
    assert set(study.SCENARIOS) == {"signal", "noise"}


def test_refuses_an_existing_workdir(tmp_path):
    (tmp_path / "used").mkdir()
    with pytest.raises(study.StudyError, match="workdir_not_new"):
        run(tmp_path, name="used", scenario="signal")


# ---------------------------------------------------------------- failure cases (AC-9)
def test_data_changed_after_registration_is_insufficient(signal):
    edited = dict(signal.preregistration)
    edited["generator"] = {**edited["generator"], "seed": edited["generator"]["seed"] + 1}
    check = study.reverify_study(signal, preregistration=edited)
    assert not check.selection_ok                       # no ranking from unverifiable results
    assert check.gate_report.decision == "INSUFFICIENT_EVIDENCE"
    assert any("binding_or_preregistration_mismatch" in r for r in check.gate_report.reasons)


def test_dropping_a_family_member_is_insufficient(signal):
    check = study.reverify_study(signal, results=signal.results[1:])
    assert check.gate_report.decision == "INSUFFICIENT_EVIDENCE"
    assert "completed_results_missing_or_extra" in check.gate_report.reasons
    assert not check.selection_ok


def test_a_duplicated_family_member_is_refused(signal):
    check = study.reverify_study(signal, results=signal.results + signal.results[:1])
    assert not check.selection_ok and "selection_not_per_rule" in check.reasons
    assert check.gate_report.decision == "INSUFFICIENT_EVIDENCE"


def test_a_candidate_other_than_the_rule_choice_is_refused(signal):
    other = next(r for r in signal.ranking if r["trial_id"] != signal.selected_trial_id)
    forged = replace(signal, selected_trial_id=other["trial_id"], selected_name=other["name"])
    check = study.reverify_study(forged)
    assert not check.selection_ok and "selection_not_per_rule" in check.reasons


def test_the_registered_budget_refuses_any_attempt_beyond_the_family(tmp_path):
    """n_max equals the family size: a fifth attempt cannot be allocated at all."""
    o = run(tmp_path, scenario="signal")
    rows = study.generate_rows(o.preregistration)
    from crypto_trader.validation.contracts import RegistryError
    from crypto_trader.validation.registry import TrialRegistry
    with TrialRegistry(o.workdir / "trials.sqlite") as reg:
        extra = WalkForward(rows.walk_forward, preregistration=o.preregistration,
                            manifest=study.MANIFEST, source_sha256=o.evidence["source_sha256"],
                            fit=study.fit, decide=study.STRATEGIES["buy_and_hold"])
        with pytest.raises(RegistryError, match="capacity exhausted"):
            run_registered_trial(reg, study.STUDY_ID,
                                 {"strategy": {"name": "unregistered-extra", "family_index": 9},
                                  "walk_forward": o.preregistration["walk_forward_plan"]},
                                 study.MANIFEST, extra.evaluate)
        assert reg.status_counts(study.STUDY_ID) == {"COMPLETED": 4}
    check = study.reverify_study(o)
    assert check.selection_ok
    assert study.comparable(replace(o, gate_report=check.gate_report)) == study.comparable(o)


def test_outcome_cannot_carry_any_authority(signal):
    for override in ({"permits_promotion": True}, {"permits_paper_or_trading": True},
                     {"operational_authorization": "PAPER"}, {"label": "REAL"}):
        with pytest.raises(study.StudyError):
            replace(signal, **override)


# ---------------------------------------------------------------- no network (AC-8)
def test_runs_with_network_and_process_launch_denied(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network or process launch attempted")
    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    o = run(tmp_path, scenario="signal")
    assert o.gate_report.decision in ("RESEARCH_APPROVED", "RESEARCH_REJECTED", "INSUFFICIENT_EVIDENCE")
