"""P4-03: research-only Deflated Sharpe Ratio against an independent float reference."""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from statistics import NormalDist

import pytest

from crypto_trader.validation import dsr as dsr_module
from crypto_trader.validation.contracts import StudySpec
from crypto_trader.validation.dsr import (
    deflated_sharpe, expected_max_normal, normal_cdf, normal_ppf,
)
from crypto_trader.validation.registry import TrialRegistry
from crypto_trader.validation.research_path import TrialRejected, run_registered_trial

STUDY = "11111111-1111-4111-8111-111111111111"
MANIFEST = {"src/crypto_trader/paper/session.py": "b" * 64}
N01 = NormalDist()
EULER = 0.5772156649015329
T = 120


# ---------------------------------------------------------------- independent reference
def ref_stats(xs):
    n = len(xs)
    mu = sum(xs) / n
    d = [x - mu for x in xs]
    ss = sum(v * v for v in d)
    s = math.sqrt(ss / (n - 1))
    m2, m3, m4 = ss / n, sum(v ** 3 for v in d) / n, sum(v ** 4 for v in d) / n
    return mu / s, m3 / m2 ** 1.5, m4 / m2 ** 2


def ref_amax(n):
    if n == 1:
        return 0.0
    h = 0.0005
    zs = [-10 + i * h for i in range(int(round(20 / h)) + 1)]
    vals = [z * N01.pdf(z) * N01.cdf(z) ** (n - 1) for z in zs]
    return n * h * (sum(vals) - 0.5 * (vals[0] + vals[-1]))


def ref_dsr(series, selected, m):
    stats = {k: ref_stats(v) for k, v in series.items()}
    srs = [s[0] for s in stats.values()]
    sr, g3, g4 = stats[selected]
    if m == 1:
        sr0, v_used = 0.0, None
    else:
        mean = sum(srs) / len(srs)
        v_cross = sum((x - mean) ** 2 for x in srs) / (len(srs) - 1)
        v_est = max((1 - a * b + (c - 1) / 4 * b * b) / (T - 1) for b, a, c in stats.values())
        v_used = max(v_cross, v_est)
        sr0 = math.sqrt(v_used) * ref_amax(m)
    num = 1 - g3 * sr + (g4 - 1) / 4 * sr * sr
    return N01.cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(num)), sr0, v_used


# ---------------------------------------------------------------- fixtures
def gaussian(seed, mean, sd=0.01):
    rng = random.Random(seed)
    return [f"{rng.gauss(mean, sd):.8f}" for _ in range(T)]


def index_hash(start=date(2025, 1, 1), n=T):
    days = "\n".join((start + timedelta(days=i)).isoformat() for i in range(n))
    return hashlib.sha256(days.encode("ascii")).hexdigest()


INDEX = index_hash()


def _ok(xs, period="1d", index=INDEX):
    def evaluate(receipt, config):
        metrics = {"excess_returns": xs, "period": period}
        if index is not None:
            metrics["observation_index_sha256"] = index
        return metrics
    return evaluate


def _fail(receipt, config):
    raise ValueError("evaluation error")


def study(tmp_path, specs, *, failed=0, prechecked=0, capacity=60, indexes=None):
    reg = TrialRegistry(tmp_path / "trials.sqlite")
    reg.register_study(StudySpec(
        study_id=STUDY, preregistration_hash="a" * 64, n_max=capacity,
        allowed_paths=("src/crypto_trader/paper/session.py",), cutoff="UNKNOWN",
    ))
    indexes = indexes or [INDEX] * len(specs)
    results = [run_registered_trial(reg, STUDY, {"variant": i}, MANIFEST, _ok(xs, index=ix))
               for i, (xs, ix) in enumerate(zip(specs, indexes))]
    for j in range(failed):
        with pytest.raises(ValueError):
            run_registered_trial(reg, STUDY, {"failed": j}, MANIFEST, _fail)
    for j in range(prechecked):
        with pytest.raises(TrialRejected):
            run_registered_trial(reg, STUDY, {"pre": j}, MANIFEST, _fail, precheck=lambda c: "no")
    return reg, results


SPECS = [gaussian(11, 0.0030), gaussian(12, 0.0010), gaussian(13, -0.0005)]


def floats(specs):
    return [[float(x) for x in xs] for xs in specs]


# ---------------------------------------------------------------- numerics
def test_normal_cdf_and_ppf_match_reference():
    for x in (-8, -3.5, -1, -0.25, 0, 0.4, 1.96, 3, 7.5):
        assert abs(float(normal_cdf(Decimal(str(x)))) - N01.cdf(x)) < 1e-15
    for p in ("0.001", "0.5", "0.8", "0.975", "0.999999"):
        exact_p = float(p)  # both sides get the identical binary probability
        assert abs(float(normal_ppf(Decimal(exact_p))) - N01.inv_cdf(exact_p)) < 1e-12


def _erfc_cdf(x):
    return math.erfc(-x / math.sqrt(2)) / 2


@pytest.mark.parametrize("x", [-37, -30, -25, -20, -18, -12, -8, -5.5, -5, -4.5, -3, -1])
def test_normal_cdf_lower_tail_has_relative_accuracy_against_erfc(x):
    value = normal_cdf(Decimal(str(x)))
    assert value > 0
    # math.erfc itself carries ~1.2e-13 relative error below x = -25 on this platform
    assert abs(float(value) / _erfc_cdf(x) - 1) < 1e-12


def test_normal_cdf_sol_case_minus_18_is_positive_and_correct():
    value = normal_cdf(Decimal("-18"))
    assert value > 0
    assert abs(float(value) - 9.7409489189372e-73) < 1e-84


@pytest.mark.parametrize("p", ["1e-70", "1e-30", "1e-12", "3e-7"])
def test_normal_ppf_lower_tail_inverts_erfc(p):
    z = float(normal_ppf(Decimal(p)))
    assert abs(_erfc_cdf(z) / float(p) - 1) < 1e-10


def test_normal_ppf_sol_case_1e_minus_70():
    assert abs(float(normal_ppf(Decimal("1e-70"))) - (-17.7416)) < 1e-4


def test_sol_negative_sharpe_case_returns_dsr_inside_unit_interval(tmp_path):
    xs = ["-0.027"] * 60 + ["-0.007"] * 60
    random.Random(91801).shuffle(xs)
    reg, results = _fresh(tmp_path, "a", [xs])
    with reg:
        report = deflated_sharpe(reg, STUDY, results, selected_trial_id=results[0].trial_id)
        assert report.status == "VALID_RESEARCH_RESULT", report.reasons
        assert 0 <= report.dsr <= 1
        sr, g3, g4 = ref_stats([float(x) for x in xs])
        z = sr * math.sqrt(T - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr * sr)
        assert abs(float(report.dsr) / _erfc_cdf(z) - 1) < 1e-10


def test_dsr_outside_unit_interval_is_refused(tmp_path, monkeypatch):
    reg, results = _fresh(tmp_path, "a", SPECS[:1])
    monkeypatch.setattr(dsr_module, "normal_cdf", lambda z: Decimal("-1E-64"))
    with reg:
        report = deflated_sharpe(reg, STUDY, results, selected_trial_id=results[0].trial_id)
        assert report.status == "INSUFFICIENT_EVIDENCE"
        assert "dsr_outside_unit_interval" in report.reasons and report.dsr is None


def test_expected_max_exact_for_small_n_and_published_approximation_above():
    assert expected_max_normal(1) == (Decimal(0), "exact")
    known = {2: 1 / math.sqrt(math.pi), 3: 3 / (2 * math.sqrt(math.pi)), 5: 1.1629644736405196}
    for n, value in known.items():
        a, method = expected_max_normal(n)
        assert method == "exact_simpson" and abs(float(a) - value) < 1e-8
    for n in (10, 100, 1000):
        assert abs(float(expected_max_normal(n)[0]) - ref_amax(n)) < 1e-7
    a, method = expected_max_normal(5000)
    approx = (1 - EULER) * N01.inv_cdf(1 - 1 / 5000) + EULER * N01.inv_cdf(1 - 1 / (5000 * math.e))
    assert method == "bailey_approximation" and abs(float(a) - approx) < 1e-10


# ---------------------------------------------------------------- DSR against the reference
def _fresh(tmp_path, name, specs, **kw):
    path = tmp_path / name
    path.mkdir()
    return study(path, specs, **kw)


def test_dsr_matches_reference_counting_failed_but_not_precheck(tmp_path):
    reg, results = _fresh(tmp_path, "a", SPECS, failed=2, prechecked=1)
    with reg:
        selected = results[0].trial_id
        report = deflated_sharpe(reg, STUDY, results, selected_trial_id=selected)
        assert report.status == "VALID_RESEARCH_RESULT", report.reasons
        assert (report.m_total, report.m_counted, report.k_valid, report.n_dsr) == (6, 5, 3, 5)
        series = {r.trial_id: xs for r, xs in zip(results, floats(SPECS))}
        dsr, sr0, v_used = ref_dsr(series, selected, 5)
        assert abs(float(report.sr_threshold) - sr0) < 1e-7
        assert abs(float(report.variance_used) - v_used) < 1e-12
        assert abs(float(report.dsr) - dsr) < 1e-7
        assert report.observations == T and report.permits_paper_or_trading is False
        assert report.n_effective_estimated is None and report.mean_correlation is None


def test_single_attempt_uses_zero_threshold(tmp_path):
    reg, results = _fresh(tmp_path, "a", SPECS[:1])
    with reg:
        report = deflated_sharpe(reg, STUDY, results, selected_trial_id=results[0].trial_id)
        assert report.status == "VALID_RESEARCH_RESULT", report.reasons
        assert report.n_dsr == 1 and report.sr_threshold == 0 and report.variance_used is None
        sr, g3, g4 = ref_stats([float(x) for x in SPECS[0]])
        expected = N01.cdf(sr * math.sqrt(T - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr * sr))
        assert abs(float(report.dsr) - expected) < 1e-9


def test_more_failed_attempts_with_fixed_completed_never_improve_the_result(tmp_path):
    """Scoped claim: only FAILED attempts are added; the COMPLETED set stays fixed."""
    previous = None
    for failed in (0, 3, 10):
        reg, results = _fresh(tmp_path, f"f{failed}", SPECS, failed=failed)
        with reg:
            report = deflated_sharpe(reg, STUDY, results, selected_trial_id=results[0].trial_id)
            assert report.status == "VALID_RESEARCH_RESULT", report.reasons
            if previous is not None:
                assert report.sr_threshold >= previous.sr_threshold
                assert report.dsr <= previous.dsr
            previous = report


def test_adding_a_completed_attempt_can_raise_dsr_documented_limitation(tmp_path):
    """Sol's counterexample: monotonicity is NOT guaranteed when a COMPLETED attempt is added."""
    noise = [(0.01, -0.01, -0.01, 0.01)[i % 4] for i in range(T)]
    def spec(mean):
        return [f"{mean + e:.8f}" for e in noise]
    reports = []
    for name, means in (("three", (0.003, -0.007, -0.002)), ("four", (0.003, -0.007, -0.002, -0.002))):
        reg, results = _fresh(tmp_path, name, [spec(m) for m in means])
        with reg:
            report = deflated_sharpe(reg, STUDY, results, selected_trial_id=results[0].trial_id)
            assert report.status == "VALID_RESEARCH_RESULT", report.reasons
            reports.append(report)
    assert abs(float(reports[0].dsr) - 0.0905) < 5e-5 and abs(float(reports[1].dsr) - 0.0957) < 5e-5
    assert reports[1].dsr > reports[0].dsr
    assert any("COMPLETED" in item and "monoton" in item for item in dsr_module.LIMITATIONS)


def test_variance_floor_applies_when_trials_are_nearly_identical(tmp_path):
    base = SPECS[0]
    near = [base, [f"{Decimal(x) + Decimal('0.00000001'):.8f}" for x in base]]
    reg, results = _fresh(tmp_path, "a", near)
    with reg:
        report = deflated_sharpe(reg, STUDY, results, selected_trial_id=results[0].trial_id)
        assert report.status == "VALID_RESEARCH_RESULT", report.reasons
        assert report.variance_floor_applied is True
        assert report.variance_used == report.variance_estimation_max > report.variance_cross


# ---------------------------------------------------------------- refusals
def _status(reg, results, selected):
    return deflated_sharpe(reg, STUDY, results, selected_trial_id=selected)


def test_cherry_picked_or_forged_results_are_refused(tmp_path):
    reg, results = _fresh(tmp_path, "a", SPECS)
    with reg:
        sel = results[0].trial_id
        dropped = _status(reg, results[:2], sel)
        assert dropped.status == "INSUFFICIENT_EVIDENCE" and "completed_results_missing_or_extra" in dropped.reasons
        forged = replace(results[1], metrics_json=results[1].metrics_json.replace('"1d"', '"1h"'))
        bad = _status(reg, [results[0], forged, results[2]], sel)
        assert bad.status == "INSUFFICIENT_EVIDENCE" and "result_without_registration_proof" in bad.reasons
        dup = _status(reg, [results[0], results[0], results[1], results[2]], sel)
        assert dup.status == "INSUFFICIENT_EVIDENCE" and "duplicate_result" in dup.reasons
        assert dropped.dsr is None and bad.dsr is None


def test_open_and_aborted_attempts_block_the_result(tmp_path):
    reg, results = _fresh(tmp_path, "a", SPECS)
    with reg:
        reg.allocate(STUDY, {"open": 1}, MANIFEST)
        report = _status(reg, results, results[0].trial_id)
        assert report.status == "INSUFFICIENT_EVIDENCE" and "open_attempts" in report.reasons
    reg, results = _fresh(tmp_path, "b", SPECS)
    with reg:
        def interrupted(receipt, config):
            raise KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt):
            run_registered_trial(reg, STUDY, {"abort": 1}, MANIFEST, interrupted)
        report = _status(reg, results, results[0].trial_id)
        assert "aborted_or_interrupted_attempts_without_sharpe" in report.reasons


@pytest.mark.parametrize("specs, reason", [
    ([["0.00100000"] * T, gaussian(2, 0.001)], "zero_variance"),
    ([gaussian(1, 0.001)[:20], gaussian(2, 0.001)[:20]], "insufficient_observations"),
    ([gaussian(1, 0.001), gaussian(2, 0.001)[:-1]], "unaligned_observations"),
])
def test_invalid_series_are_refused(tmp_path, specs, reason):
    reg, results = _fresh(tmp_path, "a", specs)
    with reg:
        report = _status(reg, results, results[-1].trial_id)
        assert report.status == "INSUFFICIENT_EVIDENCE" and reason in report.reasons


def test_mixed_periods_and_unknown_selection_are_refused(tmp_path):
    reg = TrialRegistry(tmp_path / "t.sqlite")
    reg.register_study(StudySpec(study_id=STUDY, preregistration_hash="a" * 64, n_max=5,
                                 allowed_paths=("src/crypto_trader/paper/session.py",)))
    with reg:
        a = run_registered_trial(reg, STUDY, {"v": 1}, MANIFEST, _ok(SPECS[0], "1d"))
        b = run_registered_trial(reg, STUDY, {"v": 2}, MANIFEST, _ok(SPECS[1], "1h"))
        assert "mixed_periods" in _status(reg, [a, b], a.trial_id).reasons
        assert "selected_trial_not_in_results" in _status(
            reg, [a, b], "22222222-2222-4222-8222-222222222222").reasons


def test_started_allocation_between_count_and_head_reads_is_refused(tmp_path, monkeypatch):
    reg, results = _fresh(tmp_path, "a", SPECS[:1])
    with reg:
        original = reg.status_counts
        def counts_then_allocate(study_id):
            counts = original(study_id)
            reg.allocate(STUDY, {"racing": 1}, MANIFEST)
            return counts
        monkeypatch.setattr(reg, "status_counts", counts_then_allocate)
        report = _status(reg, results, results[0].trial_id)
        assert report.status == "INSUFFICIENT_EVIDENCE"
        assert "registry_changed_during_read" in report.reasons and report.dsr is None


def test_same_length_and_period_but_disjoint_dates_are_refused(tmp_path):
    later = index_hash(date(2025, 1, 1) + timedelta(days=365))
    reg, results = _fresh(tmp_path, "a", SPECS[:2], indexes=[INDEX, later])
    with reg:
        report = _status(reg, results, results[0].trial_id)
        assert report.status == "INSUFFICIENT_EVIDENCE" and "unaligned_observations" in report.reasons


@pytest.mark.parametrize("index", [None, "A" * 64, "a" * 63, 7])
def test_missing_or_malformed_observation_index_is_refused(tmp_path, index):
    reg = TrialRegistry(tmp_path / "t.sqlite")
    reg.register_study(StudySpec(study_id=STUDY, preregistration_hash="a" * 64, n_max=5,
                                 allowed_paths=("src/crypto_trader/paper/session.py",)))
    with reg:
        a = run_registered_trial(reg, STUDY, {"v": 1}, MANIFEST, _ok(SPECS[0], index=index))
        report = _status(reg, [a], a.trial_id)
        assert report.status == "INSUFFICIENT_EVIDENCE" and "invalid_metrics" in report.reasons


def test_serial_dependence_is_refused(tmp_path):
    rng = random.Random(7)
    level, trend = 0.0, []
    for _ in range(T):
        level = 0.9 * level + rng.gauss(0, 0.01)
        trend.append(f"{level + 0.002:.8f}")
    reg, results = _fresh(tmp_path, "a", [trend, SPECS[1]])
    with reg:
        report = _status(reg, results, results[0].trial_id)
        assert report.status == "INSUFFICIENT_EVIDENCE" and "serial_dependence" in report.reasons


def test_completed_and_failed_with_single_sharpe_cannot_estimate_variance(tmp_path):
    reg, results = _fresh(tmp_path, "a", SPECS[:1], failed=1)
    with reg:
        report = _status(reg, results, results[0].trial_id)
        assert "cannot_estimate_cross_trial_variance" in report.reasons


def test_result_proof_domain_matches_the_research_path():
    from crypto_trader.validation import research_path
    assert dsr_module.RESULT_DOMAIN == research_path._RESULT_DOMAIN


def test_dsr_imports_no_execution_surface():
    source = open(dsr_module.__file__, encoding="utf-8").read()
    for forbidden in ("crypto_trader.execution", "crypto_trader.paper", "crypto_trader.production",
                      "crypto_trader.breakglass", "crypto_trader.mainnet", "binance"):
        assert forbidden not in source
