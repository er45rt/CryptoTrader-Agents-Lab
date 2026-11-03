"""P5-01 synthetic end-to-end research dry run. Research only: synthetic data, no Paper, Testnet
or Live use, and no authorization of any kind (operational_authorization "NONE").

One study runs the closed P4 pipeline end to end through its public interfaces:
preregistration (generator, family, selection rule, walk-forward plan, holdout window and row
commitment, approval_policy) -> every family member as one registered walk-forward attempt ->
re-verification of every result -> selection by the registered rule from verified results only ->
sealed single-use holdout of the selected trial -> research gate. The gate does not check that
the candidate is the rule's choice; this module does, here and in reverify_study.

Controls (owner decision 2026-11-03): selection uses verified walk-forward results only, before
the holdout is opened, with a registered tie-break; re-runs are compared on results and
decisions, not on event identities, times or hashes that depend on them; no outcome is assumed
for the noise scenario and nothing is tuned to it; the holdout grant confirmation exists only
inside this synthetic study and is no general permission.

Limits: the data are synthetic and prove nothing about any market; the registry manifest digest
is a declared placeholder, so code identity is the commit tree, not the manifest.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from crypto_trader.research_gate import StrategyGateReport, evaluate_strategy_gate
from crypto_trader.validation.canonical import content_hash
from crypto_trader.validation.contracts import RegistryError, StudySpec
from crypto_trader.validation.holdout import (
    HoldoutLedger, evaluate_holdout, grant, holdout_rows_sha256, request_access,
)
from crypto_trader.validation.holdout import PanelRow as HoldoutRow
from crypto_trader.validation.registry import TrialRegistry
from crypto_trader.validation.research_path import run_registered_trial
from crypto_trader.validation.walk_forward import (
    PREREGISTRATION_DOMAIN, WalkForward, verify_walk_forward,
)
from crypto_trader.validation.walk_forward import PanelRow as WalkForwardRow

LABEL = "SYNTHETIC_DRY_RUN"
OPERATIONAL_AUTHORIZATION = "NONE"
VALID = "VALID_RESEARCH_RESULT"
STUDY_ID = "50000000-0000-4000-8000-000000000001"
MANIFEST = {"src/crypto_trader/research_study.py": "0" * 64}
SOURCE_DOMAIN = "p5-synthetic-source-v1"
DAY_NS = 86_400 * 10**9
BASE_NS = 1_735_689_600 * 10**9          # 2025-01-01T00:00:00Z
ROWS = 232
HOLDOUT_DAYS = (200, 232)
PLAN_DAYS = {"warmup": 0, "anchor": 10, "boundaries": (100, 132, 164, 196)}   # 3 x 32 OOS rows

FAMILY = ("buy_and_hold", "momentum_long_flat", "momentum_long_short", "contrarian_long_flat")
SELECTION_RULE = {
    "metric": "walk_forward_aggregate_net_sharpe", "order": "highest",
    "tie_break": "earliest_in_family_order", "missing_sharpe": "ranked_last",
}
SCENARIOS = {
    "signal": {"seed": 5101, "drift": "0.006", "volatility": "0.01"},
    "noise": {"seed": 5102, "drift": "0", "volatility": "0.01"},
}
APPROVAL_POLICY_V1 = {
    "min_dsr": "0.95", "max_pbo": "0.20", "holdout_net_sharpe_above": "0",
    "holdout_compounded_return_above": "0", "min_completed_trials": 2, "pbo_slice_count": 8,
}


class StudyError(RegistryError):
    """The study stopped; the message starts with a fixed reason code."""


def _signal(features) -> Decimal:
    current = max(features, key=lambda f: f.decision_utc_ns)
    return Decimal(dict(current.features)["signal"])


def fit(features, labels, config):
    """Deterministic and parameter-free: every family member is a fixed rule."""
    return {"label_rows": len(labels)}


STRATEGIES = {
    "buy_and_hold": lambda state, now, features, config: "1",
    "momentum_long_flat": lambda state, now, features, config: "1" if _signal(features) > 0 else "0",
    "momentum_long_short": lambda state, now, features, config: "1" if _signal(features) > 0 else "-1",
    "contrarian_long_flat": lambda state, now, features, config: "1" if _signal(features) < 0 else "0",
}


@dataclass(frozen=True)
class SyntheticRows:
    walk_forward: tuple
    history: tuple
    holdout: tuple


@dataclass(frozen=True)
class StudyCheck:
    selection_ok: bool
    reasons: tuple
    gate_report: StrategyGateReport


@dataclass(frozen=True)
class StudyOutcome:
    scenario: str
    workdir: Path
    preregistration: dict
    ranking: tuple
    selected_name: str
    selected_trial_id: str
    results: tuple
    holdout_report: object
    gate_report: StrategyGateReport
    evidence: dict
    ledger_events: tuple
    trials_verified_before_holdout: int
    label: str = LABEL
    operational_authorization: str = OPERATIONAL_AUTHORIZATION
    permits_promotion: bool = False
    permits_paper_or_trading: bool = False

    def __post_init__(self):
        if (self.label != LABEL or self.operational_authorization != OPERATIONAL_AUTHORIZATION
                or self.permits_promotion is not False or self.permits_paper_or_trading is not False):
            raise StudyError("a synthetic study outcome cannot carry any authority")


def _day(i: int) -> int:
    return BASE_NS + i * DAY_NS


def _plan() -> dict:
    return {
        "warmup_start_utc_ns": str(_day(PLAN_DAYS["warmup"])),
        "anchor_start_utc_ns": str(_day(PLAN_DAYS["anchor"])),
        "oos_boundaries_utc_ns": [str(_day(d)) for d in PLAN_DAYS["boundaries"]],
        "purge_ns": str(DAY_NS), "period": "1d", "fee_bps": "10", "slippage_bps": "5",
    }


def _generator(scenario: str) -> dict:
    spec = SCENARIOS[scenario]
    return {"kind": "gaussian_daily_excess_returns", "seed": spec["seed"], "drift": spec["drift"],
            "volatility": spec["volatility"], "rows": ROWS, "decimals": 8}


def generate_rows(preregistration: dict) -> SyntheticRows:
    """Rebuild the synthetic rows from the registered generator; feature = previous return."""
    spec = preregistration["generator"]
    rng = random.Random(spec["seed"])
    returns = [f"{rng.gauss(float(spec['drift']), float(spec['volatility'])):.8f}" for _ in range(spec["rows"])]
    def rows(cls):
        return tuple(cls(
            row_id=f"r{i:05d}", decision_utc_ns=_day(i), feature_available_utc_ns=_day(i),
            revision_first_seen_utc_ns=_day(i), exposure_end_utc_ns=_day(i + 1),
            excess_return=returns[i], features=(("signal", returns[i - 1] if i else "0"),),
        ) for i in range(spec["rows"]))
    start, end = HOLDOUT_DAYS
    holdout_rows = rows(HoldoutRow)
    return SyntheticRows(rows(WalkForwardRow)[:start], holdout_rows[:start], holdout_rows[start:end])


def _preregistration(scenario: str) -> dict:
    doc = {
        "study": "P5-01 synthetic end-to-end dry run", "label": LABEL,
        "generator": _generator(scenario), "family": list(FAMILY), "selection_rule": dict(SELECTION_RULE),
        "walk_forward_plan": _plan(), "approval_policy": dict(APPROVAL_POLICY_V1),
        "holdout": {"start_utc_ns": str(_day(HOLDOUT_DAYS[0])), "end_utc_ns": str(_day(HOLDOUT_DAYS[1]))},
    }
    held = generate_rows(doc).holdout
    doc["holdout_commitment"] = {"rows_sha256": holdout_rows_sha256(held), "row_count": len(held)}
    return doc


def select_by_rule(rule: dict, ranking) -> dict:
    """Highest Sharpe; a tie keeps the earliest in family order; a missing Sharpe ranks last."""
    if rule != SELECTION_RULE or not ranking:
        raise StudyError("selection_rule_unsupported")
    best = None
    for entry in ranking:
        if entry["sharpe"] is None:
            continue
        if best is None or Decimal(entry["sharpe"]) > Decimal(best["sharpe"]):
            best = entry
    return best if best is not None else ranking[0]


def _ranking(registry, results, preregistration, panel):
    """Family-ordered ranking from VERIFIED walk-forward results only, or None if incomplete."""
    by_name = {}
    for result in results:
        report = verify_walk_forward(registry, result, study_id=STUDY_ID, panel=panel,
                                     preregistration=preregistration, manifest=MANIFEST)
        if report.status != VALID:
            return None
        name = report.metrics["config"]["strategy"]["name"]
        if name in by_name:
            return None
        by_name[name] = {"name": name, "trial_id": report.trial_id,
                         "sharpe": report.metrics["aggregate"]["sharpe"]}
    if set(by_name) != set(preregistration["family"]) or len(by_name) != len(preregistration["family"]):
        return None
    return tuple(by_name[name] for name in preregistration["family"])


def run_synthetic_study(workdir, *, scenario: str, confirm_synthetic_holdout) -> StudyOutcome:
    """Run one synthetic study in a NEW directory. The holdout confirmation is synthetic-only."""
    if scenario not in SCENARIOS:
        raise StudyError("unknown_scenario")
    if confirm_synthetic_holdout is not True:
        raise StudyError("synthetic_holdout_not_confirmed")
    workdir = Path(workdir)
    if workdir.exists():
        raise StudyError("workdir_not_new")
    workdir.mkdir(parents=True)
    doc = _preregistration(scenario)
    rows = generate_rows(doc)
    source = content_hash(SOURCE_DOMAIN, doc["generator"])
    with TrialRegistry(workdir / "trials.sqlite") as registry, HoldoutLedger(workdir / "holdout.sqlite") as ledger:
        registry.register_study(StudySpec(
            study_id=STUDY_ID, preregistration_hash=content_hash(PREREGISTRATION_DOMAIN, doc),
            n_max=len(FAMILY), allowed_paths=tuple(MANIFEST)))
        results = []
        for index, name in enumerate(FAMILY):
            runner = WalkForward(rows.walk_forward, preregistration=doc, manifest=MANIFEST,
                                 source_sha256=source, fit=fit, decide=STRATEGIES[name])
            results.append(run_registered_trial(
                registry, STUDY_ID, {"strategy": {"name": name, "family_index": index},
                                     "walk_forward": doc["walk_forward_plan"]},
                MANIFEST, runner.evaluate))
        results = tuple(results)

        # Selection from verified results only, before the holdout is opened.
        ranking = _ranking(registry, results, doc, rows.walk_forward)
        if ranking is None:
            raise StudyError("walk_forward_not_valid")
        chosen = select_by_rule(doc["selection_rule"], ranking)
        selected = next(r for r in results if r.trial_id == chosen["trial_id"])

        request = request_access(ledger, registry, study_id=STUDY_ID, candidate=selected,
                                 preregistration=doc, manifest=MANIFEST, history=rows.history)
        grant(ledger, request, owner_confirms=confirm_synthetic_holdout)
        holdout_report = evaluate_holdout(
            ledger, registry, request, candidate=selected, preregistration=doc, manifest=MANIFEST,
            history=rows.history, holdout_rows=rows.holdout, fit=fit, decide=STRATEGIES[chosen["name"]])
        report = evaluate_strategy_gate(
            registry, ledger, study_id=STUDY_ID, selected_trial_id=chosen["trial_id"], results=results,
            holdout_report=holdout_report, preregistration=doc, manifest=MANIFEST,
            panel=rows.walk_forward, history=rows.history, holdout_rows=rows.holdout)
        used_rows = next((h for h in report.evidence["trial_outputs"] if chosen["trial_id"] in h), None)
        evidence = {
            "preregistration_sha256": content_hash(PREREGISTRATION_DOMAIN, doc), "source_sha256": source,
            "used_rows_sha256": selected.metrics["data"]["used_rows_sha256"], "selected_output": used_rows,
            "holdout_result": holdout_report.evidence_hashes[0],
            "gate_report_sha256": report.report_sha256,
            "registry_head": registry.verify().head_hash, "ledger_head": ledger.head(),
        }
        events = tuple(ledger.events(STUDY_ID))
    return StudyOutcome(scenario, workdir, doc, ranking, chosen["name"], chosen["trial_id"], results,
                        holdout_report, report, evidence, events, len(ranking))


def reverify_study(outcome: StudyOutcome, *, preregistration=None, results=None) -> StudyCheck:
    """Re-derive selection and gate from the persisted registers and the registered generator."""
    doc = outcome.preregistration if preregistration is None else preregistration
    results = outcome.results if results is None else tuple(results)
    rows = generate_rows(doc)
    with TrialRegistry(outcome.workdir / "trials.sqlite") as registry, \
            HoldoutLedger(outcome.workdir / "holdout.sqlite") as ledger:
        ranking = _ranking(registry, results, doc, rows.walk_forward)
        selection_ok = (ranking is not None and doc.get("selection_rule") == SELECTION_RULE
                        and select_by_rule(doc["selection_rule"], ranking)["trial_id"] == outcome.selected_trial_id)
        report = evaluate_strategy_gate(
            registry, ledger, study_id=STUDY_ID, selected_trial_id=outcome.selected_trial_id,
            results=results, holdout_report=outcome.holdout_report, preregistration=doc,
            manifest=MANIFEST, panel=rows.walk_forward, history=rows.history, holdout_rows=rows.holdout)
    return StudyCheck(selection_ok, () if selection_ok else ("selection_not_per_rule",), report)


def comparable(outcome: StudyOutcome) -> dict:
    """Results and decisions only: no trial or event identities, times or hashes depending on them."""
    gate = outcome.gate_report
    held = outcome.holdout_report.metrics
    return {
        "scenario": outcome.scenario, "decision": gate.decision, "reasons": list(gate.reasons),
        "measures": dict(gate.measures), "checks": [dict(c) for c in gate.checks], "policy": gate.policy,
        "selected_name": outcome.selected_name,
        "ranking": [(r["name"], r["sharpe"]) for r in outcome.ranking],
        "holdout": {"net_sharpe": held["sharpe"], "compounded": held["compounded_net_excess_return"],
                    "positions": list(held["positions"])},
    }
