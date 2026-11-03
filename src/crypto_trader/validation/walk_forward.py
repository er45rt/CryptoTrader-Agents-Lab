"""P4-04 anchored expanding walk-forward. Research-only measurement: grants no strategy
approval (that is P4-06), promotion, Paper or trading permission.

Run path: the caller builds ``WalkForward(panel, ...)`` and passes ``.evaluate`` to the P4-02
``run_registered_trial``; this module does not import the research path. One walk-forward run
is ONE registered attempt; its windows are not attempts. ``verify_walk_forward`` re-derives
everything except the recorded decisions from the registry, the panel and the preregistration.

Layout (UTC nanoseconds, half-open intervals). The plan is ``config["walk_forward"]``, frozen
and bound by P4-02: warmup_start <= anchor_start; OOS boundaries b_0 < ... < b_K with
K >= MIN_FOLDS folds [b_(k-1), b_k); purge_ns >= 0; period; fee_bps; slippage_bps.
* Used rows have decision in [warmup_start, b_K); other rows are ignored and not hashed.
* Every row: feature_available <= decision, revision_first_seen <= decision < exposure_end.
* Every used row with decision in [b_0, b_K) is one OOS step of exactly one fold, at least
  MIN_OOS_PER_FOLD per fold; its exposure ends by the next step's decision and its fold's end.
* Fold k refits at cutoff c_k = b_(k-1) - purge_ns. fit sees the features of used rows with
  feature_available <= c_k and the realized excess returns of rows with decision >=
  anchor_start and exposure_end <= c_k (matured; the purge drops the rest). Warm-up rows
  (before anchor_start) give features only.
* Step t: decide sees the frozen fit state, a fresh config copy and the features of used rows
  with feature_available <= decision_t. It never receives a realized return or exposure end.
  The framework then applies the position to the step's realized excess return.

Accounting (single asset, Decimal): position p_t in [-1, 1] (fraction of capital); gross
p_t * r_t, r_t the row's excess return over its exposure; turnover |p_t - p_(t-1)|, each fold
starting flat and closed after its last step (+|p_n|); cost = turnover * (fee_bps +
slippage_bps) / 10000, i.e. once per traded side; net = gross - cost. Per fold and over the
concatenated OOS series: mean, (T-1) Sharpe (None at zero variance), prod(1 + net) - 1.
Costs are research assumptions, not observed execution prices.

Holdout: the preregistration document holds holdout {start_utc_ns, end_utc_ns}; its content
hash must be the registered study's preregistration_hash, proven by recomputing the registry
binding. The plan range [warmup_start, b_K) or any used row span meeting the holdout refuses.
This shows a declaration in the local UNANCHORED registry, not that the holdout is blind,
independent or declared first to an independent party (P4-05).

Limits: the guarantee is what the data interface hands over. Python callbacks can still read
data through closures or global state; malicious evaluator code is not contained. Market data
is not admitted: dataset_trial_binding still refuses with anchor_missing, and a SHA256 is
recorded evidence, not trust.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import (
    Context, Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext,
)

from .canonical import (
    SERIALIZER_ID, CanonicalError, _nfc, canonical_bytes, content_hash, input_manifest,
    parse_json_strict, sha256_text,
)
from .contracts import RegistryError, TrialReceipt
from .registry import TrialRegistry

RESULT_DOMAIN = "research-result-v1"     # must equal research_path._RESULT_DOMAIN (tested)
BINDING_DOMAIN = "trial-binding-v1"      # must equal the registry binding domain (tested)
PREREGISTRATION_DOMAIN = "walk-forward-preregistration-v1"
SCHEMA = "walk-forward-oos-v1"
MIN_FOLDS = 3
MIN_OOS_PER_FOLD = 30
MAX_ROWS = 20000
VALID = "VALID_RESEARCH_RESULT"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

_ROWS_DOMAIN = "walk-forward-used-rows-v1"
_INDEX_DOMAIN = "walk-forward-oos-index-v1"
_STATE_DOMAIN = "walk-forward-fit-state-v1"
_MAX_NS = 2**63 - 1
_NS = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
_DEC = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_BPS = Decimal(10000)
_ZERO, _ONE = Decimal(0), Decimal(1)
_PLAN_KEYS = frozenset({
    "warmup_start_utc_ns", "anchor_start_utc_ns", "oos_boundaries_utc_ns", "purge_ns",
    "period", "fee_bps", "slippage_bps",
})

ASSUMPTIONS = (
    "single asset; position is a fraction of capital in [-1, 1]; excess return applies linearly",
    "each fold starts flat and is closed after its last OOS step",
    "cost = turnover * (fee_bps + slippage_bps) / 10000, once per traded side",
    "fit is re-run per fold by the frozen configuration on matured history only",
)
LIMITATIONS = (
    "costs are research assumptions, not observed execution prices",
    "the no-look-ahead guarantee covers the data interface only; callbacks reading closures "
    "or global state are not contained",
    "market data is not admitted (anchor_missing); its SHA256 is recorded evidence, not trust",
    "the holdout declaration is bound in the local UNANCHORED registry; blindness and "
    "independence are not shown (P4-05)",
    "a research result grants no strategy approval, promotion, Paper, Testnet or Live permission",
)


class WalkForwardError(RegistryError):
    """Refuse the whole run; nothing partial is returned."""


@dataclass(frozen=True)
class PanelRow:
    row_id: str
    decision_utc_ns: int
    feature_available_utc_ns: int
    revision_first_seen_utc_ns: int
    exposure_end_utc_ns: int
    excess_return: str
    features: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class FeatureRow:
    """What a decision may see: identity, times and features. No realized outcome."""
    row_id: str
    decision_utc_ns: int
    feature_available_utc_ns: int
    features: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class LabelRow:
    """A matured training outcome, handed to fit only."""
    row_id: str
    decision_utc_ns: int
    exposure_end_utc_ns: int
    excess_return: str


@dataclass(frozen=True)
class _Plan:
    warmup_start: int
    anchor_start: int
    bounds: tuple[int, ...]
    purge: int
    period: str
    cost_rate: Decimal


@dataclass(frozen=True)
class _Fold:
    start: int
    end: int
    cutoff: int
    features: tuple[FeatureRow, ...]
    labels: tuple[LabelRow, ...]
    steps: tuple[PanelRow, ...]


@dataclass(frozen=True)
class _Layout:
    folds: tuple[_Fold, ...]
    by_availability: tuple[FeatureRow, ...]
    used_rows: int
    used_rows_sha256: str
    oos_index_sha256: str


@dataclass(frozen=True)
class WalkForwardReport:
    status: str
    reasons: tuple[str, ...]
    study_id: str
    trial_id: object
    evidence_hashes: tuple[str, ...] = ()
    metrics: dict | None = None
    limitations: tuple[str, ...] = LIMITATIONS
    permits_strategy_approval: bool = False
    permits_promotion: bool = False
    permits_paper_or_trading: bool = False


def _ctx() -> Context:
    return Context(prec=50, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])


def _ns(value, what: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_NS:
        raise WalkForwardError(f"corrupt_timestamp:{what}")
    return value


def _ns_text(value, what: str) -> int:
    """Plan and holdout times are decimal strings: canonical JSON integers stop at 2**53."""
    if type(value) is not str or not _NS.fullmatch(value) or int(value) > _MAX_NS:
        raise WalkForwardError(f"corrupt_timestamp:{what}")
    return int(value)


def _decimal(value, what: str) -> Decimal:
    if type(value) is not str or not _DEC.fullmatch(value):
        raise WalkForwardError(f"invalid_decimal:{what}")
    return Decimal(value)


def _position(value) -> Decimal:
    if type(value) is not str or not _DEC.fullmatch(value) or not -_ONE <= Decimal(value) <= _ONE:
        raise WalkForwardError("invalid_position")
    return Decimal(value)


def _validated_panel(panel) -> tuple[PanelRow, ...]:
    if type(panel) is not tuple or not 1 <= len(panel) <= MAX_ROWS:
        raise WalkForwardError("invalid_panel")
    ids: set[str] = set()
    last = None
    for row in panel:
        if type(row) is not PanelRow:
            raise WalkForwardError("invalid_panel")
        try:
            _nfc(row.row_id)
        except CanonicalError as exc:
            raise WalkForwardError("duplicate_or_empty_row_id") from exc
        if not row.row_id or row.row_id in ids:
            raise WalkForwardError("duplicate_or_empty_row_id")
        ids.add(row.row_id)
        decision = _ns(row.decision_utc_ns, "decision")
        available = _ns(row.feature_available_utc_ns, "feature_available")
        revision = _ns(row.revision_first_seen_utc_ns, "revision_first_seen")
        exposure_end = _ns(row.exposure_end_utc_ns, "exposure_end")
        if available > decision or revision > decision:
            raise WalkForwardError("point_in_time_violation")
        if exposure_end <= decision:
            raise WalkForwardError("invalid_exposure")
        if last is not None and decision <= last:
            raise WalkForwardError("unsorted_or_duplicate_decision")
        last = decision
        if _decimal(row.excess_return, "excess_return") <= -_ONE:
            raise WalkForwardError("invalid_excess_return")
        features = row.features
        if (type(features) is not tuple
                or any(type(p) is not tuple or len(p) != 2 or type(p[0]) is not str
                       or type(p[1]) is not str for p in features)):
            raise WalkForwardError("invalid_features")
        keys = [k for k, _ in features]
        if keys != sorted(set(keys)):
            raise WalkForwardError("invalid_features")
        try:
            for key, value in features:
                _nfc(key)
                _nfc(value)
        except CanonicalError as exc:
            raise WalkForwardError("invalid_features") from exc
    return panel


def _holdout(document) -> tuple[str, int, int]:
    """(preregistration hash, holdout start, holdout end) from the preregistration document."""
    try:
        prereg = content_hash(PREREGISTRATION_DOMAIN, document)
        copy = parse_json_strict(canonical_bytes(document).decode("utf-8"))
    except (CanonicalError, TypeError) as exc:
        raise WalkForwardError("holdout_unproven") from exc
    holdout = copy.get("holdout") if type(copy) is dict else None
    if type(holdout) is not dict or set(holdout) != {"start_utc_ns", "end_utc_ns"}:
        raise WalkForwardError("holdout_unproven")
    try:
        start = _ns_text(holdout["start_utc_ns"], "holdout")
        end = _ns_text(holdout["end_utc_ns"], "holdout")
    except WalkForwardError as exc:
        raise WalkForwardError("holdout_unproven") from exc
    if start >= end:
        raise WalkForwardError("holdout_unproven")
    return prereg, start, end


def _binding(study_id: str, prereg: str, config, manifest, parent) -> str:
    """The registry's trial binding (registry.allocate), recomputed from the preregistration."""
    return content_hash(BINDING_DOMAIN, {
        "study_id": study_id, "preregistration_hash": prereg, "serializer_id": SERIALIZER_ID,
        "config": config, "manifest": manifest, "parent": parent,
    })


def _plan(config) -> _Plan:
    plan = config.get("walk_forward") if type(config) is dict else None
    if type(plan) is not dict or set(plan) != _PLAN_KEYS:
        raise WalkForwardError("invalid_plan")
    warmup = _ns_text(plan["warmup_start_utc_ns"], "warmup_start")
    anchor = _ns_text(plan["anchor_start_utc_ns"], "anchor_start")
    purge = _ns_text(plan["purge_ns"], "purge")
    raw = plan["oos_boundaries_utc_ns"]
    if type(raw) is not list:
        raise WalkForwardError("invalid_plan")
    if len(raw) < MIN_FOLDS + 1:
        raise WalkForwardError("too_few_folds")
    bounds = tuple(_ns_text(b, "oos_boundary") for b in raw)
    if any(a >= b for a, b in zip(bounds, bounds[1:])):
        raise WalkForwardError("invalid_plan")
    if not warmup <= anchor < bounds[0] - purge:
        raise WalkForwardError("invalid_plan")
    period = plan["period"]
    if type(period) is not str or not period:
        raise WalkForwardError("invalid_plan")
    fee = _decimal(plan["fee_bps"], "fee_bps")
    slippage = _decimal(plan["slippage_bps"], "slippage_bps")
    if not (_ZERO <= fee <= _BPS and _ZERO <= slippage <= _BPS):
        raise WalkForwardError("invalid_cost_model")
    with localcontext(_ctx()):
        rate = (fee + slippage) / _BPS
    return _Plan(warmup, anchor, bounds, purge, period, rate)


def _overlaps(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start < other_end and other_start < end


def _feature_row(row: PanelRow) -> FeatureRow:
    return FeatureRow(row.row_id, row.decision_utc_ns, row.feature_available_utc_ns, row.features)


def _layout(panel: tuple[PanelRow, ...], plan: _Plan, holdout_start: int, holdout_end: int) -> _Layout:
    first, last = plan.warmup_start, plan.bounds[-1]
    used = tuple(r for r in panel if first <= r.decision_utc_ns < last)
    if _overlaps(first, last, holdout_start, holdout_end):
        raise WalkForwardError("holdout_overlap")
    for r in used:
        span_start = min(r.feature_available_utc_ns, r.revision_first_seen_utc_ns)
        if _overlaps(span_start, r.exposure_end_utc_ns, holdout_start, holdout_end):
            raise WalkForwardError("holdout_overlap")
    ordered = sorted(used, key=lambda r: (r.feature_available_utc_ns, r.decision_utc_ns))
    by_availability = tuple(_feature_row(r) for r in ordered)
    folds = []
    for start, end in zip(plan.bounds, plan.bounds[1:]):
        cutoff = start - plan.purge
        steps = tuple(r for r in used if start <= r.decision_utc_ns < end)
        if len(steps) < MIN_OOS_PER_FOLD:
            raise WalkForwardError("insufficient_oos_observations")
        for i, r in enumerate(steps):
            limit = steps[i + 1].decision_utc_ns if i + 1 < len(steps) else end
            if r.exposure_end_utc_ns > limit:
                raise WalkForwardError("overlapping_or_overrunning_exposure")
        labels = tuple(
            LabelRow(r.row_id, r.decision_utc_ns, r.exposure_end_utc_ns, r.excess_return)
            for r in used
            if r.decision_utc_ns >= plan.anchor_start and r.exposure_end_utc_ns <= cutoff
        )
        if not labels:
            raise WalkForwardError("empty_training")
        features = tuple(f for f in by_availability if f.feature_available_utc_ns <= cutoff)
        folds.append(_Fold(start, end, cutoff, features, labels, steps))
    used_hash = content_hash(_ROWS_DOMAIN, [{
        "row_id": r.row_id, "decision_utc_ns": str(r.decision_utc_ns),
        "feature_available_utc_ns": str(r.feature_available_utc_ns),
        "revision_first_seen_utc_ns": str(r.revision_first_seen_utc_ns),
        "exposure_end_utc_ns": str(r.exposure_end_utc_ns),
        "excess_return": r.excess_return, "features": [list(p) for p in r.features],
    } for r in used])
    index = content_hash(_INDEX_DOMAIN, [str(r.decision_utc_ns) for f in folds for r in f.steps])
    return _Layout(tuple(folds), by_availability, len(used), used_hash, index)


def _visible(layout: _Layout, fold: _Fold):
    """Yield (step, features available at the step's decision); never a later feature."""
    rows = layout.by_availability
    i = 0
    for step in fold.steps:
        while i < len(rows) and rows[i].feature_available_utc_ns <= step.decision_utc_ns:
            i += 1
        yield step, rows[:i]


def _stats(xs: list[Decimal]) -> dict:
    n = len(xs)
    mean = sum(xs) / n
    ss = sum((x - mean) ** 2 for x in xs)
    sharpe = None if ss == 0 else mean / (ss / (n - 1)).sqrt()
    growth = _ONE
    for x in xs:
        growth *= _ONE + x
    return {"mean": str(mean), "sharpe": None if sharpe is None else str(sharpe),
            "compounded_net_excess_return": str(growth - _ONE)}


def _metrics(config, plan: _Plan, layout: _Layout, prereg: str, holdout: tuple[int, int],
             source_sha256: str, positions: list[list[str]], states: list[str]) -> dict:
    if len(positions) != len(layout.folds) or len(states) != len(layout.folds):
        raise WalkForwardError("invalid_positions")
    folds, every_net = [], []
    with localcontext(_ctx()):
        total_turnover = total_cost = _ZERO
        for fold, fold_positions, state in zip(layout.folds, positions, states):
            if type(fold_positions) is not list or len(fold_positions) != len(fold.steps):
                raise WalkForwardError("invalid_positions")
            sha256_text(state)
            previous = _ZERO
            gross, costs, nets = [], [], []
            turnover_sum = cost_sum = _ZERO
            for i, (step, text) in enumerate(zip(fold.steps, fold_positions)):
                p = _position(text)
                turnover = abs(p - previous) + (abs(p) if i == len(fold.steps) - 1 else _ZERO)
                cost = turnover * plan.cost_rate
                g = p * Decimal(step.excess_return)
                net = g - cost
                if _ONE + net <= 0:
                    raise WalkForwardError("invalid_compounding")
                gross.append(g)
                costs.append(cost)
                nets.append(net)
                turnover_sum += turnover
                cost_sum += cost
                previous = p
            every_net += nets
            total_turnover += turnover_sum
            total_cost += cost_sum
            folds.append({
                "index": len(folds), "oos_start_utc_ns": str(fold.start), "oos_end_utc_ns": str(fold.end),
                "train_cutoff_utc_ns": str(fold.cutoff), "train_label_rows": len(fold.labels),
                "fit_feature_rows": len(fold.features), "oos_rows": len(fold.steps),
                "fit_state_sha256": state, "positions": list(fold_positions),
                "gross_excess_returns": [str(x) for x in gross], "costs": [str(x) for x in costs],
                "net_excess_returns": [str(x) for x in nets],
                "turnover": str(turnover_sum), "total_cost": str(cost_sum), **_stats(nets),
            })
        aggregate = {"oos_rows": len(every_net), "folds": len(folds),
                     "positive_folds": sum(1 for f in folds
                                           if Decimal(f["compounded_net_excess_return"]) > 0),
                     "turnover": str(total_turnover), "total_cost": str(total_cost), **_stats(every_net)}
    return {
        "schema": SCHEMA, "period": plan.period,
        "excess_returns": [n for f in folds for n in f["net_excess_returns"]],
        "observation_index_sha256": layout.oos_index_sha256,
        "config": config, "preregistration_sha256": prereg,
        "holdout": {"start_utc_ns": str(holdout[0]), "end_utc_ns": str(holdout[1])},
        "data": {"used_rows_sha256": layout.used_rows_sha256, "used_rows": layout.used_rows,
                 "source_sha256_declared": source_sha256,
                 "source_status": "UNANCHORED_DECLARED_NOT_ADMITTED"},
        "cost_rate": str(plan.cost_rate), "folds": folds, "aggregate": aggregate,
        "assumptions": list(ASSUMPTIONS), "limitations": list(LIMITATIONS),
        "permits_strategy_approval": False, "permits_promotion": False,
        "permits_paper_or_trading": False,
    }


def _inputs(panel, preregistration, manifest, source_sha256, parent_trial_id):
    """Validate everything that does not need the registry, before any attempt is allocated."""
    rows = _validated_panel(panel)
    prereg, start, end = _holdout(preregistration)
    try:
        approved = input_manifest(manifest, allowed_paths=tuple(manifest))
        sha256_text(source_sha256)
    except (CanonicalError, TypeError) as exc:
        raise WalkForwardError("invalid_manifest_or_source") from exc
    if parent_trial_id is not None and type(parent_trial_id) is not str:
        raise WalkForwardError("invalid_parent")
    return rows, prereg, (start, end), approved


class WalkForward:
    """One registered walk-forward run. Pass ``evaluate`` to research_path.run_registered_trial.

    fit(features, labels, config) -> canonical state (mapping/list/str/int)
    decide(state, now_utc_ns, features, config) -> position as a decimal string in [-1, 1]
    Each call receives fresh copies of the state and the frozen configuration.
    """

    def __init__(self, panel, *, preregistration, manifest, source_sha256, fit, decide,
                 parent_trial_id=None):
        if not callable(fit) or not callable(decide):
            raise WalkForwardError("fit_and_decide_required")
        self._rows, self._prereg, self._holdout, self._manifest = _inputs(
            panel, preregistration, manifest, source_sha256, parent_trial_id)
        self._source = source_sha256
        self._parent = parent_trial_id
        self._fit, self._decide = fit, decide

    def evaluate(self, receipt, config) -> dict:
        if type(receipt) is not TrialReceipt:
            raise WalkForwardError("registered_receipt_required")
        if _binding(receipt.study_id, self._prereg, config, self._manifest, self._parent) != receipt.binding_hash:
            raise WalkForwardError("holdout_unproven")
        plan = _plan(config)
        layout = _layout(self._rows, plan, *self._holdout)
        frozen = canonical_bytes(config).decode("utf-8")
        positions, states = [], []
        for fold in layout.folds:
            state = self._fit(fold.features, fold.labels, parse_json_strict(frozen))
            try:
                state_json = canonical_bytes(state).decode("utf-8")
            except CanonicalError as exc:
                raise WalkForwardError("noncanonical_fit_state") from exc
            states.append(content_hash(_STATE_DOMAIN, parse_json_strict(state_json)))
            fold_positions = []
            for step, visible in _visible(layout, fold):
                p = self._decide(parse_json_strict(state_json), step.decision_utc_ns, visible,
                                 parse_json_strict(frozen))
                _position(p)
                fold_positions.append(p)
            positions.append(fold_positions)
        return _metrics(parse_json_strict(frozen), plan, layout, self._prereg, self._holdout,
                        self._source, positions, states)


def verify_walk_forward(registry: TrialRegistry, result, *, study_id: str, panel, preregistration,
                        manifest, parent_trial_id=None) -> WalkForwardReport:
    """Re-derive a registered walk-forward result. Evidence problems return INSUFFICIENT_EVIDENCE."""
    if not isinstance(registry, TrialRegistry):
        raise RegistryError("a TrialRegistry is required")
    head = registry.verify().head_hash
    reasons: list[str] = []
    trial_id = record = metrics = None
    try:
        trial_id, rstudy, binding = result.trial_id, result.study_id, result.binding_hash
        metrics_json, output_hash = result.metrics_json, result.output_hash
        metrics = parse_json_strict(metrics_json)
        expected = content_hash(RESULT_DOMAIN, {
            "trial_id": trial_id, "study_id": rstudy, "binding_hash": binding, "metrics": metrics,
        })
        record = registry.trial_record(trial_id)
    except (AttributeError, CanonicalError, RegistryError, TypeError, ValueError):
        pass
    if registry.verify().head_hash != head:
        reasons.append("registry_changed_during_read")
    if (record is None or rstudy != study_id or record["study_id"] != study_id
            or record["status"] != "COMPLETED" or record["binding_hash"] != binding
            or output_hash != expected or record["output_hash"] != expected):
        reasons.append("result_without_registration_proof")
    evidence = (f"registry_head:{head}",)
    if not reasons:
        evidence += (f"trial:{trial_id}:{output_hash}",)
        try:
            rows, prereg, holdout, approved = _inputs(
                panel, preregistration, manifest, metrics["data"]["source_sha256_declared"],
                parent_trial_id)
            config = metrics["config"]
            if _binding(study_id, prereg, config, approved, parent_trial_id) != binding:
                reasons.append("binding_or_preregistration_mismatch")
            else:
                plan = _plan(config)
                layout = _layout(rows, plan, *holdout)
                rebuilt = _metrics(config, plan, layout, prereg, holdout,
                                   metrics["data"]["source_sha256_declared"],
                                   [f["positions"] for f in metrics["folds"]],
                                   [f["fit_state_sha256"] for f in metrics["folds"]])
                if canonical_bytes(rebuilt) != canonical_bytes(metrics):
                    reasons.append("metrics_not_reproducible")
                evidence += (f"preregistration:{prereg}", f"used_rows:{layout.used_rows_sha256}")
        except WalkForwardError as exc:
            reasons.append(str(exc).split(":")[0])
        except (CanonicalError, KeyError, TypeError):
            reasons.append("invalid_metrics")
    if reasons:
        return WalkForwardReport(INSUFFICIENT, tuple(dict.fromkeys(reasons)), study_id, trial_id,
                                 evidence)
    return WalkForwardReport(VALID, (), study_id, trial_id, evidence, metrics)
