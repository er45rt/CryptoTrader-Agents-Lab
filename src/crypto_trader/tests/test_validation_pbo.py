"""Priority 4: research-only CSCV/PBO estimator; zero promotion or execution authority."""
from __future__ import annotations

import ast
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import socket
import subprocess
import time

import pytest

from crypto_trader.validation.pbo import (
    MEAN_RETURN_SCORE, CandidateReturns, PartitionOutcome, PBOError, PBOLabel,
    PBOReport, estimate_pbo,
)

BASE = 1_800_000_000_000_000_000
MODULE_PATH = Path(__file__).resolve().parents[1] / "validation" / "pbo.py"
D = Decimal


def stamps(count):
    return tuple(BASE + 1_000 * i for i in range(count))


def series(name, *values):
    return CandidateReturns(name, tuple(D(v) for v in values))


def from_slice_sums(sums_by_id, rows_per_slice=2):
    """Each slice sum s becomes rows (s - 0.5, 0.5[, 0 ...]) so slice sums stay exact."""
    out = []
    for name, sums in sums_by_id.items():
        rows = []
        for s in sums:
            rows.extend((D(s) - D("0.5"), D("0.5")) + (D(0),) * (rows_per_slice - 2))
        out.append(CandidateReturns(name, tuple(rows)))
    return tuple(out)


def run(candidates, *, slice_count=4, family=None, score=MEAN_RETURN_SCORE, ts=None):
    rows = len(candidates[0].returns)
    return estimate_pbo(
        stamps(rows) if ts is None else ts, candidates,
        slice_count=slice_count,
        search_family_size=len(candidates) if family is None else family,
        score=score,
    )


# Manually derived fixture: 3 candidates x 8 rows, S = 4 slices of 2 rows.
# Slice sums (s0, s1, s2, s3):   A = 3, 3, 3, 3   B = 5.5, 0, 1, 0   C = 0, 4, 0, 1
#
# train | IS sums A / B / C   | winner | OOS sums A / B / C | asc rank | 2r <= N+1 = 4?
# {0,1} | 6   / 5.5 / 4       | A      | 6 / 1   / 1        | 3        | no
# {0,2} | 6   / 6.5 / 0       | B      | 6 / 0   / 5        | 1        | OVERFIT
# {0,3} | 6   / 5.5 / 1       | A      | 6 / 1   / 4        | 3        | no
# {1,2} | 6   / 1   / 4       | A      | 6 / 5.5 / 1        | 3        | no
# {1,3} | 6   / 0   / 5       | A      | 6 / 6.5 / 0        | 2        | OVERFIT (median, N odd)
# {2,3} | 6   / 1   / 1       | A      | 6 / 5.5 / 4        | 3        | no
# PBO = 2 / 6.
def known_family():
    return (
        series("A", "1.25", "1.75", "2", "1", "0.5", "2.5", "3", "0"),
        series("B", "5", "0.5", "-1", "1", "1", "0", "0.25", "-0.25"),
        series("C", "0", "0", "4", "0", "-0.5", "0.5", "0.75", "0.25"),
    )


def test_known_answer_fixture_per_partition_winners_ranks_and_pbo():
    report = run(known_family())
    assert type(report) is PBOReport
    assert report.candidate_ids == ("A", "B", "C")
    assert report.observation_count == 8
    assert report.slice_count == 4
    assert report.observations_per_slice == 2
    assert report.slice_bounds == ((0, 2), (2, 4), (4, 6), (6, 8))
    assert [p.train_slices for p in report.partitions] == [
        (0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]
    assert [p.test_slices for p in report.partitions] == [
        (2, 3), (1, 3), (1, 2), (0, 3), (0, 2), (0, 1)]
    assert [p.selected_candidate_id for p in report.partitions] == ["A", "B", "A", "A", "A", "A"]
    assert [p.oos_rank for p in report.partitions] == [3, 1, 3, 3, 2, 3]
    assert [p.overfit for p in report.partitions] == [False, True, False, False, True, False]
    assert all(p.candidate_count == 3 for p in report.partitions)
    assert report.partition_count == 6
    assert report.overfit_partition_count == 2
    assert report.pbo == D(2) / D(6)
    assert report.pbo == D("0.3333333333333333333333333333")


def test_known_answer_fixture_full_reversal_is_pbo_one():
    # A = 5,1,-2,0  B = 0,3,1,2  C = 2,-1,4,-3  (hand-derived; IS winner always
    # lands at or below the OOS median).
    family = from_slice_sums({"A": (5, 1, -2, 0), "B": (0, 3, 1, 2), "C": (2, -1, 4, -3)})
    report = run(family)
    assert [p.selected_candidate_id for p in report.partitions] == ["A", "C", "A", "B", "B", "B"]
    assert [p.oos_rank for p in report.partitions] == [1, 1, 1, 2, 1, 2]
    assert report.overfit_partition_count == 6
    assert report.pbo == D(1)


def test_persistent_dominant_candidate_is_pbo_zero():
    family = from_slice_sums({"A": (3, 3, 3, 3), "B": (1, 2, 0, 1), "C": (0, 1, 2, -1)})
    report = run(family)
    assert {p.selected_candidate_id for p in report.partitions} == {"A"}
    assert {p.oos_rank for p in report.partitions} == {3}
    assert report.pbo == D(0)


def test_report_is_research_only_and_grants_nothing():
    report = run(known_family())
    assert report.label is PBOLabel.RESEARCH_ONLY_STATISTIC
    assert report.score == MEAN_RETURN_SCORE
    assert report.permits_trial_allocation is False
    assert report.permits_promotion is False
    assert report.permits_trading is False
    assert report.claims_dataset_trust is False
    assert report.accessed_holdout is False
    assert "ascending" in report.rank_convention and "2*rank <= N+1" in report.rank_convention
    assert any("not a paper gate" in a.lower() for a in report.assumptions)
    for field in ("permits_trial_allocation", "permits_promotion", "permits_trading",
                  "claims_dataset_trust", "accessed_holdout"):
        with pytest.raises(PBOError):
            replace(report, **{field: True})
    forbidden = {"allocate", "promote", "trade", "place_order", "deploy", "approve",
                 "allocation", "weights", "holdout"}
    assert not forbidden.intersection(dir(report))


def test_report_rejects_tampered_counts_and_pbo():
    report = run(known_family())
    with pytest.raises(PBOError):
        replace(report, overfit_partition_count=3)
    with pytest.raises(PBOError):
        replace(report, pbo=D("0.5"))
    with pytest.raises(PBOError):
        replace(report, partitions=report.partitions[:-1])
    with pytest.raises(PBOError):
        replace(report, label="RESEARCH_ONLY_STATISTIC")


def test_partition_outcome_pins_rank_convention_for_even_and_odd_n():
    # Overfit iff relative rank r/(N+1) <= 1/2, i.e. logit <= 0, i.e. 2r <= N+1.
    PartitionOutcome((0, 1), (2, 3), "A", 2, 4, True)
    PartitionOutcome((0, 1), (2, 3), "A", 3, 4, False)
    PartitionOutcome((0, 1), (2, 3), "A", 2, 3, True)
    PartitionOutcome((0, 1), (2, 3), "A", 3, 3, False)
    for rank, n, overfit in ((2, 4, False), (3, 4, True), (2, 3, False), (1, 2, False)):
        with pytest.raises(PBOError):
            PartitionOutcome((0, 1), (2, 3), "A", rank, n, overfit)
    for rank in (0, 5, True):
        with pytest.raises(PBOError):
            PartitionOutcome((0, 1), (2, 3), "A", rank, 4, False)
    with pytest.raises(PBOError):
        PartitionOutcome((0, 1), (1, 3), "A", 1, 4, True)


def test_repeated_calls_and_candidate_permutations_are_identical():
    family = known_family()
    first = run(family)
    assert run(family) == first
    assert run(tuple(reversed(family))) == first
    assert run((family[1], family[2], family[0])) == first


def test_in_sample_tie_for_winner_fails_closed():
    # B s0 = 5 instead of 5.5: partition {0,2} has IS A = B = 6.
    family = known_family()
    family = (family[0], series("B", "4.5", "0.5", "-1", "1", "1", "0", "0.25", "-0.25"),
              family[2])
    with pytest.raises(PBOError, match="in-sample tie"):
        run(family)


def test_out_of_sample_tie_with_selected_candidate_fails_closed():
    # Train {0,1}: IS A = 6 > B = 2 -> A; OOS A = 6 == B = 6 -> rank undefined.
    family = from_slice_sums({"A": (3, 3, 3, 3), "B": (1, 1, 3, 3)})
    with pytest.raises(PBOError, match="out-of-sample tie"):
        run(family)


def test_ties_not_involving_selection_or_selected_rank_are_allowed():
    # In the known fixture, train {0,1} has OOS B == C == 1 while A ranks 3.
    report = run(known_family())
    assert report.partitions[0].oos_rank == 3


@pytest.mark.parametrize("bad", [
    0.5, 1, True, None, "0.5", D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity"),
    D("1E-19"), D("1E+19"), D("-1E+19"),
])
def test_non_decimal_non_finite_and_out_of_range_values_fail_closed(bad):
    family = known_family()
    rows = list(family[0].returns)
    rows[3] = bad
    with pytest.raises(PBOError):
        run((CandidateReturns("A", tuple(rows)),) + family[1:])


def test_decimal_subclass_rejected():
    class Sneaky(Decimal):
        pass

    family = known_family()
    rows = (Sneaky("1.25"),) + family[0].returns[1:]
    with pytest.raises(PBOError):
        run((CandidateReturns("A", rows),) + family[1:])


def test_unequal_lengths_and_ragged_panels_fail_closed():
    family = known_family()
    with pytest.raises(PBOError):
        run((family[0], CandidateReturns("B", family[1].returns[:-1]), family[2]))
    with pytest.raises(PBOError):
        run(family, ts=stamps(7))
    with pytest.raises(PBOError):
        run(family, ts=stamps(9))


@pytest.mark.parametrize("ts", [
    stamps(7) + (BASE,),                               # not increasing
    stamps(7) + (BASE + 6_000,),                       # equal timestamp
    (BASE + 1,) + stamps(8)[1:][::-1],                 # reversed
    (0,) + stamps(8)[1:],                              # zero
    (-5,) + stamps(8)[1:],                             # negative
    stamps(7) + (2**63,),                              # out of int64
    stamps(7) + (True,),                               # bool
    stamps(7) + (float(BASE + 9_000),),                # float
    list(stamps(8)),                                   # not a tuple
])
def test_invalid_timestamps_fail_closed(ts):
    with pytest.raises(PBOError):
        run(known_family(), ts=ts)


@pytest.mark.parametrize("slice_count", [0, 2, 3, 5, 18, -4, True, 4.0, None])
def test_invalid_or_too_small_or_too_large_slice_counts_fail_closed(slice_count):
    with pytest.raises(PBOError):
        run(known_family(), slice_count=slice_count)


def test_rows_not_divisible_or_too_few_per_slice_fail_closed():
    family = known_family()
    # 8 rows into 6 slices: not equal slices.
    with pytest.raises(PBOError):
        run(family, slice_count=6)
    # 8 rows into 8 slices: one observation per slice is too small.
    with pytest.raises(PBOError):
        run(family, slice_count=8)


def test_final_winner_only_or_undeclared_family_fails_closed():
    family = known_family()
    with pytest.raises(PBOError):
        run(family[:1])
    with pytest.raises(PBOError):
        run(family[:1], family=40)
    with pytest.raises(PBOError):
        run(family, family=4)
    with pytest.raises(PBOError):
        run(family, family=True)
    with pytest.raises(PBOError):
        estimate_pbo(stamps(8), list(family), slice_count=4,
                     search_family_size=3, score=MEAN_RETURN_SCORE)


@pytest.mark.parametrize("score", ["sharpe", "MEAN_RETURN", "", None, 1])
def test_only_predeclared_mean_return_score_is_supported(score):
    with pytest.raises(PBOError):
        run(known_family(), score=score)


@pytest.mark.parametrize("name", ["", "a\x00b", "x​", "é", 7, "x" * 129])
def test_unsafe_candidate_ids_fail_closed(name):
    family = known_family()
    with pytest.raises(PBOError):
        run((CandidateReturns(name, family[0].returns),) + family[1:])


def test_duplicate_ids_and_duplicate_return_series_fail_closed():
    family = known_family()
    with pytest.raises(PBOError):
        run((family[0], CandidateReturns("A", family[1].returns), family[2]))
    with pytest.raises(PBOError, match="duplicate"):
        run((family[0], CandidateReturns("A2", family[0].returns), family[1]))


def test_non_tuple_returns_and_foreign_candidate_types_fail_closed():
    family = known_family()
    with pytest.raises(PBOError):
        run((CandidateReturns("A", list(family[0].returns)),) + family[1:])
    with pytest.raises(PBOError):
        estimate_pbo(stamps(8), (("A", family[0].returns),) + family[1:], slice_count=4,
                     search_family_size=3, score=MEAN_RETURN_SCORE)


def test_combinatorial_explosion_refused_before_value_validation():
    # S = 16 -> C(16, 8) = 12870 partitions; 21 candidates exceeds the work budget.
    start = time.perf_counter()
    rows = 32
    bad_value = (0.5,) * rows   # never validated: structural refusal comes first
    family = tuple(CandidateReturns(f"c{i:02d}", bad_value) for i in range(21))
    with pytest.raises(PBOError, match="budget"):
        estimate_pbo(stamps(rows), family, slice_count=16, search_family_size=21,
                     score=MEAN_RETURN_SCORE)
    assert time.perf_counter() - start < 1.0


def test_largest_slice_count_runs_within_bounds():
    rows = 32
    state = 20261102
    family = []
    for i in range(3):
        values = []
        for _ in range(rows):
            # Fixed LCG: exact 6-decimal returns in [-1, 1]; deterministic, no RNG import.
            state = (state * 1103515245 + 12345) % 2**31
            values.append(D(state % 2_000_001 - 1_000_000) / D(10**6))
        family.append(CandidateReturns(f"c{i}", tuple(values)))
    family = tuple(family)
    start = time.perf_counter()
    report = estimate_pbo(stamps(rows), family, slice_count=16, search_family_size=3,
                          score=MEAN_RETURN_SCORE)
    assert report.partition_count == 12870
    assert len(report.partitions) == 12870
    assert time.perf_counter() - start < 30.0


def test_runs_with_network_and_process_launch_denied(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network or subprocess forbidden in PBO research math")

    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    assert run(known_family()).overfit_partition_count == 2


def test_module_imports_only_pure_stdlib_and_has_no_escape_hatches():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    allowed = {"__future__", "dataclasses", "decimal", "enum", "unicodedata"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] in allowed for a in node.names), node.lineno
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and (node.module or "").split(".")[0] in allowed, node.lineno
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {
                "eval", "exec", "compile", "__import__", "open", "getattr", "setattr",
                "globals", "locals", "vars", "input"}, node.lineno
        elif isinstance(node, ast.Name):
            assert node.id not in {"__builtins__", "os", "sys", "socket", "subprocess"}, node.lineno
    public = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
              and not n.name.startswith("_")}
    assert public == {"PBOError", "PBOLabel", "CandidateReturns", "PartitionOutcome",
                      "PBOReport", "estimate_pbo"}
