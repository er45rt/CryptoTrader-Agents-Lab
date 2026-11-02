"""Research-only CSCV estimate of the Probability of Backtest Overfitting (PBO).

Nonparametric combinatorially symmetric cross-validation (CSCV) of Bailey,
Borwein, Lopez de Prado and Zhu, "The Probability of Backtest Overfitting",
Journal of Computational Finance 20(4), 2017.

Statistical research math only. Not a Paper gate, not a promotion or trading
signal, not a trial allocation, and no claim that the supplied returns come
from a trusted dataset or that a blind holdout was respected. The estimator
cannot detect omitted trials: callers must pass the FULL search family.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_HALF_EVEN, Clamped, Context, Decimal, DecimalException, DivisionByZero,
    Inexact, InvalidOperation, Overflow, Rounded, Subnormal, Underflow, localcontext,
)
from enum import Enum
from unicodedata import category, normalize, unidata_version

MEAN_RETURN_SCORE = "mean_return"

_MAX_NS = 2**63 - 1
_MIN_SLICES = 4
_MAX_SLICES = 16
_MIN_ROWS_PER_SLICE = 2
_MIN_CANDIDATES = 2
_MAX_CANDIDATES = 256
_MAX_CELLS = 2**20
# partitions * candidates * slices; C(16, 8) = 12870 allows at most 20 candidates.
_MAX_WORK = 2**22
# Every return satisfies |x| < 1E+19 with at most 18 decimal places, so any sum
# of <= 2**20 of them has < 64 significant digits: summation below is exact.
_MAX_ADJUSTED = 18
_MIN_EXPONENT = -18
_UNICODE_VERSION = "15.0.0"
_EXACT = Context(prec=64, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
                 traps=[InvalidOperation, DivisionByZero, Overflow, Underflow,
                        Subnormal, Inexact, Rounded, Clamped])
# The reported PBO ratio is rounded; the exact ratio is
# overfit_partition_count / partition_count.
_REPORT = Context(prec=28, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
                  traps=[InvalidOperation, DivisionByZero, Overflow])

_ESTIMATOR = "CSCV-PBO (Bailey, Borwein, Lopez de Prado, Zhu 2017), nonparametric"
_RANK_CONVENTION = (
    "OOS rank r of the in-sample winner among ALL N candidates, ascending: "
    "r = 1 + #candidates with strictly lower OOS mean return (1 = worst, N = best). "
    "Relative rank w = r/(N+1), logit = ln(w/(1-w)). A partition is overfit iff "
    "logit < 0 (the IS winner underperforms the OOS median), i.e. 2*rank < N+1; "
    "for odd N a winner exactly at the OOS median (logit 0) is NOT overfit. "
    "PBO = overfit partitions / C(S, S/2)."
)
_ASSUMPTIONS = (
    "research-only statistic; not a Paper gate, promotion, allocation or trading signal",
    "the panel is the complete search family of every candidate tried, not a pre-filtered subset",
    "rows are chronological, strictly increasing UTC ns; slices are equal, contiguous, half-open [start, end) row ranges",
    "score is the predeclared arithmetic mean return; equal half sizes make it order-equivalent to the exact sum",
    "all C(S, S/2) halves are enumerated, so every split is used in both orientations",
    "slices are treated as exchangeable; serial dependence and overlapping labels are NOT corrected",
    "ties affecting the in-sample winner or its OOS rank are refused, not broken",
    "no dataset trust, provenance or holdout independence is claimed or checked",
)


class PBOError(ValueError):
    """Fail the entire request. No partial or approximate estimate is returned."""


class PBOLabel(str, Enum):
    RESEARCH_ONLY_STATISTIC = "RESEARCH_ONLY_STATISTIC"


@dataclass(frozen=True)
class CandidateReturns:
    candidate_id: str
    returns: tuple[Decimal, ...]


@dataclass(frozen=True)
class PartitionOutcome:
    train_slices: tuple[int, ...]
    test_slices: tuple[int, ...]
    selected_candidate_id: str
    oos_rank: int
    candidate_count: int
    overfit: bool

    def __post_init__(self):
        for half in (self.train_slices, self.test_slices):
            if (type(half) is not tuple or len(half) < _MIN_SLICES // 2
                    or any(type(k) is not int or not 0 <= k < _MAX_SLICES for k in half)
                    or any(a >= b for a, b in zip(half, half[1:]))):
                raise PBOError("noncanonical partition half")
        if (len(self.train_slices) != len(self.test_slices)
                or set(self.train_slices) & set(self.test_slices)
                or type(self.selected_candidate_id) is not str or not self.selected_candidate_id
                or type(self.candidate_count) is not int
                or not _MIN_CANDIDATES <= self.candidate_count <= _MAX_CANDIDATES
                or type(self.oos_rank) is not int
                or not 1 <= self.oos_rank <= self.candidate_count
                or type(self.overfit) is not bool
                or self.overfit is not (2 * self.oos_rank < self.candidate_count + 1)):
            raise PBOError("inconsistent partition outcome")


@dataclass(frozen=True)
class PBOReport:
    score: str
    candidate_ids: tuple[str, ...]
    observation_count: int
    slice_count: int
    observations_per_slice: int
    slice_bounds: tuple[tuple[int, int], ...]
    partitions: tuple[PartitionOutcome, ...]
    partition_count: int
    overfit_partition_count: int
    pbo: Decimal
    estimator: str = _ESTIMATOR
    rank_convention: str = _RANK_CONVENTION
    assumptions: tuple[str, ...] = _ASSUMPTIONS
    label: PBOLabel = PBOLabel.RESEARCH_ONLY_STATISTIC
    permits_trial_allocation: bool = False
    permits_promotion: bool = False
    permits_trading: bool = False
    claims_dataset_trust: bool = False
    accessed_holdout: bool = False

    def __post_init__(self):
        if any(type(v) is not bool or v is not False for v in (
                self.permits_trial_allocation, self.permits_promotion,
                self.permits_trading, self.claims_dataset_trust, self.accessed_holdout)):
            raise PBOError("research statistic cannot grant permissions or trust")
        if (type(self.label) is not PBOLabel
                or self.label is not PBOLabel.RESEARCH_ONLY_STATISTIC
                or type(self.estimator) is not str or self.estimator != _ESTIMATOR
                or type(self.rank_convention) is not str
                or self.rank_convention != _RANK_CONVENTION
                or type(self.assumptions) is not tuple or self.assumptions != _ASSUMPTIONS
                or type(self.score) is not str or self.score != MEAN_RETURN_SCORE):
            raise PBOError("noncanonical PBO report labelling")
        s, rows = self.slice_count, self.observations_per_slice
        ids = self.candidate_ids
        if (type(s) is not int or type(rows) is not int
                or type(self.observation_count) is not int
                or s % 2 or not _MIN_SLICES <= s <= _MAX_SLICES
                or rows < _MIN_ROWS_PER_SLICE or self.observation_count != s * rows
                or self.slice_bounds != tuple((k * rows, (k + 1) * rows) for k in range(s))
                or type(ids) is not tuple
                or not _MIN_CANDIDATES <= len(ids) <= _MAX_CANDIDATES
                or any(type(v) is not str for v in ids)
                or ids != tuple(sorted(set(ids)))):
            raise PBOError("noncanonical PBO report layout")
        halves = _halves(s)
        if (type(self.partitions) is not tuple
                or type(self.partition_count) is not int
                or self.partition_count != len(halves)
                or len(self.partitions) != self.partition_count
                or any(type(p) is not PartitionOutcome for p in self.partitions)):
            raise PBOError("incomplete PBO partition set")
        overfit = 0
        for p, train in zip(self.partitions, halves):
            # Revalidate caller-supplied rows, including tampered frozen fields.
            PartitionOutcome(p.train_slices, p.test_slices, p.selected_candidate_id,
                             p.oos_rank, p.candidate_count, p.overfit)
            if (p.train_slices != train
                    or p.test_slices != tuple(k for k in range(s) if k not in train)
                    or p.candidate_count != len(ids)
                    or p.selected_candidate_id not in ids):
                raise PBOError("inconsistent PBO partition")
            overfit += p.overfit
        if (type(self.overfit_partition_count) is not int
                or self.overfit_partition_count != overfit
                or type(self.pbo) is not Decimal
                or self.pbo != _ratio(overfit, self.partition_count)):
            raise PBOError("PBO does not match partition outcomes")


def _binomial(n: int, k: int) -> int:
    out = 1
    for i in range(1, k + 1):
        out = out * (n - k + i) // i
    return out


def _halves(slice_count: int) -> tuple[tuple[int, ...], ...]:
    """All size-S/2 subsets of range(S) in lexicographic order (no itertools)."""
    k = slice_count // 2
    out: list[tuple[int, ...]] = []
    combo = list(range(k))
    while True:
        out.append(tuple(combo))
        i = k - 1
        while i >= 0 and combo[i] == slice_count - k + i:
            i -= 1
        if i < 0:
            return tuple(out)
        combo[i] += 1
        for j in range(i + 1, k):
            combo[j] = combo[j - 1] + 1


def _ratio(numerator: int, denominator: int) -> Decimal:
    with localcontext(_REPORT):
        return Decimal(numerator) / Decimal(denominator)


def _timestamp(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_NS:
        raise PBOError("require bounded exact-integer UTC nanoseconds")
    return value


def _candidate_id(value: object) -> str:
    if unidata_version != _UNICODE_VERSION:
        raise PBOError("require pinned Unicode database 15.0.0")
    if type(value) is not str or not 1 <= len(value) <= 128:
        raise PBOError("invalid candidate ID")
    for char in value:
        if category(char) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            raise PBOError("unsafe Unicode category in candidate ID")
    if normalize("NFC", value) != value:
        raise PBOError("candidate IDs must already be NFC")
    return value


def _return(value: object) -> Decimal:
    # Exact Decimal only: floats, ints, bools, strings and Decimal subclasses are
    # refused rather than converted, so no binary rounding enters silently.
    if type(value) is not Decimal or not value.is_finite():
        raise PBOError("returns must be finite decimal.Decimal values")
    exponent = value.as_tuple().exponent
    if (type(exponent) is not int or exponent < _MIN_EXPONENT
            or (value and value.adjusted() > _MAX_ADJUSTED)):
        raise PBOError("return outside exact-summation bounds")
    return value


def estimate_pbo(
    observation_utc_ns: tuple[int, ...],
    candidates: tuple[CandidateReturns, ...],
    *,
    slice_count: int,
    search_family_size: int,
    score: str,
) -> PBOReport:
    """Estimate PBO by CSCV over a complete chronological candidate panel.

    Rows 0..T-1 are split into S equal contiguous half-open slices
    [k*T/S, (k+1)*T/S). For each of the C(S, S/2) training halves J (test half
    is the complement) the candidate with the highest in-sample mean return is
    selected, then ranked against ALL candidates by out-of-sample mean return
    (see _RANK_CONVENTION). Any invalid input or unsupported tie rejects the
    entire request; no partial estimate is produced.
    """
    # Phase 1: structure only. Everything is bounded before any value is read.
    if type(score) is not str or score != MEAN_RETURN_SCORE:
        raise PBOError("only the predeclared mean_return score is supported")
    if type(observation_utc_ns) is not tuple or type(candidates) is not tuple:
        raise PBOError("require tuple inputs")
    n = len(candidates)
    if not _MIN_CANDIDATES <= n <= _MAX_CANDIDATES:
        raise PBOError("require the full search family of 2..256 candidates, not only a winner")
    if type(search_family_size) is not int or search_family_size != n:
        raise PBOError("declared search family size does not match supplied candidates")
    if (type(slice_count) is not int or slice_count % 2
            or not _MIN_SLICES <= slice_count <= _MAX_SLICES):
        raise PBOError("slice count must be an even integer in 4..16")
    t = len(observation_utc_ns)
    if t % slice_count or t // slice_count < _MIN_ROWS_PER_SLICE:
        raise PBOError("observations must split into equal slices of at least 2 rows")
    partition_count = _binomial(slice_count, slice_count // 2)
    if t * n > _MAX_CELLS or partition_count * n * slice_count > _MAX_WORK:
        raise PBOError("panel exceeds the approved time/memory budget")
    rows_per_slice = t // slice_count

    # Phase 2: read and validate each scalar once into owned copies.
    stamps = tuple(_timestamp(v) for v in observation_utc_ns)
    if any(a >= b for a, b in zip(stamps, stamps[1:])):
        raise PBOError("observation timestamps must be strictly increasing")
    by_id: dict[str, tuple[Decimal, ...]] = {}
    seen_series: set[tuple[Decimal, ...]] = set()
    for row in candidates:
        if type(row) is not CandidateReturns:
            raise PBOError("unexpected candidate type")
        candidate_id = _candidate_id(row.candidate_id)
        values = row.returns
        if type(values) is not tuple or len(values) != t:
            raise PBOError("ragged panel: every candidate needs one return per observation")
        values = tuple(_return(v) for v in values)
        if candidate_id in by_id:
            raise PBOError("duplicate candidate ID")
        if values in seen_series:
            raise PBOError("duplicate return series: degenerate search family")
        by_id[candidate_id] = values
        seen_series.add(values)

    # Phase 3: exact CSCV. Candidate order is canonical, so input order is irrelevant.
    ids = tuple(sorted(by_id))
    zero = Decimal(0)
    try:
        with localcontext(_EXACT):
            slice_sums = tuple(
                tuple(sum(by_id[c][k * rows_per_slice:(k + 1) * rows_per_slice], zero)
                      for k in range(slice_count))
                for c in ids)
            totals = tuple(sum(row, zero) for row in slice_sums)
            outcomes: list[PartitionOutcome] = []
            for train in _halves(slice_count):
                # Equal half sizes: comparing exact sums == comparing mean returns.
                in_sample = tuple(sum((row[k] for k in train), zero) for row in slice_sums)
                best = max(in_sample)
                if in_sample.count(best) != 1:
                    raise PBOError(f"in-sample tie for best candidate in training half {train}")
                winner = in_sample.index(best)
                oos = tuple(total - ins for total, ins in zip(totals, in_sample))
                target = oos[winner]
                if oos.count(target) != 1:
                    raise PBOError(f"out-of-sample tie with selected candidate in training half {train}")
                rank = 1 + sum(1 for v in oos if v < target)
                outcomes.append(PartitionOutcome(
                    train, tuple(k for k in range(slice_count) if k not in train),
                    ids[winner], rank, n, 2 * rank < n + 1,
                ))
    except DecimalException as exc:
        raise PBOError("inexact or invalid decimal arithmetic") from exc

    overfit = sum(1 for p in outcomes if p.overfit)
    return PBOReport(
        score=MEAN_RETURN_SCORE,
        candidate_ids=ids,
        observation_count=t,
        slice_count=slice_count,
        observations_per_slice=rows_per_slice,
        slice_bounds=tuple((k * rows_per_slice, (k + 1) * rows_per_slice)
                           for k in range(slice_count)),
        partitions=tuple(outcomes),
        partition_count=partition_count,
        overfit_partition_count=overfit,
        pbo=_ratio(overfit, partition_count),
    )
