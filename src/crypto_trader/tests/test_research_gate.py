"""P4-06: research strategy approval gate over the closed P4-02..P4-05 evidence (research only)."""
from __future__ import annotations

import inspect
import random
from dataclasses import replace
from decimal import Decimal

import pytest

from crypto_trader import research_gate as gate
from crypto_trader.validation import holdout as ho
from crypto_trader.validation import walk_forward as wf
from crypto_trader.validation.canonical import content_hash
from crypto_trader.validation.contracts import StudySpec
from crypto_trader.validation.registry import TrialRegistry
from crypto_trader.validation.research_path import run_registered_trial

STUDY = "11111111-1111-4111-8111-111111111111"
PATHS = ("src/crypto_trader/paper/session.py",)
MANIFEST = {PATHS[0]: "b" * 64}
DAY = 86_400 * 10**9
BASE = 1_735_689_600 * 10**9   # 2025-01-01T00:00:00Z
WF_BOUNDS = (100, 132, 164, 196)   # 3 folds x 32 = 96 OOS rows = 8 slices x 12
HOLD = (200, 232)
POLICY = {"min_dsr": "0.95", "max_pbo": "0.20", "holdout_net_sharpe_above": "0",
          "holdout_compounded_return_above": "0", "min_completed_trials": 2, "pbo_slice_count": 8}


def day(i):
    return BASE + i * DAY


def make_rows(cls, n=232, seed=21, mean=0.003):
    rng = random.Random(seed)
    returns = [f"{rng.gauss(mean, 0.01):.8f}" for _ in range(n)]
    return tuple(cls(
        row_id=f"r{i:05d}", decision_utc_ns=day(i), feature_available_utc_ns=day(i),
        revision_first_seen_utc_ns=day(i), exposure_end_utc_ns=day(i + 1),
        excess_return=returns[i], features=(("signal", returns[i - 1] if i else "0"),),
    ) for i in range(n))


def plan(bounds=WF_BOUNDS):
    return {"warmup_start_utc_ns": str(day(0)), "anchor_start_utc_ns": str(day(10)),
            "oos_boundaries_utc_ns": [str(day(d)) for d in bounds], "purge_ns": str(DAY),
            "period": "1d", "fee_bps": "10", "slippage_bps": "5"}


def fit_mean(features, labels, cfg):
    return {"mean": f"{sum(float(x.excess_return) for x in labels) / len(labels):.10f}"}


def decide_sign(state, now_utc_ns, features, cfg):
    current = max(features, key=lambda f: f.decision_utc_ns)
    return "1" if float(dict(current.features)["signal"]) > 0 else "0"


STRATEGIES = {
    "always-long": lambda s, n, f, c: "1",
    "half-long": lambda s, n, f, c: "0.5",
    "sign-momentum": decide_sign,
}


class Study:
    """Registered study with walk-forward trials and one completed sealed holdout evaluation."""

    def __init__(self, path, *, seed=21, mean=0.006, policy=POLICY, selected="always-long",
                 names=("always-long", "half-long", "sign-momentum"), bounds=WF_BOUNDS,
                 failed=0, holdout_for=None):
        self.path = path
        hold_rows_all = make_rows(ho.PanelRow, seed=seed, mean=mean)
        self.history = hold_rows_all[:HOLD[0]]
        self.holdout_rows = hold_rows_all[HOLD[0]:HOLD[1]]
        self.panel = make_rows(wf.PanelRow, seed=seed, mean=mean)[:HOLD[0]]
        self.doc = {"study": "p4-06 synthetic", "holdout": {
            "start_utc_ns": str(day(HOLD[0])), "end_utc_ns": str(day(HOLD[1]))},
            "holdout_commitment": {"rows_sha256": ho.holdout_rows_sha256(self.holdout_rows),
                                   "row_count": len(self.holdout_rows)}}
        if policy is not None:
            self.doc["approval_policy"] = policy
        self.registry = TrialRegistry(path / "trials.sqlite")
        self.registry.register_study(StudySpec(
            study_id=STUDY, preregistration_hash=content_hash(wf.PREREGISTRATION_DOMAIN, self.doc),
            n_max=40, allowed_paths=PATHS))
        self.results = {}
        for name in names:
            runner = wf.WalkForward(self.panel, preregistration=self.doc, manifest=MANIFEST,
                                    source_sha256="c" * 64, fit=fit_mean, decide=STRATEGIES[name])
            self.results[name] = run_registered_trial(
                self.registry, STUDY, {"strategy": {"name": name}, "walk_forward": plan(bounds)},
                MANIFEST, runner.evaluate)
        for j in range(failed):
            def broken(receipt, cfg):
                raise ValueError("evaluation error")
            with pytest.raises(ValueError):
                run_registered_trial(self.registry, STUDY, {"failed": j}, MANIFEST, broken)
        self.selected = self.results[selected]
        self.ledger = ho.HoldoutLedger(path / "holdout.sqlite")
        held = self.results[holdout_for or selected]
        rid = ho.request_access(self.ledger, self.registry, study_id=STUDY, candidate=held,
                                preregistration=self.doc, manifest=MANIFEST, history=self.history)
        ho.grant(self.ledger, rid, owner_confirms=True)
        self.holdout_report = ho.evaluate_holdout(
            self.ledger, self.registry, rid, candidate=held, preregistration=self.doc,
            manifest=MANIFEST, history=self.history, holdout_rows=self.holdout_rows,
            fit=fit_mean, decide=STRATEGIES[holdout_for or selected])

    def gate(self, **over):
        kwargs = dict(study_id=STUDY, selected_trial_id=self.selected.trial_id,
                      results=tuple(self.results.values()), holdout_report=self.holdout_report,
                      preregistration=self.doc, manifest=MANIFEST, panel=self.panel,
                      history=self.history, holdout_rows=self.holdout_rows)
        kwargs.update(over)
        return gate.evaluate_strategy_gate(self.registry, self.ledger, **kwargs)

    def close(self):
        self.ledger.close()
        self.registry.close()


@pytest.fixture
def study(tmp_path):
    s = Study(tmp_path)
    yield s
    s.close()


def make(tmp_path, name, **kw):
    path = tmp_path / name
    path.mkdir()
    return Study(path, **kw)


# ---------------------------------------------------------------- decision paths
def test_complete_evidence_meeting_the_policy_is_research_approved(study):
    report = study.gate()
    assert report.decision == "RESEARCH_APPROVED", report.reasons
    assert [c["passed"] for c in report.checks] == [True, True, True, True]
    m = report.measures
    assert Decimal(m["dsr"]) >= Decimal("0.95") and Decimal(m["pbo"]) <= Decimal("0.20")
    assert Decimal(m["holdout_net_sharpe"]) > 0 and Decimal(m["holdout_compounded_net_excess_return"]) > 0
    assert m["completed_trials"] == 3 and m["oos_observations"] == 96
    assert report.label == "RESEARCH_APPROVAL_ONLY" and report.operational_authorization == "NONE"
    assert report.permits_promotion is False and report.permits_paper_or_trading is False


def test_measures_are_the_closed_modules_own_outputs(study):
    from crypto_trader.validation.dsr import deflated_sharpe
    from crypto_trader.validation.pbo import CandidateReturns, estimate_pbo
    report = study.gate()
    results = tuple(study.results.values())
    dsr = deflated_sharpe(study.registry, STUDY, results, selected_trial_id=study.selected.trial_id)
    ts = tuple(r.decision_utc_ns for r in study.panel if day(100) <= r.decision_utc_ns < day(196))
    pbo = estimate_pbo(ts, tuple(CandidateReturns(r.trial_id, tuple(Decimal(x) for x in r.metrics["excess_returns"]))
                                 for r in results), slice_count=8, search_family_size=3, score="mean_return")
    assert report.measures["dsr"] == str(dsr.dsr) and report.measures["pbo"] == str(pbo.pbo)
    assert report.measures["holdout_net_sharpe"] == study.holdout_report.metrics["sharpe"]


def test_complete_evidence_failing_a_criterion_is_research_rejected(tmp_path):
    strict = {**POLICY, "holdout_compounded_return_above": "10"}
    s = make(tmp_path, "a", policy=strict)
    try:
        report = s.gate()
    finally:
        s.close()
    assert report.decision == "RESEARCH_REJECTED"
    assert report.reasons == ("criterion_failed:holdout_compounded_net_excess_return",)
    assert report.permits_paper_or_trading is False and report.operational_authorization == "NONE"


def test_moderate_edge_is_rejected_on_dsr_not_adjusted(tmp_path):
    """Drift 0.003: DSR about 0.937 against the registered 0.95; the gate rejects."""
    s = make(tmp_path, "a", mean=0.003)
    try:
        report = s.gate()
    finally:
        s.close()
    assert report.decision == "RESEARCH_REJECTED" and report.reasons == ("criterion_failed:dsr",)
    assert Decimal("0.93") < Decimal(report.measures["dsr"]) < Decimal("0.95")


@pytest.mark.parametrize("dsr, pbo, sharpe, comp, failed", [
    ("0.95", "0.20", "0.0001", "0.0001", []),
    ("0.9499999", "0.20", "1", "1", ["dsr"]),
    ("0.95", "0.2000001", "1", "1", ["pbo"]),
    ("1", "0", "0", "1", ["holdout_net_sharpe"]),
    ("1", "0", None, "1", ["holdout_net_sharpe"]),
    ("1", "0", "1", "0", ["holdout_compounded_net_excess_return"]),
])
def test_threshold_boundaries(dsr, pbo, sharpe, comp, failed):
    policy = gate.parse_approval_policy({"approval_policy": POLICY})
    checks = gate.judge(policy, dsr=Decimal(dsr), pbo=Decimal(pbo),
                        holdout_sharpe=None if sharpe is None else Decimal(sharpe),
                        holdout_compounded=Decimal(comp))
    assert [c["criterion"] for c in checks if not c["passed"]] == failed


# ---------------------------------------------------------------- policy binding (condition 2)
def test_missing_policy_is_insufficient(tmp_path):
    s = make(tmp_path, "a", policy=None)
    try:
        report = s.gate()
    finally:
        s.close()
    assert report.decision == "INSUFFICIENT_EVIDENCE" and "approval_policy_missing" in report.reasons


@pytest.mark.parametrize("change", [
    {"max_pbo": "0.50"}, {"min_dsr": "0.10"}, {"pbo_slice_count": 6},
    {"holdout_compounded_return_above": "-1"}, {"min_completed_trials": 1},
])
def test_policy_edited_after_registration_breaks_the_binding(study, change):
    edited = dict(study.doc)
    edited["approval_policy"] = {**POLICY, **change}
    report = study.gate(preregistration=edited)
    assert report.decision == "INSUFFICIENT_EVIDENCE"
    assert any(r.startswith("walk_forward_not_valid") and "binding_or_preregistration_mismatch" in r
               for r in report.reasons)
    assert report.checks == ()


@pytest.mark.parametrize("change, reason", [
    ({"max_pbo": "0.50"}, "policy_below_project_floor"),
    ({"min_dsr": "0.90"}, "policy_below_project_floor"),
    ({"pbo_slice_count": 6}, "policy_below_project_floor"),
    ({"holdout_net_sharpe_above": "-0.1"}, "policy_below_project_floor"),
    ({"min_completed_trials": 1}, "policy_below_project_floor"),
    ({"min_dsr": 1}, "invalid_approval_policy"),
    ({"extra": "1"}, "invalid_approval_policy"),
])
def test_registered_policy_looser_than_the_project_floor_is_insufficient(tmp_path, change, reason):
    s = make(tmp_path, "a", policy={**POLICY, **change})
    try:
        report = s.gate()
    finally:
        s.close()
    assert report.decision == "INSUFFICIENT_EVIDENCE" and reason in report.reasons


def test_stricter_registered_policy_is_honoured(tmp_path):
    s = make(tmp_path, "a", policy={**POLICY, "min_dsr": "0.99", "max_pbo": "0.10"})
    try:
        report = s.gate()
    finally:
        s.close()
    assert [c["threshold"] for c in report.checks][:2] == ["0.99", "0.10"]


# ---------------------------------------------------------------- evidence completeness
def test_failed_attempts_make_the_pbo_family_incomplete(tmp_path):
    s = make(tmp_path, "a", failed=1)
    try:
        report = s.gate()
    finally:
        s.close()
    assert report.decision == "INSUFFICIENT_EVIDENCE" and "failed_attempts_present" in report.reasons


def test_open_attempt_before_the_gate_is_insufficient(study):
    study.registry.allocate(STUDY, {"still": "running"}, MANIFEST)
    report = study.gate()
    assert report.decision == "INSUFFICIENT_EVIDENCE" and "open_or_interrupted_attempts" in report.reasons


def test_missing_extra_or_forged_results_are_insufficient(study):
    results = tuple(study.results.values())
    assert "completed_results_missing_or_extra" in study.gate(results=results[:2]).reasons
    forged = replace(results[1], metrics_json=results[1].metrics_json.replace('"1d"', '"1h"'))
    report = study.gate(results=(results[0], forged, results[2]))
    assert any(r.startswith("walk_forward_not_valid") for r in report.reasons)
    assert "duplicate_result" in study.gate(results=results + results[:1]).reasons


def test_selected_trial_must_be_the_holdout_candidate(tmp_path):
    s = make(tmp_path, "a", holdout_for="half-long")
    try:
        report = s.gate()
    finally:
        s.close()
    assert report.decision == "INSUFFICIENT_EVIDENCE" and "selected_not_holdout_candidate" in report.reasons


def test_unknown_selected_trial_is_insufficient(study):
    report = study.gate(selected_trial_id="22222222-2222-4222-8222-222222222222")
    assert "selected_trial_not_in_results" in report.reasons


def test_forged_or_foreign_holdout_reports_are_insufficient(study, tmp_path):
    metrics = dict(study.holdout_report.metrics)
    metrics["sharpe"] = "9"
    report = study.gate(holdout_report=replace(study.holdout_report, metrics=metrics))
    assert any(r.startswith("holdout_not_valid") for r in report.reasons)
    other = make(tmp_path, "other", seed=22)
    try:
        report = study.gate(holdout_report=other.holdout_report)
    finally:
        other.close()
    assert report.decision == "INSUFFICIENT_EVIDENCE"
    assert any(r.startswith("holdout_not_valid") or r == "selected_not_holdout_candidate"
               for r in report.reasons)


def test_tampered_history_or_holdout_rows_are_insufficient(study):
    history = study.history[:50] + (replace(study.history[50], excess_return="0.05"),) + study.history[51:]
    assert any("history_changed" in r for r in study.gate(history=history).reasons)
    rows = study.holdout_rows[:3] + (replace(study.holdout_rows[3], excess_return="0.05"),) + study.holdout_rows[4:]
    assert any("holdout_data_mismatch" in r for r in study.gate(holdout_rows=rows).reasons)


# ---------------------------------------------------------------- PBO data requirements (condition 1)
def test_oos_count_not_splitting_into_eight_slices_is_insufficient_not_resliced(tmp_path):
    s = make(tmp_path, "a", bounds=(100, 130, 160, 190))     # 90 OOS rows: not 8 equal slices
    try:
        report = s.gate()
    finally:
        s.close()
    assert report.decision == "INSUFFICIENT_EVIDENCE"
    assert any(r.startswith("pbo_not_computable") for r in report.reasons)
    assert "pbo" not in report.measures


def test_unaligned_oos_series_are_insufficient(study):
    other = wf.WalkForward(study.panel, preregistration=study.doc, manifest=MANIFEST,
                           source_sha256="c" * 64, fit=fit_mean, decide=decide_sign)
    shifted = run_registered_trial(study.registry, STUDY,
                                   {"strategy": {"name": "shifted"}, "walk_forward": plan((104, 136, 168, 200))},
                                   MANIFEST, other.evaluate)
    report = study.gate(results=tuple(study.results.values()) + (shifted,))
    assert report.decision == "INSUFFICIENT_EVIDENCE" and "unaligned_oos" in report.reasons


def test_fewer_completed_trials_than_the_policy_is_insufficient(tmp_path):
    s = make(tmp_path, "a", names=("always-long",))
    try:
        report = s.gate()
    finally:
        s.close()
    assert report.decision == "INSUFFICIENT_EVIDENCE" and "too_few_completed_trials" in report.reasons


# ---------------------------------------------------------------- stability, determinism, no authority
def test_registry_change_during_the_gate_is_insufficient(study, monkeypatch):
    original = gate.estimate_pbo
    def pbo_then_write(*args, **kwargs):
        out = original(*args, **kwargs)
        study.registry.allocate(STUDY, {"racing": 1}, MANIFEST)
        return out
    monkeypatch.setattr(gate, "estimate_pbo", pbo_then_write)
    report = study.gate()
    assert report.decision == "INSUFFICIENT_EVIDENCE" and "registry_changed_during_read" in report.reasons


def test_ledger_change_during_the_gate_is_insufficient(study, monkeypatch):
    original = gate.estimate_pbo
    def pbo_then_ledger(*args, **kwargs):
        out = original(*args, **kwargs)
        with pytest.raises(ho.HoldoutError):
            ho.request_access(study.ledger, study.registry, study_id=STUDY, candidate=study.selected,
                              preregistration=study.doc, manifest=MANIFEST, history=study.history)
        return out
    monkeypatch.setattr(gate, "estimate_pbo", pbo_then_ledger)
    report = study.gate()
    assert "ledger_changed_during_read" in report.reasons


def test_same_inputs_give_the_same_report_hash(study):
    a, b = study.gate(), study.gate()
    assert a.report_sha256 == b.report_sha256 and a == b
    assert gate.report_sha256(gate.report_payload(a)) == a.report_sha256
    assert a.decision == "RESEARCH_APPROVED"
    payload = gate.report_payload(a)
    payload["decision"] = "RESEARCH_REJECTED"
    assert gate.report_sha256(payload) != a.report_sha256


def test_no_dsr_or_pbo_value_can_be_supplied_from_outside():
    params = set(inspect.signature(gate.evaluate_strategy_gate).parameters)
    assert not {"dsr", "pbo", "dsr_report", "pbo_report", "measures", "checks"} & params


def test_report_cannot_carry_any_authority():
    base = dict(decision="RESEARCH_APPROVED", reasons=(), study_id=STUDY, selected_trial_id="x",
                checks=(), policy=None, measures={}, evidence={}, report_sha256="0" * 64)
    for override in ({"permits_promotion": True}, {"permits_paper_or_trading": True},
                     {"operational_authorization": "PAPER"}, {"label": "APPROVED"}):
        with pytest.raises(gate.GateError):
            gate.StrategyGateReport(**base, **override)


def test_limitations_state_what_the_gate_cannot_prove():
    text = " ".join(gate.LIMITATIONS)
    for phrase in ("not authorization", "Paper, Testnet or Live", "anchor_missing", "UNANCHORED",
                   "blind", "outside the registry", "governance choices", "not a confidence level",
                   "serial dependence", "N_eff"):
        assert phrase in text, phrase
