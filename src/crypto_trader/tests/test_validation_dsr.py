"""P4-03: research-only Deflated Sharpe Ratio against an independent float reference."""
from __future__ import annotations

import math
import random
from dataclasses import replace
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


def _ok(xs, period="1d"):
    return lambda receipt, config: {"excess_returns": xs, "period": period}


def _fail(receipt, config):
    raise ValueError("evaluation error")


def study(tmp_path, specs, *, failed=0, prechecked=0, capacity=60):
    reg = TrialRegistry(tmp_path / "trials.sqlite")
    reg.register_study(StudySpec(
        study_id=STUDY, preregistration_hash="a" * 64, n_max=capacity,
        allowed_paths=("src/crypto_trader/paper/session.py",), cutoff="UNKNOWN",
    ))
    results = [run_registered_trial(reg, STUDY, {"variant": i}, MANIFEST, _ok(xs))
               for i, xs in enumerate(specs)]
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


def test_more_attempts_never_improve_the_result(tmp_path):
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
