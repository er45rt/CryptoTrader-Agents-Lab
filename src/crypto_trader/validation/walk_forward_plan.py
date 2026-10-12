"""Pure synthetic temporal layout planner; no causal or execution authority.

A split layout digest asserts deterministic metadata/layout identity only. It does
not attest source code, market timestamp correctness, strategy evaluation, custody,
holdout independence, or admission. External reviewers must bind code bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from unicodedata import category, normalize

from .canonical import content_hash

_MAX_NS = 2**63 - 1
_MAX_SAMPLES = 2048
_MAX_FOLDS = 32
_DOMAIN = "crypto-trader-walk-forward-layout-v1"
# Any canonical-encoding contract change requires a new _DOMAIN and _SEMANTICS
# version; historical content digests must remain interpretable.
_SEMANTICS = "synthetic-halfopen-prevalidation-strict-purge-v1"
_REASONS = ("synthetic_metadata_unverified", "anchor_missing")


class SplitPlanError(ValueError):
    """Fail the entire synthetic request. No partial layout is returned."""


class SplitLabel(str, Enum):
    EXPLORATORY_SPLIT_ONLY = "EXPLORATORY_SPLIT_ONLY"


@dataclass(frozen=True)
class SampleWindow:
    sample_id: str
    decision_utc_ns: int
    feature_available_utc_ns: int
    revision_first_seen_utc_ns: int
    exposure_end_utc_ns: int


@dataclass(frozen=True)
class FoldWindow:
    train_start_utc_ns: int
    train_end_utc_ns: int
    validation_start_utc_ns: int
    validation_end_utc_ns: int
    pre_validation_gap_ns: int


@dataclass(frozen=True)
class FoldLayout:
    fold_id: str
    window: FoldWindow
    train_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    pair_codes: tuple[tuple[str, str], ...]
    # Evidence commitment only, not custody or source authentication.
    sample_digest: str = ""

    def __post_init__(self):
        if (type(self.fold_id) is not str or len(self.fold_id) != 64
                or any(char not in "0123456789abcdef" for char in self.fold_id)
                or type(self.window) is not FoldWindow
                or type(self.train_ids) is not tuple
                or type(self.validation_ids) is not tuple
                or not self.train_ids or not self.validation_ids
                or any(type(row) is not str for row in self.train_ids + self.validation_ids)
                or type(self.pair_codes) is not tuple
                or any(type(v) is not tuple or len(v) != 2
                       or type(v[0]) is not str or type(v[1]) is not str
                       for v in self.pair_codes)):
            raise SplitPlanError("noncanonical synthetic fold layout")
        _validated_fold(self.window)
        if (not 1 <= len(self.pair_codes) <= _MAX_SAMPLES
                or type(self.sample_digest) is not str or len(self.sample_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.sample_digest)
                or tuple(row[0] for row in self.pair_codes) != tuple(sorted(
                    set(row[0] for row in self.pair_codes)))
                or any(row[1] not in ("TRAIN", "VALIDATION", "PURGED_GAP_OVERLAP",
                                     "OUTSIDE_WINDOWS") for row in self.pair_codes)
                or self.train_ids != tuple(row[0] for row in self.pair_codes if row[1] == "TRAIN")
                or self.validation_ids != tuple(row[0] for row in self.pair_codes if row[1] == "VALIDATION")):
            raise SplitPlanError("inconsistent synthetic fold layout")
        for row in self.pair_codes:
            _id(row[0])
        if self.fold_id != content_hash(
                _DOMAIN + "-fold",
                {"semantics_version": _SEMANTICS, "sample_manifest": self.sample_digest,
                 "window": _fold_evidence(self.window), "pair_codes": self.pair_codes}):
            raise SplitPlanError("synthetic fold digest mismatch")


@dataclass(frozen=True)
class SplitLayoutReport:
    folds: tuple[FoldLayout, ...]
    split_layout_digest: str
    sample_digest: str = ""
    label: SplitLabel = SplitLabel.EXPLORATORY_SPLIT_ONLY
    reasons: tuple[str, str] = _REASONS
    permits_causal_use: bool = False
    permits_trial_allocation: bool = False
    permits_promotion: bool = False
    permits_trading: bool = False

    def __post_init__(self):
        if (type(self.label) is not SplitLabel
                or self.label is not SplitLabel.EXPLORATORY_SPLIT_ONLY
                or type(self.reasons) is not tuple
                or any(type(row) is not str for row in self.reasons)
                or self.reasons != _REASONS
                or type(self.folds) is not tuple
                or not 1 <= len(self.folds) <= _MAX_FOLDS
                or any(type(fold) is not FoldLayout for fold in self.folds)
                or type(self.split_layout_digest) is not str
                or len(self.split_layout_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.split_layout_digest)
                or type(self.sample_digest) is not str or len(self.sample_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.sample_digest)):
            raise SplitPlanError("noncanonical synthetic layout report")
        if any(type(v) is not bool or v is not False for v in (
                self.permits_causal_use, self.permits_trial_allocation,
                self.permits_promotion, self.permits_trading)):
            raise SplitPlanError("synthetic layout cannot grant permissions")
        prev_validation_end = 0
        for fold in self.folds:
            # Revalidate caller-supplied layouts, including tampered frozen rows.
            FoldLayout(fold.fold_id, fold.window, fold.train_ids, fold.validation_ids,
                       fold.pair_codes, fold.sample_digest)
            if (fold.sample_digest != self.sample_digest
                    or tuple(row[0] for row in fold.pair_codes) != tuple(
                        row[0] for row in self.folds[0].pair_codes)
                    or fold.window.validation_start_utc_ns < prev_validation_end):
                raise SplitPlanError("inconsistent synthetic report folds")
            prev_validation_end = fold.window.validation_end_utc_ns
        if self.split_layout_digest != content_hash(
                _DOMAIN + "-aggregate",
                {"semantics_version": _SEMANTICS, "synthetic": True,
                 "sample_manifest": self.sample_digest,
                 "fold_ids": [fold.fold_id for fold in self.folds],
                 "label": self.label.value, "reasons": self.reasons,
                 "permits_causal_use": False, "permits_trial_allocation": False,
                 "permits_promotion": False, "permits_trading": False}):
            raise SplitPlanError("synthetic report digest mismatch")


def _time(value: object, *, allow_zero: bool = False) -> int:
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= _MAX_NS:
        raise SplitPlanError("require bounded exact-integer nanoseconds")
    return value


def _id(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 128:
        raise SplitPlanError("invalid synthetic sample ID")
    for char in value:
        if category(char) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            raise SplitPlanError("unsafe Unicode category in sample ID")
    if normalize("NFC", value) != value:
        raise SplitPlanError("sample IDs must already be NFC")
    return value


def _validated_sample(sample: object) -> SampleWindow:
    if type(sample) is not SampleWindow:
        raise SplitPlanError("unexpected synthetic sample type")
    sample_id = _id(sample.sample_id)
    d = _time(sample.decision_utc_ns)
    f = _time(sample.feature_available_utc_ns)
    r = _time(sample.revision_first_seen_utc_ns)
    e = _time(sample.exposure_end_utc_ns)
    if f > d or r > d or e <= d:
        raise SplitPlanError("future availability or invalid half-open exposure")
    # Read and validate each scalar once. All subsequent phases use this owned
    # value copy, even if a caller mutates its frozen dataclass during the call.
    return SampleWindow(sample_id, d, f, r, e)


def _validated_fold(fold: object) -> FoldWindow:
    if type(fold) is not FoldWindow:
        raise SplitPlanError("unexpected synthetic fold type")
    a = _time(fold.train_start_utc_ns)
    b = _time(fold.train_end_utc_ns)
    c = _time(fold.validation_start_utc_ns)
    d = _time(fold.validation_end_utc_ns)
    gap = _time(fold.pre_validation_gap_ns, allow_zero=True)
    if not (a < b <= c < d) or c - gap <= a:
        raise SplitPlanError("invalid training / validation / pre-validation gap")
    # Independent frozen copy of the validated values: FoldLayout.window must
    # never alias a caller-owned object that could later diverge from fold_id.
    return FoldWindow(a, b, c, d, gap)


def _sample_evidence(row: SampleWindow) -> dict[str, str]:
    return {
        "sample_id": row.sample_id,
        "decision_utc_ns": str(row.decision_utc_ns),
        "feature_available_utc_ns": str(row.feature_available_utc_ns),
        "revision_first_seen_utc_ns": str(row.revision_first_seen_utc_ns),
        "exposure_end_utc_ns": str(row.exposure_end_utc_ns),
    }


def _fold_evidence(fold: FoldWindow) -> dict[str, str]:
    return {
        "train_start_utc_ns": str(fold.train_start_utc_ns),
        "train_end_utc_ns": str(fold.train_end_utc_ns),
        "validation_start_utc_ns": str(fold.validation_start_utc_ns),
        "validation_end_utc_ns": str(fold.validation_end_utc_ns),
        "pre_validation_gap_ns": str(fold.pre_validation_gap_ns),
    }


def plan_synthetic_folds(
    samples: tuple[SampleWindow, ...],
    folds: tuple[FoldWindow, ...],
) -> SplitLayoutReport:
    """Layout synthetic metadata only. Any invalid row or fold rejects all output.

    Windows and exposures are half-open. Training exposure must end STRICTLY
    before the pre-validation cutoff: equality is conservatively purged.
    Post-test embargo, real data admission and statistical independence are
    explicitly out of scope.
    """
    if (type(samples) is not tuple or not 1 <= len(samples) <= _MAX_SAMPLES
            or type(folds) is not tuple or not 1 <= len(folds) <= _MAX_FOLDS):
        raise SplitPlanError("sample/fold tuple size outside approved bounds")

    rows: list[SampleWindow] = []
    seen: set[str] = set()
    for row in samples:
        row = _validated_sample(row)
        if row.sample_id in seen:
            raise SplitPlanError("duplicate sample ID")
        seen.add(row.sample_id)
        rows.append(row)

    ordered_rows = tuple(sorted(rows, key=lambda row: row.sample_id))
    prev_validation_end = 0
    checked_folds: list[FoldWindow] = []
    for fold in folds:
        fold = _validated_fold(fold)
        if fold.validation_start_utc_ns < prev_validation_end:
            raise SplitPlanError("validation windows overlap or are unordered")
        prev_validation_end = fold.validation_end_utc_ns
        checked_folds.append(fold)

    sample_digest = content_hash(
        _DOMAIN + "-samples",
        {"semantics_version": _SEMANTICS,
         "samples": [_sample_evidence(row) for row in ordered_rows]},
    )
    fold_layouts: list[FoldLayout] = []
    for fold in checked_folds:
        cutoff = fold.validation_start_utc_ns - fold.pre_validation_gap_ns
        train: list[str] = []
        validation: list[str] = []
        codes: list[tuple[str, str]] = []

        for row in ordered_rows:
            decision = row.decision_utc_ns
            if fold.validation_start_utc_ns <= decision < fold.validation_end_utc_ns:
                if row.exposure_end_utc_ns > fold.validation_end_utc_ns:
                    raise SplitPlanError("validation exposure overruns its fold")
                validation.append(row.sample_id)
                code = "VALIDATION"
            elif fold.train_start_utc_ns <= decision < fold.train_end_utc_ns:
                if row.exposure_end_utc_ns < cutoff:
                    train.append(row.sample_id)
                    code = "TRAIN"
                else:
                    code = "PURGED_GAP_OVERLAP"
            else:
                code = "OUTSIDE_WINDOWS"
            codes.append((row.sample_id, code))

        if not train or not validation:
            raise SplitPlanError("empty training or validation fold")
        pairs = tuple(codes)
        fold_id = content_hash(
            _DOMAIN + "-fold",
            {"semantics_version": _SEMANTICS, "sample_manifest": sample_digest,
             "window": _fold_evidence(fold), "pair_codes": pairs},
        )
        fold_layouts.append(FoldLayout(
            fold_id, fold, tuple(train), tuple(validation), pairs, sample_digest,
        ))

    layouts = tuple(fold_layouts)
    digest = content_hash(
        _DOMAIN + "-aggregate",
        {"semantics_version": _SEMANTICS, "synthetic": True,
         "sample_manifest": sample_digest,
         "fold_ids": [fold.fold_id for fold in layouts],
         "label": SplitLabel.EXPLORATORY_SPLIT_ONLY.value,
         "reasons": _REASONS,
         "permits_causal_use": False, "permits_trial_allocation": False,
         "permits_promotion": False, "permits_trading": False},
    )
    return SplitLayoutReport(layouts, digest, sample_digest)
