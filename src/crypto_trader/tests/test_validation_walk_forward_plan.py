"""Stage 3A: synthetic temporal-layout tests; zero strategy or execution authority."""
from __future__ import annotations

import ast
from dataclasses import replace
from enum import IntEnum
from pathlib import Path
import socket
import subprocess

import pytest

from crypto_trader.validation.walk_forward_plan import (
    FoldWindow, SampleWindow, SplitLabel, SplitLayoutReport,
    SplitPlanError, plan_synthetic_folds,
)

BASE = 1_800_000_000_000_000_000
MODULE_PATH = Path(__file__).resolve().parents[1] / "validation" / "walk_forward_plan.py"


def sample(name, decision, end=None, feature=None, revision=None):
    return SampleWindow(
        name, decision,
        decision if feature is None else feature,
        decision if revision is None else revision,
        decision + 1 if end is None else end,
    )


def fold(start=0, end=20, val_start=30, val_end=50, gap=0):
    return FoldWindow(BASE + start, BASE + end, BASE + val_start,
                      BASE + val_end, gap)


def valid():
    rows = (sample("train", BASE + 2, BASE + 5),
            sample("valid", BASE + 33, BASE + 35))
    return rows, (fold(),)


def test_valid_plan_inert_and_complete_pair_codes():
    rows, folds = valid()
    report = plan_synthetic_folds(rows, folds)
    assert report.label is SplitLabel.EXPLORATORY_SPLIT_ONLY
    assert report.reasons == ("synthetic_metadata_unverified", "anchor_missing")
    assert not any((report.permits_causal_use, report.permits_trial_allocation,
                    report.permits_promotion, report.permits_trading))
    assert len(report.split_layout_digest) == 64
    f = report.folds[0]
    assert f.train_ids == ("train",)
    assert f.validation_ids == ("valid",)
    assert f.pair_codes == (("train", "TRAIN"), ("valid", "VALIDATION"))
    assert len(f.fold_id) == 64


def test_permutation_invariance_and_metadata_changes_digest():
    rows, fs = valid()
    first = plan_synthetic_folds(rows, fs)
    assert plan_synthetic_folds(tuple(reversed(rows)), fs) == first
    altered = (replace(rows[0], feature_available_utc_ns=BASE + 1), rows[1])
    changed = plan_synthetic_folds(altered, fs)
    assert changed.split_layout_digest != first.split_layout_digest
    assert changed.folds[0].pair_codes == first.folds[0].pair_codes
    assert plan_synthetic_folds(rows, (replace(fs[0], pre_validation_gap_ns=1),)).split_layout_digest != first.split_layout_digest


@pytest.mark.parametrize("row", [
    lambda: sample("x", BASE + 5, feature=BASE + 6),
    lambda: sample("x", BASE + 5, revision=BASE + 6),
    lambda: sample("x", BASE + 5, end=BASE + 5),
    lambda: sample("x", BASE + 5, end=BASE + 4),
    lambda: sample("x", True, end=BASE + 8),
    lambda: sample("x", float(BASE), end=BASE + 8),
    lambda: sample("x", 2**63),
    lambda: sample("x", 0),
    lambda: sample("x", -1),
    lambda: sample("", BASE + 5),
    lambda: sample("bad\x00name", BASE + 5),
    lambda: sample("bad\x1fname", BASE + 5),
    lambda: sample("e\u0301", BASE + 5),
    lambda: sample("\ud800", BASE + 5),
    lambda: sample("x" * 129, BASE + 5),
])
def test_invalid_sample_fails_closed(row):
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds((row(),) + valid()[0], valid()[1])


def test_invalid_row_rejects_entire_call():
    rows, fs = valid()
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds(rows + (sample("bad", BASE + 10, feature=BASE + 11),), fs)


def test_one_nanosecond_boundaries_and_conservative_tie_purge():
    rows = (
        sample("train_start", BASE, BASE + 1),
        sample("train_end_minus", BASE + 19, BASE + 20),
        sample("train_end", BASE + 20, BASE + 21),
        sample("cutoff_minus", BASE + 3, BASE + 28),
        sample("cutoff_tie", BASE + 4, BASE + 29),
        sample("cutoff_plus", BASE + 5, BASE + 30),
        sample("validation_start", BASE + 30, BASE + 31),
        sample("validation_end_minus", BASE + 49, BASE + 50),
        sample("validation_end", BASE + 50, BASE + 51),
    )
    f = plan_synthetic_folds(rows, (fold(gap=1),)).folds[0]
    assert f.train_ids == ("cutoff_minus", "train_end_minus", "train_start")
    assert f.validation_ids == ("validation_end_minus", "validation_start")
    codes = dict(f.pair_codes)
    assert codes["cutoff_tie"] == codes["cutoff_plus"] == "PURGED_GAP_OVERLAP"
    assert codes["train_end"] == codes["validation_end"] == "OUTSIDE_WINDOWS"


def test_validation_exposure_overrun_refuses_whole_call():
    rows, fs = valid()
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds(rows + (sample("over", BASE + 40, BASE + 51),), fs)
    assert plan_synthetic_folds(rows + (sample("tie", BASE + 49, BASE + 50),), fs).folds[0].validation_ids == ("tie", "valid")


def test_empty_training_or_validation_fold_rejected():
    rows, fs = valid()
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds((rows[1],), fs)
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds((rows[0],), fs)


@pytest.mark.parametrize("bad_fold", [
    lambda f: replace(f, train_start_utc_ns=f.train_end_utc_ns),
    lambda f: replace(f, train_end_utc_ns=f.validation_start_utc_ns + 1),
    lambda f: replace(f, validation_end_utc_ns=f.validation_start_utc_ns),
    lambda f: replace(f, pre_validation_gap_ns=-1),
    lambda f: replace(f, pre_validation_gap_ns=True),
    lambda f: replace(f, pre_validation_gap_ns=2**63),
    lambda f: replace(f, pre_validation_gap_ns=30),
    lambda f: replace(f, validation_start_utc_ns=0),
    lambda f: replace(f, train_start_utc_ns=1.0),
])
def test_bad_fold_refused(bad_fold):
    rows, fs = valid()
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds(rows, (bad_fold(fs[0]),))


def test_overlapping_and_reverse_folds_refused():
    fs = (fold(), fold(start=10, end=40, val_start=45, val_end=65))
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds(valid()[0], fs)
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds(valid()[0], tuple(reversed(fs)))


def test_duplicate_ids_and_invalid_containers_refused():
    rows, fs = valid()
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds(rows + (rows[0],), fs)
    class TupleSubclass(tuple):
        pass
    class RowSubclass(SampleWindow):
        pass
    class FoldSubclass(FoldWindow):
        pass
    bad = (RowSubclass("other", BASE + 6, BASE + 6, BASE + 6, BASE + 8),)
    for samples, folds in [
        (list(rows), fs), (TupleSubclass(rows), fs), (iter(rows), fs),
        (rows, list(fs)), (rows, TupleSubclass(fs)),
        (rows + bad, fs), (rows, (FoldSubclass(*fs[0].__dict__.values()),)),
        (rows * 1025, fs), (rows, fs * 33),
    ]:
        with pytest.raises(SplitPlanError):
            plan_synthetic_folds(samples, folds)


def test_mutated_frozen_dataclass_revalidated_at_call_entry():
    rows, fs = valid()
    x = replace(rows[0])
    object.__setattr__(x, "feature_available_utc_ns", BASE + 100)
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds((x, rows[1]), fs)


def test_non_authority_flags_constructor_and_unknown_kwargs():
    rows, fs = valid()
    result = plan_synthetic_folds(rows, fs)
    with pytest.raises((SplitPlanError, ValueError)):
        replace(result, permits_trading=True)
    with pytest.raises((SplitPlanError, ValueError)):
        replace(result, label="CAUSAL_ELIGIBLE")
    with pytest.raises(TypeError):
        plan_synthetic_folds(rows, fs, trusted=True)
    assert not hasattr(fs[0], "embargo_ns")


def test_large_integer_ns_exact_and_correct_bounds():
    hi = 2**63 - 1
    rows = (sample("early", hi - 100, hi - 90),
            sample("later", hi - 30, hi - 1))
    f = FoldWindow(hi - 120, hi - 60, hi - 40, hi, 2)
    result = plan_synthetic_folds(rows, (f,))
    assert result.folds[0].train_ids == ("early",)
    assert result.folds[0].validation_ids == ("later",)
    assert len(result.split_layout_digest) == 64


def test_two_disjoint_validation_folds():
    rows = (sample("train1", BASE + 1, BASE + 3),
            sample("valid1", BASE + 31, BASE + 35),
            sample("train2", BASE + 51, BASE + 53),
            sample("valid2", BASE + 81, BASE + 85))
    fs = (fold(), fold(start=50, end=70, val_start=80, val_end=90))
    result = plan_synthetic_folds(rows, fs)
    assert len(result.folds) == 2
    assert result.folds[1].train_ids == ("train2",)
    assert result.folds[1].validation_ids == ("valid2",)
    assert result.folds[0].fold_id != result.folds[1].fold_id


def test_exact_capacity_with_duplicate_names_refuses_early():
    rows, fs = valid()
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds(rows * 1025, fs)
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds(rows, fs * 33)


def _strict_planner_scanner(tree):
    """Supplement the existing, broader package scanner without editing it."""
    allowed_from = {
        "__future__": ("annotations",),
        "dataclasses": ("dataclass",),
        "enum": ("Enum",),
        "unicodedata": ("category", "normalize"),
        "canonical": ("content_hash",),
    }
    forbidden_names = {
        "open", "eval", "exec", "compile", "__import__", "getattr", "setattr",
        "globals", "locals", "vars", "__builtins__", "breakpoint",
    }
    # New attributes require explicit review. Restrict to the vetted planner's
    # static interface, not merely a denylist of known dangerous attributes.
    allowed_attributes = {
        "EXPLORATORY_SPLIT_ONLY", "add", "append", "decision_utc_ns",
        "exposure_end_utc_ns", "feature_available_utc_ns", "fold_id", "folds",
        "label", "permits_causal_use", "permits_promotion", "permits_trading",
        "permits_trial_allocation", "pre_validation_gap_ns", "reasons",
        "revision_first_seen_utc_ns", "sample_id", "split_layout_digest",
        "train_end_utc_ns", "train_start_utc_ns", "validation_end_utc_ns",
        "validation_start_utc_ns", "value",
    }
    # No implicit builtins, module globals, or undeclared names. This is a
    # deliberately frozen source-surface allowlist for the audited planner.
    allowed_names = {
        "EXPLORATORY_SPLIT_ONLY", "Enum", "FoldLayout", "FoldWindow",
        "SampleWindow", "SplitLabel", "SplitLayoutReport", "SplitPlanError",
        "ValueError", "_DOMAIN", "_MAX_FOLDS", "_MAX_NS", "_MAX_SAMPLES",
        "_REASONS", "_SEMANTICS", "_fold_evidence", "_id", "_sample_evidence",
        "_time", "_validated_fold", "_validated_sample", "a", "allow_zero",
        "any", "b", "bool", "c", "category", "char", "checked_folds", "code",
        "codes", "content_hash", "cutoff", "d", "dataclass", "decision",
        "decision_utc_ns", "dict", "digest", "e", "exposure_end_utc_ns",
        "f", "feature_available_utc_ns", "fold", "fold_id", "fold_layouts",
        "folds", "gap", "int", "label", "layouts", "len", "list",
        "normalize", "object", "ordered_rows", "pair_codes", "pairs",
        "permits_causal_use", "permits_promotion", "permits_trading",
        "permits_trial_allocation", "pre_validation_gap_ns",
        "prev_validation_end", "r", "reasons", "revision_first_seen_utc_ns",
        "row", "rows", "sample", "sample_digest", "sample_id", "samples",
        "seen", "self", "set", "sorted", "split_layout_digest", "str",
        "train", "train_end_utc_ns", "train_ids", "train_start_utc_ns",
        "tuple", "type", "v", "validation", "validation_end_utc_ns",
        "validation_ids", "validation_start_utc_ns", "value", "window",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            raise AssertionError(("unapproved direct import", node.lineno))
        if isinstance(node, ast.ImportFrom):
            if node.level:
                if not (node.level == 1 and node.module == "canonical"):
                    raise AssertionError(("unapproved relative import", node.lineno))
            elif node.module not in allowed_from or node.module == "canonical":
                raise AssertionError(("unapproved absolute import", node.lineno))
            if tuple(alias.name for alias in node.names) != allowed_from[node.module]:
                raise AssertionError(("unapproved import symbols", node.lineno))
            if any(alias.asname is not None for alias in node.names):
                raise AssertionError(("aliased import", node.lineno))
        if isinstance(node, ast.Name):
            if node.id not in allowed_names or node.id.startswith("__") or node.id in forbidden_names:
                raise AssertionError(("undeclared callable or global", node.id))
        if isinstance(node, ast.Match):
            # MatchClass.kwd_attrs can smuggle dunder names as plain strings.
            raise AssertionError(("unsupported structural match", node.lineno))
        if isinstance(node, ast.keyword):
            # Keyword names are plain strings, not ast.Name nodes.
            if node.arg not in {"allow_zero", "frozen", "key"}:
                raise AssertionError(("unreviewed keyword argument", node.arg))
        if isinstance(node, ast.arg):
            if node.arg not in {
                "allow_zero", "fold", "folds", "row", "sample",
                "samples", "self", "value",
            }:
                raise AssertionError(("unreviewed function parameter", node.arg))
        if isinstance(node, ast.AsyncFunctionDef):
            # No asynchronous definitions are approved in the pinned planner.
            raise AssertionError(("unreviewed async function definition", node.name))
        if isinstance(node, ast.FunctionDef):
            if node.name not in {
                "__post_init__", "_fold_evidence", "_id", "_sample_evidence",
                "_time", "_validated_fold", "_validated_sample",
                "plan_synthetic_folds",
            }:
                raise AssertionError(("unreviewed function definition", node.name))
        if isinstance(node, ast.ClassDef):
            if node.name not in {
                "FoldLayout", "FoldWindow", "SampleWindow", "SplitLabel",
                "SplitLayoutReport", "SplitPlanError",
            }:
                raise AssertionError(("unreviewed class definition", node.name))
        if isinstance(node, (ast.Global, ast.Nonlocal, ast.While)):
            raise AssertionError(("unreviewed control or scope statement", node.lineno))
        if isinstance(node, ast.ExceptHandler) and node.name is not None:
            raise AssertionError(("unreviewed exception binding", node.name))
        if isinstance(node, ast.Attribute):
            if node.attr not in allowed_attributes or node.attr.startswith("__"):
                raise AssertionError(("unreviewed attribute", node.attr))
            if node.attr in {"open", "system", "popen", "connect", "allocate", "place_order"}:
                raise AssertionError(("forbidden attribute", node.attr))


@pytest.mark.parametrize("source", [
    "async def unreviewed():\n    pass",
    "async def _id(value):\n    pass",
])
def test_planner_scanner_rejects_unreviewed_async_definitions(source):
    with pytest.raises(AssertionError, match="unreviewed async function definition"):
        _strict_planner_scanner(ast.parse(source))

def test_new_source_isolation_ast_including_adversarial_specimens():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    _strict_planner_scanner(tree)
    for bad in (
        "import sqlite3", "import socket", "from .registry import TrialRegistry",
        "from .dataset_admission import inspect_dataset", "from pathlib import Path",
        "from builtins import open", "open('file')", "getattr(x, 'secret')",
        "import os", "import subprocess", "TrialRegistry.allocate()",
    ):
        with pytest.raises(AssertionError):
            _strict_planner_scanner(ast.parse(bad))


def test_call_has_no_network_or_process_launch(monkeypatch):
    def forbid(*args, **kwargs):
        raise AssertionError("no network or process access allowed in planner")
    monkeypatch.setattr(socket, "socket", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    monkeypatch.setattr(subprocess, "Popen", forbid)
    rows, fs = valid()
    assert plan_synthetic_folds(rows, fs).label is SplitLabel.EXPLORATORY_SPLIT_ONLY


def test_planner_does_not_allocate_registry_trial(monkeypatch):
    from crypto_trader.validation.registry import TrialRegistry
    def forbid(*args, **kwargs):
        raise AssertionError("TrialRegistry.allocate forbidden")
    monkeypatch.setattr(TrialRegistry, "allocate", forbid)
    rows, fs = valid()
    assert plan_synthetic_folds(rows, fs).folds[0].train_ids == ("train",)


def test_bound_output_uses_immutable_tuples_and_numerical_hash_strings():
    rows, fs = valid()
    result = plan_synthetic_folds(rows, fs)
    assert isinstance(result.folds, tuple)
    assert isinstance(result.folds[0].pair_codes, tuple)
    with pytest.raises(Exception):
        result.folds += ()
    with pytest.raises(Exception):
        result.folds[0].pair_codes += (("forged", "TRAIN"),)


def test_equality_available_at_decision_synthetic_only():
    rows, fs = valid()
    assert rows[0].feature_available_utc_ns == rows[0].decision_utc_ns
    result = plan_synthetic_folds(rows, fs)
    assert result.reasons == ("synthetic_metadata_unverified", "anchor_missing")


def test_maximum_approved_2048_samples_and_32_folds_stays_bounded():
    rows = []
    folds = []
    for i in range(32):
        t = BASE + i * 100
        folds.append(FoldWindow(t, t + 20, t + 30, t + 50, 1))
        rows.extend((
            sample(f"train_{i:02d}", t + 1, t + 3),
            sample(f"valid_{i:02d}", t + 31, t + 35),
        ))
    rows.extend(
        sample(f"outside_{i:04d}", BASE + 5000 + i, BASE + 5001 + i)
        for i in range(2048 - len(rows))
    )
    assert len(rows) == 2048 and len(folds) == 32
    report = plan_synthetic_folds(tuple(rows), tuple(folds))
    assert len(report.folds) == 32
    assert all(len(f.pair_codes) == 2048 for f in report.folds)
    assert all(len(f.train_ids) == len(f.validation_ids) == 1 for f in report.folds)
    assert not report.permits_trial_allocation
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds(tuple(rows) + (sample("overflow", BASE + 8000),), tuple(folds))


def test_malformed_ns_and_identity_classes_not_coerced():
    class SpecialInt(IntEnum):
        EPOCH = BASE
    rows, fs = valid()
    for bad in (True, SpecialInt.EPOCH, 1.5, 2**63):
        with pytest.raises(SplitPlanError):
            plan_synthetic_folds((replace(rows[0], decision_utc_ns=bad), rows[1]), fs)
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds((replace(rows[0], sample_id=42), rows[1]), fs)
    with pytest.raises(SplitPlanError):
        plan_synthetic_folds((replace(rows[0], sample_id="\u0080"), rows[1]), fs)


def test_no_existing_validation_source_depends_on_new_planner():
    root = MODULE_PATH.parent
    for path in root.glob("*.py"):
        if path == MODULE_PATH:
            continue
        assert "walk_forward_plan" not in path.read_text(encoding="utf-8")


# Stage 3A v0.3 review-closure regression cases. Do not weaken precise time bounds.
def test_adjacent_validation_folds_are_accepted_without_overlap():
    rows = (
        sample("t", BASE + 2, BASE + 4),
        sample("v1", BASE + 31, BASE + 35),
        sample("v2", BASE + 51, BASE + 53),
    )
    fs = (fold(val_start=30, val_end=50),
          fold(val_start=50, val_end=70))
    report = plan_synthetic_folds(rows, fs)
    assert tuple(x.validation_ids for x in report.folds) == (("v1",), ("v2",))
    assert tuple(x.train_ids for x in report.folds) == (("t",), ("t",))


def test_one_ns_validation_overlap_is_rejected():
    rows = (sample("t", BASE + 2, BASE + 4),
            sample("v1", BASE + 31, BASE + 35),
            sample("v2", BASE + 51, BASE + 53))
    fs = (fold(val_start=30, val_end=50),
          fold(val_start=49, val_end=70))
    with pytest.raises(SplitPlanError, match="overlap or are unordered"):
        plan_synthetic_folds(rows, fs)


def test_train_start_minus_one_and_validation_start_minus_one_are_outside():
    rows = (sample("pretrain", BASE + 1, BASE + 2),
            sample("train", BASE + 3, BASE + 5),
            sample("preval", BASE + 29, BASE + 31),
            sample("valid", BASE + 33, BASE + 35))
    layout = plan_synthetic_folds(rows, (fold(start=2, end=20),)).folds[0]
    codes = dict(layout.pair_codes)
    assert codes["pretrain"] == "OUTSIDE_WINDOWS"
    assert codes["preval"] == "OUTSIDE_WINDOWS"
    assert layout.train_ids == ("train",)
    assert layout.validation_ids == ("valid",)


def test_train_end_equals_validation_start_is_valid_geometry():
    rows = (sample("train", BASE + 2, BASE + 5),
            sample("valid", BASE + 33, BASE + 35))
    f = fold(start=0, end=30, val_start=30, val_end=50)
    result = plan_synthetic_folds(rows, (f,))
    assert result.folds[0].train_ids == ("train",)
    assert result.folds[0].validation_ids == ("valid",)


def test_cutoff_one_ns_after_train_start_rejects_empty_train():
    rows = (sample("purged", BASE + 1, BASE + 2),
            sample("valid", BASE + 33, BASE + 35))
    # Exposure is half-open but must end STRICTLY before cutoff.
    # With integer ns and cutoff = train_start + 1, no positive exposure fits.
    f = fold(start=1, end=10, val_start=20, val_end=50, gap=18)
    with pytest.raises(SplitPlanError, match="empty training or validation fold"):
        plan_synthetic_folds(rows, (f,))


def test_reverse_order_rejected_even_when_folds_are_disjoint():
    rows = (sample("t", BASE + 2, BASE + 4),
            sample("v1", BASE + 31, BASE + 35),
            sample("v2", BASE + 61, BASE + 63))
    fs = (fold(val_start=30, val_end=50),
          fold(val_start=60, val_end=80))
    assert len(plan_synthetic_folds(rows, fs).folds) == 2
    with pytest.raises(SplitPlanError, match="overlap or are unordered"):
        plan_synthetic_folds(rows, tuple(reversed(fs)))


def test_revision_timestamp_change_alters_digest_without_changing_membership():
    rows, fs = valid()
    original = plan_synthetic_folds(rows, fs)
    shifted = (replace(rows[0], revision_first_seen_utc_ns=BASE + 1), rows[1])
    result = plan_synthetic_folds(shifted, fs)
    assert result.folds[0].pair_codes == original.folds[0].pair_codes
    assert result.split_layout_digest != original.split_layout_digest


def test_planner_ast_rejects_import_alias_and_dunder_escape_specimens():
    # Static inspection only: none of these strings is executed.
    specimens = (
        "from .canonical import __builtins__ as b\\nb['open']('x', 'w')",
        "from unicodedata import __dict__ as items",
        "().__class__.__mro__[1].__subclasses__()",
    )
    for specimen in specimens:
        with pytest.raises(AssertionError):
            _strict_planner_scanner(ast.parse(specimen.replace('\\n', '\n')))

# Stage3A follow-up: independently audited AST escape specimens are parse-only.
@pytest.mark.parametrize("specimen", (
    "match content_hash:\n    case object(__globals__=g):\n        b = g['__builtins__']",
    "g = (i for i in ()); x = g.gi_frame.f_builtins['open']",
    "x = __loader__.get_data('something')",
    "x = builtins.getattr(x, 'open')",
    "'{0.__globals__}'.format(content_hash)",
))
def test_scanner_refuses_indirect_runtime_access_without_executing_it(specimen):
    # Parsing these untrusted strings does NOT execute them.
    with pytest.raises(AssertionError):
        _strict_planner_scanner(ast.parse(specimen))


def test_mixed_gap_cutoff_inside_training_window_is_conservative():
    # Fold cutoff is BASE+15, strictly inside the training decision window.
    rows = (
        sample("train", BASE + 12, BASE + 14),
        sample("cutoff_tie", BASE + 13, BASE + 15),
        sample("decision_at_cutoff", BASE + 15, BASE + 16),
        sample("valid", BASE + 33, BASE + 35),
    )
    report = plan_synthetic_folds(rows, (fold(gap=15),))
    f = report.folds[0]
    assert f.train_ids == ("train",)
    assert f.validation_ids == ("valid",)
    codes = dict(f.pair_codes)
    assert codes["train"] == "TRAIN"
    assert codes["cutoff_tie"] == "PURGED_GAP_OVERLAP"
    assert codes["decision_at_cutoff"] == "PURGED_GAP_OVERLAP"


def test_fixed_known_answer_hashes_for_valid_synthetic_fixture():
    # The independent research manifest pins both values across runs.
    report = plan_synthetic_folds(*valid())
    assert report.split_layout_digest == (
        "cf822a7ff539998260b7f5c831fca7bb07f96121c87bff8f8816ea2030e8e998"
    )
    assert report.folds[0].fold_id == (
        "0c39d99a1332f27e0f18f7eb8c9949631754fe88d48327fe1e7743f3289490fd"
    )


def test_report_constructor_is_not_a_trust_boundary():
    # Document a P2 representational weakness without claiming privileges.
    forged = SplitLayoutReport((), "x" * 64)
    assert forged.folds == ()
    assert forged.permits_causal_use is False
    assert forged.permits_trial_allocation is False
    assert forged.permits_promotion is False
    assert forged.permits_trading is False

@pytest.mark.parametrize("specimen", (
    "license()",
    "input('read stdin')",
    "print('write stdout')",
    "raise SystemExit",
    "quit()",
))
def test_scanner_refuses_unreviewed_builtin_or_process_exit(specimen):
    # Parse-only adversarial specimens: not executed in this test.
    with pytest.raises(AssertionError):
        _strict_planner_scanner(ast.parse(specimen))

def test_scanner_enforces_deny_rules_under_python_optimized_bytecode():
    own_source = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    scanner_def = next(
        n for n in own_source.body
        if isinstance(n, ast.FunctionDef) and n.name == "_strict_planner_scanner"
    )
    assert not any(isinstance(n, ast.Assert) for n in ast.walk(scanner_def))
    sandbox = {"ast": ast}
    exec(
        compile(ast.Module(body=[scanner_def], type_ignores=[]),
                "<optimized-scanner-only>", "exec", optimize=2),
        sandbox,
    )
    with pytest.raises(AssertionError):
        sandbox["_strict_planner_scanner"](ast.parse("license()"))
    with pytest.raises(AssertionError):
        sandbox["_strict_planner_scanner"](ast.parse("import socket"))

@pytest.mark.parametrize("specimen", (
    "FoldLayout(__reduce__=1)",
    "str(__globals__=1)",
    "FoldLayout(**dict())",
    "def sample(__globals__):\n    pass",
    "def unreviewed():\n    pass",
    "class Impostor:\n    pass",
    "global __builtins__",
    "nonlocal value",
    "while True:\n    pass",
))
def test_planner_scanner_rejects_unreviewed_ast_string_fields(specimen):
    # No submitted specimen is compiled or executed, only parsed.
    with pytest.raises(AssertionError):
        _strict_planner_scanner(ast.parse(specimen))


def test_planner_scanner_accepts_reviewed_keyword_contract():
    _strict_planner_scanner(ast.parse("FoldLayout(key=value)"))
    _strict_planner_scanner(ast.parse("FoldLayout(frozen=True)"))
