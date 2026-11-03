"""P4-04: research-only anchored expanding walk-forward against an independent float reference."""
from __future__ import annotations

import ast
import dataclasses
import json
import math
import random
from dataclasses import replace

import pytest

from crypto_trader.validation import walk_forward as wf_module
from crypto_trader.validation.canonical import content_hash
from crypto_trader.validation.contracts import RegistryError, StudySpec
from crypto_trader.validation.dsr import deflated_sharpe
from crypto_trader.validation.registry import TrialRegistry
from crypto_trader.validation.research_path import run_registered_trial
from crypto_trader.validation.walk_forward import (
    FeatureRow, LabelRow, PanelRow, WalkForward, WalkForwardError, verify_walk_forward,
)

STUDY = "11111111-1111-4111-8111-111111111111"
PATHS = ("src/crypto_trader/paper/session.py",)
MANIFEST = {PATHS[0]: "b" * 64}
SOURCE = "c" * 64
DAY = 86_400 * 10**9
BASE = 1_735_689_600 * 10**9  # 2025-01-01T00:00:00Z


def day(i):
    return BASE + i * DAY


def make_panel(n=200, seed=5, mean=0.0005):
    """Daily rows; the feature of row i is the realized return of row i-1 (known at decision i)."""
    rng = random.Random(seed)
    returns = [f"{rng.gauss(mean, 0.01):.8f}" for _ in range(n)]
    rows = []
    for i in range(n):
        signal = returns[i - 1] if i else "0"
        rows.append(PanelRow(
            row_id=f"r{i:05d}", decision_utc_ns=day(i), feature_available_utc_ns=day(i),
            revision_first_seen_utc_ns=day(i), exposure_end_utc_ns=day(i + 1),
            excess_return=returns[i], features=(("signal", signal),),
        ))
    return tuple(rows)


PANEL = make_panel()


def make_plan(**overrides):
    plan = {
        "warmup_start_utc_ns": str(day(0)), "anchor_start_utc_ns": str(day(10)),
        "oos_boundaries_utc_ns": [str(day(d)) for d in (100, 130, 160, 190)],
        "purge_ns": str(DAY), "period": "1d", "fee_bps": "10", "slippage_bps": "5",
    }
    plan.update(overrides)
    return plan


def make_doc(start=300, end=400, **extra):
    return {"study": "p4-04 synthetic", "holdout": {
        "start_utc_ns": str(day(start)), "end_utc_ns": str(day(end))}, **extra}


DOC = make_doc()


def config(plan=None, name="sign-momentum"):
    return {"strategy": {"name": name}, "walk_forward": plan or make_plan()}


def fit_mean(features, labels, cfg):
    total = sum(float(x.excess_return) for x in labels)
    return {"mean": f"{total / len(labels):.10f}", "labels": len(labels)}


def decide_sign(state, now_utc_ns, features, cfg):
    current = max(features, key=lambda f: f.decision_utc_ns)
    signal = float(dict(current.features)["signal"])
    return "1" if signal > 0 else "0"


def registry(path, doc=DOC, n_max=20):
    reg = TrialRegistry(path / "wf.sqlite")
    reg.register_study(StudySpec(
        study_id=STUDY, preregistration_hash=content_hash(wf_module.PREREGISTRATION_DOMAIN, doc),
        n_max=n_max, allowed_paths=PATHS,
    ))
    return reg


def runner(panel=PANEL, doc=DOC, fit=fit_mean, decide=decide_sign):
    return WalkForward(panel, preregistration=doc, manifest=MANIFEST, source_sha256=SOURCE,
                       fit=fit, decide=decide)


def run(path, *, cfg=None, panel=PANEL, doc=DOC, study_doc=None, fit=fit_mean, decide=decide_sign):
    reg = registry(path, study_doc if study_doc is not None else doc)
    result = run_registered_trial(reg, STUDY, cfg or config(), MANIFEST,
                                  runner(panel, doc, fit, decide).evaluate)
    return reg, result


def verify(reg, result, panel=PANEL, doc=DOC):
    return verify_walk_forward(reg, result, study_id=STUDY, panel=panel, preregistration=doc,
                               manifest=MANIFEST)


def sub(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


# ---------------------------------------------------------------- independent float reference
def ref_fold(positions, returns, rate):
    prev, nets, costs = 0.0, [], []
    for i, (p, r) in enumerate(zip(positions, returns)):
        turnover = abs(p - prev) + (abs(p) if i == len(positions) - 1 else 0.0)
        costs.append(turnover * rate)
        nets.append(p * r - turnover * rate)
        prev = p
    return nets, costs


def ref_stats(xs):
    n = len(xs)
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1))
    growth = 1.0
    for x in xs:
        growth *= 1 + x
    return mean, (mean / sd if sd else None), growth - 1


# ---------------------------------------------------------------- happy path and reference
def test_valid_run_matches_float_reference_and_verifies(tmp_path):
    reg, result = run(tmp_path)
    with reg:
        report = verify(reg, result)
        assert report.status == "VALID_RESEARCH_RESULT", report.reasons
        metrics = result.metrics
        assert report.metrics == metrics
        returns = {r.decision_utc_ns: float(r.excess_return) for r in PANEL}
        everything = []
        for fold in metrics["folds"]:
            start, end = int(fold["oos_start_utc_ns"]), int(fold["oos_end_utc_ns"])
            steps = [d for d in sorted(returns) if start <= d < end]
            positions = [float(p) for p in fold["positions"]]
            nets, costs = ref_fold(positions, [returns[d] for d in steps], 0.0015)
            assert fold["oos_rows"] == len(steps) == 30
            assert max(abs(float(a) - b) for a, b in zip(fold["net_excess_returns"], nets)) < 1e-15
            assert max(abs(float(a) - b) for a, b in zip(fold["costs"], costs)) < 1e-15
            mean, sharpe, compounded = ref_stats(nets)
            assert abs(float(fold["mean"]) - mean) < 1e-15
            assert abs(float(fold["sharpe"]) - sharpe) < 1e-9
            assert abs(float(fold["compounded_net_excess_return"]) - compounded) < 1e-12
            everything += nets
        mean, sharpe, compounded = ref_stats(everything)
        agg = metrics["aggregate"]
        assert agg["oos_rows"] == 90 and abs(float(agg["sharpe"]) - sharpe) < 1e-9
        assert abs(float(agg["compounded_net_excess_return"]) - compounded) < 1e-12
        assert [float(x) for x in metrics["excess_returns"]] == pytest.approx(everything, abs=1e-15)


def test_anchored_expanding_layout_and_purge(tmp_path):
    seen = []
    def spy(features, labels, cfg):
        seen.append((min(x.decision_utc_ns for x in labels), max(x.exposure_end_utc_ns for x in labels),
                     max(f.feature_available_utc_ns for f in features), len(labels)))
        return {"k": len(seen)}
    reg, result = run(tmp_path, fit=spy)
    with reg:
        folds = result.metrics["folds"]
    for k, (first, last_end, last_feature, n) in enumerate(seen):
        cutoff = int(folds[k]["oos_start_utc_ns"]) - DAY
        assert first == day(10)                            # anchored at anchor_start
        assert last_end <= cutoff and last_feature <= cutoff
        assert n == (cutoff - DAY - day(10)) // DAY + 1    # labels mature by the purged cutoff
        assert int(folds[k]["train_cutoff_utc_ns"]) == cutoff
    assert [n for *_, n in seen] == sorted(n for *_, n in seen)  # expanding
    starts = [int(f["oos_start_utc_ns"]) for f in folds]
    ends = [int(f["oos_end_utc_ns"]) for f in folds]
    assert starts == [day(100), day(130), day(160)] and ends == [day(130), day(160), day(190)]


def test_purge_boundary_is_exact_to_the_nanosecond(tmp_path):
    cutoff = day(99)                       # fold 0: oos_start day 100 minus purge 1 day
    seen = []
    def spy(features, labels, cfg):
        seen.append({x.row_id for x in labels})
        return {}
    run(sub(tmp_path, "a"), fit=spy)
    assert "r00098" in seen[0] and "r00099" not in seen[0]     # r98 matures exactly at cutoff
    late = PANEL[:98] + (replace(PANEL[98], exposure_end_utc_ns=cutoff + 1),) + PANEL[99:]
    seen.clear()
    run(sub(tmp_path, "b"), panel=late, fit=spy)
    assert "r00097" in seen[0] and "r00098" not in seen[0]     # 1 ns past the cutoff is purged


def test_beyond_plan_rows_and_reruns_do_not_change_the_result(tmp_path):
    reg_a, a = run(sub(tmp_path, "a"))
    reg_b, b = run(sub(tmp_path, "b"), panel=make_panel(n=260))
    reg_c, c = run(sub(tmp_path, "c"))
    assert a.metrics_json == b.metrics_json == c.metrics_json
    assert a.trial_id != c.trial_id  # identity differs; results and input hashes do not
    with reg_b:
        assert verify(reg_b, b, panel=make_panel(n=260)).status == "VALID_RESEARCH_RESULT"


# ---------------------------------------------------------------- causality inside OOS
def test_decide_sees_only_features_available_at_its_decision(tmp_path):
    calls = []
    def snoop(state, now_utc_ns, features, cfg):
        assert all(f.feature_available_utc_ns <= now_utc_ns for f in features)
        assert all(type(f) is FeatureRow for f in features)
        for f in features:
            with pytest.raises(AttributeError):
                f.excess_return
            with pytest.raises(AttributeError):
                f.exposure_end_utc_ns
        calls.append((now_utc_ns, max(f.decision_utc_ns for f in features), len(features)))
        return "0"
    run(tmp_path, decide=snoop)
    oos = [r.decision_utc_ns for r in PANEL if day(100) <= r.decision_utc_ns < day(190)]
    assert [now for now, *_ in calls] == oos
    assert all(latest == now for now, latest, _ in calls)          # nothing after "now"
    assert [n for *_, n in calls] == [d // DAY - BASE // DAY + 1 for d in oos]


def test_row_first_seen_later_is_hidden_until_its_decision(tmp_path):
    """Sol P4-04 R1/R5: features declared available on day 0, but the row (decision and
    revision) is first seen on day 150. Nothing of it may reach fit or decide earlier."""
    rows = PANEL[:150] + (replace(PANEL[150], feature_available_utc_ns=day(0),
                                  features=(("signal", "FUTURE_REVISION"),)),) + PANEL[151:]
    cutoffs = iter((day(99), day(129), day(159)))   # oos_start - purge per fold
    leaks, seen = [], []
    def fit(features, labels, cfg):
        cutoff = next(cutoffs)
        leaks.extend(("fit", f.row_id) for f in features if f.decision_utc_ns > cutoff)
        return {}
    def decide(state, now_utc_ns, features, cfg):
        leaks.extend(("decide", f.row_id) for f in features if f.decision_utc_ns > now_utc_ns)
        seen.extend(now_utc_ns for f in features if dict(f.features)["signal"] == "FUTURE_REVISION")
        return "0"
    run(tmp_path, panel=rows, fit=fit, decide=decide)
    assert leaks == []
    assert min(seen) == day(150)                     # first visible exactly at its decision


def test_interfaces_carry_no_realized_return_for_decisions():
    assert {f.name for f in dataclasses.fields(FeatureRow)} == {
        "row_id", "decision_utc_ns", "feature_available_utc_ns", "features"}
    assert {f.name for f in dataclasses.fields(LabelRow)} == {
        "row_id", "decision_utc_ns", "exposure_end_utc_ns", "excess_return"}


def test_mutating_state_or_config_inside_callbacks_has_no_effect(tmp_path):
    def mutate_fit(features, labels, cfg):
        cfg["strategy"]["name"] = "hacked"
        return {"memory": []}
    def mutate_decide(state, now_utc_ns, features, cfg):
        assert cfg["strategy"]["name"] == "sign-momentum" and state["memory"] == []
        state["memory"].append(now_utc_ns)
        cfg["walk_forward"]["fee_bps"] = "0"
        return decide_sign(state, now_utc_ns, features, cfg)
    reg_a, a = run(sub(tmp_path, "a"), fit=mutate_fit, decide=mutate_decide)
    reg_b, b = run(sub(tmp_path, "b"), fit=lambda f, l, c: {"memory": []})
    assert a.metrics_json == b.metrics_json
    assert a.metrics["config"]["walk_forward"]["fee_bps"] == "10"


def test_caller_editing_config_after_freezing_changes_nothing(tmp_path):
    cfg = config()
    reg, result = run(tmp_path, cfg=cfg)
    cfg["walk_forward"]["fee_bps"] = "0"
    cfg["strategy"]["name"] = "other"
    with reg:
        report = verify(reg, result)
        assert report.status == "VALID_RESEARCH_RESULT", report.reasons
        assert report.metrics["config"]["strategy"]["name"] == "sign-momentum"


# ---------------------------------------------------------------- costs and returns
def scripted(script):
    """Position changes on the scripted days and is held otherwise."""
    last = ["0"]
    def decide(state, now_utc_ns, features, cfg):
        last[0] = script.get((now_utc_ns - BASE) // DAY, last[0])
        return last[0]
    return decide


def test_entry_hold_flip_exit_and_no_trade_costs(tmp_path):
    # fold 0 (days 100..129): enter long, hold, flip short, go flat, re-enter, end long
    script = {100: "1", 105: "-1", 110: "0", 120: "0.5", 130: "0", 160: "0"}
    reg, result = run(tmp_path, decide=scripted(script))
    fold0, fold1 = result.metrics["folds"][:2]
    rate = 0.0015
    costs = [float(c) for c in fold0["costs"]]
    assert costs[0] == pytest.approx(1 * rate)        # entry
    assert costs[1:5] == [0.0] * 4                    # holding is free
    assert costs[5] == pytest.approx(2 * rate)        # flip = two sides
    assert costs[10] == pytest.approx(1 * rate)       # exit to flat
    assert costs[20] == pytest.approx(0.5 * rate)     # re-entry
    assert costs[29] == pytest.approx(0.5 * rate)     # forced close at fold end
    assert float(fold0["turnover"]) == pytest.approx(1 + 2 + 1 + 0.5 + 0.5)
    assert float(fold0["total_cost"]) == pytest.approx(5 * rate)    # each side charged once
    assert float(fold1["total_cost"]) == 0 and float(fold1["turnover"]) == 0
    assert all(float(x) == 0 for x in fold1["net_excess_returns"])    # no trade, no cost
    assert fold1["sharpe"] is None                                    # zero variance is not a Sharpe


def test_compounded_return_is_a_product_not_a_sum(tmp_path):
    reg, result = run(tmp_path, decide=lambda s, n, f, c: "1")
    fold = result.metrics["folds"][0]
    nets = [float(x) for x in fold["net_excess_returns"]]
    product = math.prod(1 + x for x in nets) - 1
    assert float(fold["compounded_net_excess_return"]) == pytest.approx(product, abs=1e-12)
    assert abs(product - sum(nets)) > 1e-6


@pytest.mark.parametrize("fee, slip", [("-1", "0"), ("0", "-0.5"), ("10001", "0"), ("1e2", "0"), (5, "0")])
def test_negative_or_malformed_costs_are_refused(tmp_path, fee, slip):
    reg = registry(tmp_path)
    with reg:
        with pytest.raises(WalkForwardError, match="invalid_cost_model|invalid_decimal"):
            run_registered_trial(reg, STUDY, config(make_plan(fee_bps=fee, slippage_bps=slip)),
                                 MANIFEST, runner().evaluate)
        assert reg.status_counts(STUDY) == {"FAILED": 1}


@pytest.mark.parametrize("position", ["1.5", "-1.0001", 1, "1e0", "abc"])
def test_out_of_range_or_malformed_positions_are_refused(tmp_path, position):
    reg = registry(tmp_path)
    with reg, pytest.raises(WalkForwardError, match="invalid_position"):
        run_registered_trial(reg, STUDY, config(), MANIFEST,
                             runner(decide=lambda s, n, f, c: position).evaluate)


# ---------------------------------------------------------------- holdout
def test_holdout_overlap_is_refused_before_any_result(tmp_path):
    doc = make_doc(start=189, end=250)          # last OOS row is in the holdout
    reg = registry(tmp_path, doc)
    with reg:
        with pytest.raises(WalkForwardError, match="holdout_overlap"):
            run_registered_trial(reg, STUDY, config(), MANIFEST, runner(doc=doc).evaluate)
        assert reg.status_counts(STUDY) == {"FAILED": 1}


def test_holdout_inside_the_plan_range_without_rows_is_refused(tmp_path):
    doc = {"holdout": {"start_utc_ns": str(day(50) + 1), "end_utc_ns": str(day(50) + 2)}}
    rows = tuple(r for r in PANEL if r.decision_utc_ns != day(50))   # no row spans the holdout
    reg = registry(tmp_path, doc)
    with reg, pytest.raises(WalkForwardError, match="holdout_overlap"):
        run_registered_trial(reg, STUDY, config(), MANIFEST, runner(rows, doc).evaluate)


def test_row_whose_features_come_from_the_holdout_is_refused(tmp_path):
    doc = make_doc(start=-10, end=-3)               # before the plan range
    rows = (replace(PANEL[0], feature_available_utc_ns=day(-5)),) + PANEL[1:]
    reg = registry(tmp_path, doc)
    with reg, pytest.raises(WalkForwardError, match="holdout_overlap"):
        run_registered_trial(reg, STUDY, config(), MANIFEST, runner(rows, doc).evaluate)


def test_holdout_touching_the_half_open_end_is_allowed(tmp_path):
    doc = make_doc(start=190, end=250)
    reg, result = run(tmp_path, doc=doc, panel=PANEL[:190])
    with reg:
        assert verify(reg, result, panel=PANEL[:190], doc=doc).status == "VALID_RESEARCH_RESULT"


def test_preregistration_not_matching_the_study_is_refused(tmp_path):
    other = make_doc(start=500, end=600)
    reg = registry(tmp_path, DOC)
    with reg:
        with pytest.raises(WalkForwardError, match="holdout_unproven"):
            run_registered_trial(reg, STUDY, config(), MANIFEST, runner(doc=other).evaluate)
        assert reg.status_counts(STUDY) == {"FAILED": 1}


@pytest.mark.parametrize("doc", [
    {"study": "no holdout"},
    {"holdout": {"start_utc_ns": str(day(300))}},
    {"holdout": {"start_utc_ns": str(day(400)), "end_utc_ns": str(day(300))}},
    {"holdout": {"start_utc_ns": day(300), "end_utc_ns": str(day(400))}},
    "not a mapping",
])
def test_missing_or_malformed_holdout_declaration_is_refused_before_allocation(doc):
    with pytest.raises(WalkForwardError, match="holdout_unproven"):
        runner(doc=doc)


def test_verify_with_a_different_preregistration_is_refused(tmp_path):
    reg, result = run(tmp_path)
    with reg:
        report = verify(reg, result, doc=make_doc(start=500, end=600))
        assert report.status == "INSUFFICIENT_EVIDENCE"
        assert "binding_or_preregistration_mismatch" in report.reasons and report.metrics is None


# ---------------------------------------------------------------- corrupt data and plans
def _swap(rows, i, j):
    rows = list(rows)
    rows[i], rows[j] = rows[j], rows[i]
    return tuple(rows)


@pytest.mark.parametrize("mutate, reason", [
    (lambda p: p[:5] + (replace(p[5], decision_utc_ns=float(day(5))),) + p[6:], "corrupt_timestamp"),
    (lambda p: p[:5] + (replace(p[5], feature_available_utc_ns=True),) + p[6:], "corrupt_timestamp"),
    (lambda p: p[:5] + (replace(p[5], revision_first_seen_utc_ns=-1),) + p[6:], "corrupt_timestamp"),
    (lambda p: p[:5] + (replace(p[5], exposure_end_utc_ns=2**63),) + p[6:], "corrupt_timestamp"),
    (lambda p: _swap(p, 5, 6), "unsorted_or_duplicate_decision"),
    (lambda p: p[:6] + (replace(p[6], decision_utc_ns=day(5), feature_available_utc_ns=day(5),
                                revision_first_seen_utc_ns=day(5)),) + p[7:],
     "unsorted_or_duplicate_decision"),
    (lambda p: p[:6] + (replace(p[6], row_id="r00005"),) + p[7:], "duplicate_or_empty_row_id"),
    (lambda p: p[:5] + (replace(p[5], feature_available_utc_ns=day(5) + 1),) + p[6:], "point_in_time_violation"),
    (lambda p: p[:5] + (replace(p[5], revision_first_seen_utc_ns=day(5) + 1),) + p[6:], "point_in_time_violation"),
    (lambda p: p[:5] + (replace(p[5], exposure_end_utc_ns=day(5)),) + p[6:], "invalid_exposure"),
    (lambda p: p[:5] + (replace(p[5], excess_return="0.1e1"),) + p[6:], "invalid_decimal"),
    (lambda p: p[:5] + (replace(p[5], features=(("b", "1"), ("a", "2"))),) + p[6:], "invalid_features"),
    (lambda p: list(p), "invalid_panel"),
])
def test_corrupt_panels_are_refused_before_allocation(mutate, reason):
    with pytest.raises(WalkForwardError, match=reason):
        runner(mutate(PANEL))


@pytest.mark.parametrize("plan, reason", [
    (make_plan(oos_boundaries_utc_ns=[str(day(d)) for d in (100, 130, 160)]), "too_few_folds"),
    (make_plan(oos_boundaries_utc_ns=[str(day(d)) for d in (100, 129, 160, 190)]), "insufficient_oos_observations"),
    (make_plan(oos_boundaries_utc_ns=[str(day(d)) for d in (100, 160, 130, 190)]), "invalid_plan"),
    (make_plan(anchor_start_utc_ns=str(day(99))), "invalid_plan"),
    (make_plan(warmup_start_utc_ns=str(day(11))), "invalid_plan"),
    (make_plan(purge_ns=DAY), "corrupt_timestamp"),
    (make_plan(purge_ns="-1"), "corrupt_timestamp"),
    (make_plan(period=""), "invalid_plan"),
    ({**make_plan(), "extra": "1"}, "invalid_plan"),
])
def test_corrupt_plans_fail_the_attempt(tmp_path, plan, reason):
    reg = registry(tmp_path)
    with reg:
        with pytest.raises(WalkForwardError, match=reason):
            run_registered_trial(reg, STUDY, config(plan), MANIFEST, runner().evaluate)
        assert reg.status_counts(STUDY) == {"FAILED": 1}


def test_overlapping_oos_exposures_are_refused(tmp_path):
    rows = tuple(replace(r, exposure_end_utc_ns=day(112)) if r.decision_utc_ns == day(110) else r
                 for r in PANEL)
    reg = registry(tmp_path)
    with reg, pytest.raises(WalkForwardError, match="overlapping_or_overrunning_exposure"):
        run_registered_trial(reg, STUDY, config(), MANIFEST, runner(rows).evaluate)


def test_noncanonical_fit_state_is_refused(tmp_path):
    reg = registry(tmp_path)
    with reg, pytest.raises(WalkForwardError, match="noncanonical_fit_state"):
        run_registered_trial(reg, STUDY, config(), MANIFEST,
                             runner(fit=lambda f, l, c: {"mean": 0.5}).evaluate)


# ---------------------------------------------------------------- registry and proof
def test_one_run_is_one_registered_attempt(tmp_path):
    reg, result = run(tmp_path)
    with reg:
        assert reg.status_counts(STUDY) == {"COMPLETED": 1}


def test_a_failing_window_fails_the_single_attempt(tmp_path):
    def late_failure(state, now_utc_ns, features, cfg):
        if now_utc_ns >= day(160):
            raise ValueError("fold 2 failed")
        return "0"
    reg = registry(tmp_path)
    with reg:
        with pytest.raises(ValueError):
            run_registered_trial(reg, STUDY, config(), MANIFEST, runner(decide=late_failure).evaluate)
        assert reg.status_counts(STUDY) == {"FAILED": 1}


def test_binding_recomputation_matches_the_registry(tmp_path):
    reg, result = run(tmp_path)
    with reg:
        prereg = content_hash(wf_module.PREREGISTRATION_DOMAIN, DOC)
        assert wf_module._binding(STUDY, prereg, result.metrics["config"], MANIFEST, None) == result.binding_hash


def test_forged_or_tampered_results_are_refused(tmp_path):
    reg, result = run(tmp_path)
    with reg:
        metrics = json.loads(result.metrics_json)
        metrics["aggregate"]["sharpe"] = "9"
        forged = replace(result, metrics_json=json.dumps(metrics, sort_keys=True, separators=(",", ":")))
        report = verify(reg, forged)
        assert report.status == "INSUFFICIENT_EVIDENCE" and "result_without_registration_proof" in report.reasons
        assert "result_without_registration_proof" in verify(reg, object()).reasons
        tampered = PANEL[:150] + (replace(PANEL[150], excess_return="0.05"),) + PANEL[151:]
        report = verify(reg, result, panel=tampered)
        assert report.status == "INSUFFICIENT_EVIDENCE" and "metrics_not_reproducible" in report.reasons
        wrong_study = verify_walk_forward(reg, result, study_id="22222222-2222-4222-8222-222222222222",
                                          panel=PANEL, preregistration=DOC, manifest=MANIFEST)
        assert "result_without_registration_proof" in wrong_study.reasons


def test_registry_change_during_verification_is_refused(tmp_path, monkeypatch):
    reg, result = run(tmp_path)
    with reg:
        original = reg.trial_record
        def record_then_allocate(trial_id):
            record = original(trial_id)
            reg.allocate(STUDY, {"racing": 1}, MANIFEST)
            return record
        monkeypatch.setattr(reg, "trial_record", record_then_allocate)
        report = verify(reg, result)
        assert report.status == "INSUFFICIENT_EVIDENCE"
        assert "registry_changed_during_read" in report.reasons and report.metrics is None


def test_registry_change_during_rederivation_is_refused(tmp_path, monkeypatch):
    """Sol P4-04 R4: a write after the proof read but during re-derivation must refuse."""
    reg, result = run(tmp_path)
    with reg:
        original = wf_module._metrics
        def rebuild_then_allocate(*args, **kwargs):
            rebuilt = original(*args, **kwargs)
            reg.allocate(STUDY, {"racing": 2}, MANIFEST)
            return rebuilt
        monkeypatch.setattr(wf_module, "_metrics", rebuild_then_allocate)
        report = verify(reg, result)
        assert report.status == "INSUFFICIENT_EVIDENCE"
        assert "registry_changed_during_read" in report.reasons and report.metrics is None


def test_verification_trusts_recorded_decisions_and_state_hashes_documented_limit(tmp_path):
    """Sol P4-04 R7: a well-formed substitute fit-state hash recorded at evaluation is accepted;
    verification re-derives metrics GIVEN the recorded decisions and state hashes only."""
    honest = runner()
    def substitute(receipt, cfg):
        metrics = honest.evaluate(receipt, cfg)
        metrics["folds"][0]["fit_state_sha256"] = "d" * 64
        return metrics
    reg = registry(tmp_path)
    with reg:
        result = run_registered_trial(reg, STUDY, config(), MANIFEST, substitute)
        assert verify(reg, result).status == "VALID_RESEARCH_RESULT"
    assert any("does not re-run fit or decide" in item for item in wf_module.LIMITATIONS)


# ---------------------------------------------------------------- evidence and DSR compatibility
def test_report_carries_windows_fingerprints_config_costs_and_no_permissions(tmp_path):
    reg, result = run(tmp_path)
    with reg:
        report = verify(reg, result)
    m = report.metrics
    assert m["data"]["source_sha256_declared"] == SOURCE
    assert m["data"]["source_status"] == "UNANCHORED_DECLARED_NOT_ADMITTED"
    assert len(m["data"]["used_rows_sha256"]) == 64 and m["data"]["used_rows_sha256"] != SOURCE
    assert m["data"]["used_rows"] == 190
    assert m["preregistration_sha256"] == content_hash(wf_module.PREREGISTRATION_DOMAIN, DOC)
    assert m["holdout"] == DOC["holdout"] and m["config"] == config()
    assert m["cost_rate"] == "0.0015" and m["assumptions"] and m["limitations"]
    assert all(len(f["fit_state_sha256"]) == 64 for f in m["folds"])
    assert m["permits_strategy_approval"] is m["permits_promotion"] is m["permits_paper_or_trading"] is False
    assert report.permits_strategy_approval is report.permits_promotion is False
    assert report.permits_paper_or_trading is False
    assert any(h.startswith("registry_head:") for h in report.evidence_hashes)


def test_oos_metrics_are_accepted_by_dsr_without_counting_windows_as_trials(tmp_path):
    reg = registry(tmp_path)
    with reg:
        results = [run_registered_trial(reg, STUDY, config(name=name), MANIFEST,
                                        runner(decide=decide).evaluate)
                   for name, decide in (("sign-momentum", decide_sign),
                                        ("always-long", lambda s, n, f, c: "1"))]
        report = deflated_sharpe(reg, STUDY, results, selected_trial_id=results[0].trial_id)
        assert report.m_counted == 2 and report.k_valid == 2
        assert not {"invalid_metrics", "unaligned_observations", "mixed_periods",
                    "result_without_registration_proof"} & set(report.reasons)


def test_result_domain_matches_the_research_path():
    from crypto_trader.validation import research_path
    assert wf_module.RESULT_DOMAIN == research_path._RESULT_DOMAIN


def test_walk_forward_imports_only_the_sandboxed_local_modules():
    tree = ast.parse(open(wf_module.__file__, encoding="utf-8").read())
    local = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level}
    absolute = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and not node.level}
    absolute |= {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
    assert local == {"canonical", "contracts", "registry"}   # no research_path, dsr or walk_forward_plan
    assert not any(name.startswith("crypto_trader") for name in absolute)


def test_registry_errors_propagate_as_registry_errors():
    assert issubclass(WalkForwardError, RegistryError)
