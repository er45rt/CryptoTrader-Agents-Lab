"""P4-03 Deflated Sharpe Ratio. Research-only: grants no promotion, Paper or trading permission.

Published formulas (Bailey & Lopez de Prado 2014, "The Deflated Sharpe Ratio"; PSR from
Bailey & Lopez de Prado 2012):
* per-period Sharpe  SR_i = mean(x_i) / s_i, with s_i the (T-1) sample standard deviation
  of excess returns x_i (net of costs, minus the risk-free rate);
* skewness g3 = m3 / m2^1.5 and non-excess kurtosis g4 = m4 / m2^2 from central moments;
* Sharpe estimation variance  v_i = (1 - g3*SR + (g4-1)/4 * SR^2) / (T-1);
* cross-trial variance  V_cross = sample variance (K-1) of the K valid trial Sharpe ratios;
* expected maximum  A(N) = (1-gamma)*Phi^-1(1-1/N) + gamma*Phi^-1(1-1/(N e)); for N up to
  EXACT_A_MAX_N the exact order-statistic mean N*Int z phi(z) Phi(z)^(N-1) dz is used
  instead (composite Simpson, h=0.01 on [-10, 10]); A(1) = 0;
* threshold  SR0 = sqrt(V_used) * A(N_DSR);
* DSR = Phi((SR_sel - SR0) * sqrt(T-1) / sqrt(1 - g3*SR_sel + (g4-1)/4 * SR_sel^2)).

Project policies (NOT published formulas):
* N_DSR = M, the counted attempts (all registered attempts except REJECTED_PRECHECK, which
  ends before any evaluation). N_eff and block bootstrap are deferred.
* Variance floor  V_used = max(V_cross, max_i v_i).
* FAILED attempts are counted in M but carry no Sharpe: the approved research path records
  them before any metrics exist. STARTED, ABORTED or INTERRUPTED_UNRESOLVED attempts may hide
  performance-related outcomes, so they refuse the result.
* Every COMPLETED attempt must be supplied with registration proof; dropping one refuses.
* At least MIN_OBSERVATIONS aligned observations with one period label for every trial.
* Serial dependence is screened on the selected trial by lag-1 autocorrelation only:
  |rho1| > 1.96 / sqrt(T) refuses (Lo 2002); higher-order dependence is not screened.

Numerics use only ``decimal`` (validation sandbox): Phi by the erf Taylor series with
working precision raised by the cancellation size, exact 0/1 beyond |x| >= 20 (error < 1e-88);
Phi^-1 by bisection. The registry is UNANCHORED local evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Context, Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext,
)

from .canonical import CanonicalError, content_hash, parse_json_strict
from .contracts import RegistryError
from .registry import TrialRegistry

RESULT_DOMAIN = "research-result-v1"  # must equal research_path._RESULT_DOMAIN (tested)
MIN_OBSERVATIONS = 30
EXACT_A_MAX_N = 1000
LAG1_CRITICAL_Z = Decimal("1.96")
VALID = "VALID_RESEARCH_RESULT"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

_PREC = 50
_PI = Decimal("3.14159265358979323846264338327950288419716939937510582097494459")
_EULER_GAMMA = Decimal("0.577215664901532860606512090082402431042159335939923598805767")
_ZERO, _ONE, _TWO = Decimal(0), Decimal(1), Decimal(2)
_CLAMP = Decimal(20)
_GRID_H = Decimal("0.01")
_GRID_STEPS = 2000  # [-10, 10]
_grid_cache: list = []
_amax_cache: dict = {}

LIMITATIONS = (
    "N_DSR equals counted attempts M; N_eff estimation and block bootstrap are deferred",
    "variance floor max(V_cross, max_i v_i) is a project policy, not a published formula",
    "FAILED attempts are counted in M without a Sharpe ratio (policy)",
    "serial dependence screened by lag-1 autocorrelation of the selected trial only",
    "expected maximum assumes independent standard-normal trial statistics",
    "registry evidence is UNANCHORED local evidence; research outside the approved path is not covered",
    "a research result grants no promotion, Paper, Testnet or Live permission",
)


def _ctx(prec: int = _PREC) -> Context:
    return Context(prec=prec, rounding=ROUND_HALF_EVEN,
                   traps=[InvalidOperation, DivisionByZero, Overflow])


def _finite(value) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise RegistryError("finite Decimal required")
    return value


def normal_cdf(x: Decimal) -> Decimal:
    """Standard normal CDF Phi(x) to about 50 significant digits."""
    x = _finite(x)
    if x >= _CLAMP:
        return _ONE
    if x <= -_CLAMP:
        return _ZERO
    prec = 60 + int(x * x / 2 * Decimal("0.4343")) + 10
    with localcontext(_ctx(prec)):
        z = x / _TWO.sqrt()
        z2 = z * z
        term = total = z
        tiny = Decimal(10) ** (-(prec - 5))
        n = 0
        while True:
            n += 1
            term = -term * z2 / n
            add = term / (2 * n + 1)
            total += add
            if n > z2 and abs(add) < tiny:
                break
        value = (1 + 2 * total / _PI.sqrt()) / 2
    with localcontext(_ctx()):
        return +value


def normal_ppf(p: Decimal) -> Decimal:
    """Inverse standard normal CDF by bisection on [-20, 20]."""
    p = _finite(p)
    if not _ZERO < p < _ONE:
        raise RegistryError("probability must be strictly between 0 and 1")
    lo, hi = -_CLAMP, _CLAMP
    with localcontext(_ctx()):
        for _ in range(140):
            mid = (lo + hi) / 2
            if normal_cdf(mid) < p:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2


def _grid():
    if not _grid_cache:
        with localcontext(_ctx()):
            norm = (2 * _PI).sqrt()
            for i in range(_GRID_STEPS + 1):
                z = Decimal(-10) + _GRID_H * i
                _grid_cache.append((z, (-(z * z) / 2).exp() / norm, normal_cdf(z).ln()))
    return _grid_cache


def expected_max_normal(n: int) -> tuple[Decimal, str]:
    """E[max of n iid N(0,1)]: exact (Simpson) for n <= EXACT_A_MAX_N, Bailey approximation above."""
    if type(n) is not int or n < 1:
        raise RegistryError("n must be a positive integer")
    if n == 1:
        return _ZERO, "exact"
    if n in _amax_cache:
        return _amax_cache[n]
    with localcontext(_ctx()):
        if n <= EXACT_A_MAX_N:
            total = _ZERO
            last = _GRID_STEPS
            for i, (z, pdf, ln_cdf) in enumerate(_grid()):
                weight = 1 if i in (0, last) else (4 if i % 2 else 2)
                total += weight * z * pdf * ((n - 1) * ln_cdf).exp()
            result = (n * total * _GRID_H / 3, "exact_simpson")
        else:
            e = _ONE.exp()
            value = ((1 - _EULER_GAMMA) * normal_ppf(1 - _ONE / n)
                     + _EULER_GAMMA * normal_ppf(1 - _ONE / (n * e)))
            result = (+value, "bailey_approximation")
    _amax_cache[n] = result
    return result


@dataclass(frozen=True)
class DSRReport:
    status: str
    reasons: tuple[str, ...]
    study_id: str
    selected_trial_id: str
    m_total: int
    m_counted: int
    k_valid: int
    n_dsr: int | None = None
    observations: int | None = None
    sharpe_selected: Decimal | None = None
    skewness_selected: Decimal | None = None
    kurtosis_selected: Decimal | None = None
    lag1_autocorrelation_selected: Decimal | None = None
    variance_cross: Decimal | None = None
    variance_estimation_max: Decimal | None = None
    variance_used: Decimal | None = None
    variance_floor_applied: bool | None = None
    a_n: Decimal | None = None
    a_method: str | None = None
    sr_threshold: Decimal | None = None
    dsr: Decimal | None = None
    mean_correlation: None = None          # deferred (N_eff)
    n_effective_estimated: None = None     # deferred (N_eff)
    evidence_hashes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = LIMITATIONS
    permits_promotion: bool = False
    permits_paper_or_trading: bool = False


def _verified_series(registry: TrialRegistry, result, study_id: str):
    """Return (trial_id, metrics, output_hash) only with registration proof, else None."""
    try:
        trial_id, rstudy = result.trial_id, result.study_id
        binding, metrics_json, output_hash = result.binding_hash, result.metrics_json, result.output_hash
        metrics = parse_json_strict(metrics_json)
        expected = content_hash(RESULT_DOMAIN, {
            "trial_id": trial_id, "study_id": rstudy, "binding_hash": binding, "metrics": metrics,
        })
        record = registry.trial_record(trial_id)
    except (AttributeError, CanonicalError, RegistryError, TypeError, ValueError):
        return None
    if (rstudy != study_id or record["study_id"] != study_id or record["status"] != "COMPLETED"
            or record["binding_hash"] != binding or output_hash != expected
            or record["output_hash"] != expected):
        return None
    return trial_id, metrics, output_hash


def _parse_returns(metrics):
    if type(metrics) is not dict:
        return None
    period, raw = metrics.get("period"), metrics.get("excess_returns")
    if type(period) is not str or not period or type(raw) is not list:
        return None
    values = []
    for item in raw:
        if type(item) is not str:
            return None
        try:
            value = Decimal(item)
        except InvalidOperation:
            return None
        if not value.is_finite():
            return None
        values.append(value)
    return period, values


def _moments(xs):
    """(SR, g3, g4, lag1, v_numerator) or None for zero variance."""
    t = len(xs)
    mean = sum(xs) / t
    dev = [x - mean for x in xs]
    ss = sum(d * d for d in dev)
    if ss == 0:
        return None
    s = (ss / (t - 1)).sqrt()
    m2 = ss / t
    g3 = (sum(d ** 3 for d in dev) / t) / (m2 * m2.sqrt())
    g4 = (sum(d ** 4 for d in dev) / t) / (m2 * m2)
    sr = mean / s
    lag1 = sum(dev[i] * dev[i - 1] for i in range(1, t)) / ss
    numerator = 1 - g3 * sr + (g4 - 1) / 4 * sr * sr
    return sr, g3, g4, lag1, numerator


def deflated_sharpe(registry: TrialRegistry, study_id: str, results, *,
                    selected_trial_id: str) -> DSRReport:
    """DSR for the selected trial of a registered study. Evidence problems return INSUFFICIENT_EVIDENCE."""
    if not isinstance(registry, TrialRegistry):
        raise RegistryError("a TrialRegistry is required")
    counts = registry.status_counts(study_id)
    head = registry.verify().head_hash
    m_total = sum(counts.values())
    m = m_total - counts.get("REJECTED_PRECHECK", 0)
    reasons: list[str] = []
    if counts.get("STARTED"):
        reasons.append("open_attempts")
    if counts.get("ABORTED") or counts.get("INTERRUPTED_UNRESOLVED"):
        reasons.append("aborted_or_interrupted_attempts_without_sharpe")
    if m < 1:
        reasons.append("no_counted_attempts")

    series: dict[str, tuple[str, list]] = {}
    hashes: list[str] = []
    for result in list(results):
        proven = _verified_series(registry, result, study_id)
        if proven is None:
            reasons.append("result_without_registration_proof")
            continue
        trial_id, metrics, output_hash = proven
        if trial_id in series:
            reasons.append("duplicate_result")
            continue
        parsed = _parse_returns(metrics)
        if parsed is None:
            reasons.append("invalid_metrics")
            continue
        series[trial_id] = parsed
        hashes.append(f"trial:{trial_id}:{output_hash}")
    k = len(series)
    if k != counts.get("COMPLETED", 0):
        reasons.append("completed_results_missing_or_extra")
    if selected_trial_id not in series:
        reasons.append("selected_trial_not_in_results")

    lengths = {len(v) for _, v in series.values()}
    periods = {p for p, _ in series.values()}
    if len(periods) > 1:
        reasons.append("mixed_periods")
    if len(lengths) > 1:
        reasons.append("unaligned_observations")
    elif lengths and min(lengths) < MIN_OBSERVATIONS:
        reasons.append("insufficient_observations")
    if m >= 2 and k < 2:
        reasons.append("cannot_estimate_cross_trial_variance")

    base = dict(study_id=study_id, selected_trial_id=selected_trial_id, m_total=m_total,
                m_counted=m, k_valid=k,
                evidence_hashes=(f"registry_head:{head}",) + tuple(sorted(hashes)))
    if reasons:
        return DSRReport(status=INSUFFICIENT, reasons=tuple(dict.fromkeys(reasons)), **base)

    with localcontext(_ctx()):
        stats = {}
        for trial_id, (_, xs) in series.items():
            moments = _moments(xs)
            if moments is None:
                reasons.append("zero_variance")
            elif moments[4] <= 0:
                reasons.append("invalid_sharpe_uncertainty")
            else:
                stats[trial_id] = moments
        t = lengths.pop()
        if not reasons:
            sr, g3, g4, lag1, numerator = stats[selected_trial_id]
            if abs(lag1) > LAG1_CRITICAL_Z / Decimal(t).sqrt():
                reasons.append("serial_dependence")
        if reasons:
            return DSRReport(status=INSUFFICIENT, reasons=tuple(dict.fromkeys(reasons)), **base)

        a_n, a_method = expected_max_normal(m)
        v_cross = v_est = v_used = None
        floor = None
        if m == 1:
            sr0 = _ZERO
        else:
            srs = [s[0] for s in stats.values()]
            mean_sr = sum(srs) / k
            v_cross = sum((x - mean_sr) ** 2 for x in srs) / (k - 1)
            v_est = max(s[4] for s in stats.values()) / (t - 1)
            v_used = max(v_cross, v_est)
            floor = v_used > v_cross
            sr0 = v_used.sqrt() * a_n
        z = (sr - sr0) * Decimal(t - 1).sqrt() / numerator.sqrt()
        value = normal_cdf(z)
    return DSRReport(
        status=VALID, reasons=(), n_dsr=m, observations=t,
        sharpe_selected=sr, skewness_selected=g3, kurtosis_selected=g4,
        lag1_autocorrelation_selected=lag1, variance_cross=v_cross,
        variance_estimation_max=v_est, variance_used=v_used, variance_floor_applied=floor,
        a_n=a_n, a_method=a_method, sr_threshold=sr0, dsr=value, **base,
    )
