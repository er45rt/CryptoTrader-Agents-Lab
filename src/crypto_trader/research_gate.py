"""P4-06 research strategy approval gate. Research approval only: it is not authorization for
any operational use, and every report carries operational_authorization "NONE".

The gate orchestrates the closed P4 components through their public interfaces and adds no
statistic or verification of its own:
* every COMPLETED walk-forward result is re-verified (walk_forward.verify_walk_forward), which
  also proves the preregistration document, and therefore its approval_policy, is the study's;
* DSR is recomputed for the selected trial (dsr.deflated_sharpe);
* PBO is recomputed over the verified OOS net excess returns of every COMPLETED trial
  (pbo.estimate_pbo, predeclared mean_return score, the policy's fixed slice count);
* the sealed holdout result is re-verified (holdout.verify_holdout) and must belong to the
  selected trial.
Missing, unverifiable or non-VALID evidence gives INSUFFICIENT_EVIDENCE; complete evidence is
then judged against the registered approval_policy, which must be at least as strict as the
project floor: RESEARCH_APPROVED or RESEARCH_REJECTED. The PBO slice count is never adjusted.
FAILED attempts have no return series, so they would be silently missing from the PBO family:
their presence gives INSUFFICIENT_EVIDENCE. The registry and holdout ledger heads must not change
during the gate. The report is deterministic and carries its own SHA256 for the evidence file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from crypto_trader.validation.canonical import CanonicalError, canonical_bytes, content_hash, parse_json_strict
from crypto_trader.validation.contracts import RegistryError
from crypto_trader.validation.dsr import deflated_sharpe
from crypto_trader.validation.holdout import HoldoutLedger, verify_holdout
from crypto_trader.validation.pbo import MEAN_RETURN_SCORE, CandidateReturns, PBOError, estimate_pbo
from crypto_trader.validation.registry import TrialRegistry
from crypto_trader.validation.walk_forward import verify_walk_forward

APPROVED = "RESEARCH_APPROVED"
REJECTED = "RESEARCH_REJECTED"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
VALID = "VALID_RESEARCH_RESULT"
LABEL = "RESEARCH_APPROVAL_ONLY"
OPERATIONAL_AUTHORIZATION = "NONE"
REPORT_DOMAIN = "research-gate-report-v1"
POLICY_KEY = "approval_policy"
_DECIMAL_KEYS = ("min_dsr", "max_pbo", "holdout_net_sharpe_above", "holdout_compounded_return_above")
_INT_KEYS = ("min_completed_trials", "pbo_slice_count")
_DEC = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")

# Project governance floor for version 1 (owner decision 2026-11-03): research choices for this
# project, not universal standards and no guarantee of future performance.
PROJECT_FLOOR = {
    "min_dsr": Decimal("0.95"), "max_pbo": Decimal("0.20"),
    "holdout_net_sharpe_above": Decimal(0), "holdout_compounded_return_above": Decimal(0),
    "min_completed_trials": 2, "pbo_slice_count": 8,
}

LIMITATIONS = (
    "research approval is not authorization: no promotion and no Paper, Testnet or Live use",
    "market data is not admitted (anchor_missing); registry and holdout ledger are local "
    "UNANCHORED evidence that an administrator can rewrite",
    "holdout blindness is not shown; single use holds within one ledger file; the history is "
    "unchanged since the holdout request only",
    "research done outside the registry, and callbacks reading closures or global state, are "
    "not covered",
    "thresholds are project governance choices, not universal standards or a performance "
    "guarantee",
    "the DSR value is a model-based probability estimate, not a confidence level",
    "PBO treats slices as exchangeable; serial dependence is not corrected",
    "DSR uses the counted attempts M; N_eff is not estimated",
    "costs are research assumptions, not observed execution prices",
)


class GateError(RegistryError):
    """Invalid gate input or report; the message is a fixed reason code."""


@dataclass(frozen=True)
class StrategyGateReport:
    decision: str
    reasons: tuple[str, ...]
    study_id: str
    selected_trial_id: object
    checks: tuple[dict, ...]
    policy: dict | None
    measures: dict
    evidence: dict
    report_sha256: str
    label: str = LABEL
    operational_authorization: str = OPERATIONAL_AUTHORIZATION
    permits_promotion: bool = False
    permits_paper_or_trading: bool = False
    limitations: tuple[str, ...] = LIMITATIONS

    def __post_init__(self):
        if (self.label != LABEL or self.operational_authorization != OPERATIONAL_AUTHORIZATION
                or self.permits_promotion is not False or self.permits_paper_or_trading is not False
                or self.decision not in (APPROVED, REJECTED, INSUFFICIENT)):
            raise GateError("a research gate report cannot carry any authority")


def parse_approval_policy(preregistration) -> dict:
    """The registered approval_policy, parsed and checked against the project floor."""
    try:
        copy = parse_json_strict(canonical_bytes(preregistration).decode("utf-8"))
    except (CanonicalError, TypeError) as exc:
        raise GateError("approval_policy_missing") from exc
    policy = copy.get(POLICY_KEY) if type(copy) is dict else None
    if policy is None:
        raise GateError("approval_policy_missing")
    if type(policy) is not dict or set(policy) != set(_DECIMAL_KEYS) | set(_INT_KEYS):
        raise GateError("invalid_approval_policy")
    parsed = {}
    for key in _DECIMAL_KEYS:
        if type(policy[key]) is not str or not _DEC.fullmatch(policy[key]):
            raise GateError("invalid_approval_policy")
        parsed[key] = Decimal(policy[key])
    for key in _INT_KEYS:
        if type(policy[key]) is not int:
            raise GateError("invalid_approval_policy")
        parsed[key] = policy[key]
    floor = PROJECT_FLOOR
    if not (floor["min_dsr"] <= parsed["min_dsr"] <= 1 and 0 <= parsed["max_pbo"] <= floor["max_pbo"]
            and parsed["holdout_net_sharpe_above"] >= floor["holdout_net_sharpe_above"]
            and parsed["holdout_compounded_return_above"] >= floor["holdout_compounded_return_above"]
            and parsed["min_completed_trials"] >= floor["min_completed_trials"]
            and parsed["pbo_slice_count"] == floor["pbo_slice_count"]):
        raise GateError("policy_below_project_floor")
    return parsed


def judge(policy: dict, *, dsr: Decimal, pbo: Decimal, holdout_sharpe, holdout_compounded: Decimal):
    """Compare complete evidence with the policy; a missing (zero-variance) Sharpe fails."""
    def check(criterion, value, threshold, comparison, passed):
        return {"criterion": criterion, "value": None if value is None else str(value),
                "threshold": str(threshold), "comparison": comparison, "passed": bool(passed)}
    return (
        check("dsr", dsr, policy["min_dsr"], ">=", dsr >= policy["min_dsr"]),
        check("pbo", pbo, policy["max_pbo"], "<=", pbo <= policy["max_pbo"]),
        check("holdout_net_sharpe", holdout_sharpe, policy["holdout_net_sharpe_above"], ">",
              holdout_sharpe is not None and holdout_sharpe > policy["holdout_net_sharpe_above"]),
        check("holdout_compounded_net_excess_return", holdout_compounded,
              policy["holdout_compounded_return_above"], ">",
              holdout_compounded > policy["holdout_compounded_return_above"]),
    )


def report_payload(report: StrategyGateReport) -> dict:
    """Canonical content of a report (everything except its own hash), for the evidence file."""
    return {
        "decision": report.decision, "reasons": list(report.reasons), "study_id": report.study_id,
        "selected_trial_id": report.selected_trial_id, "checks": [dict(c) for c in report.checks],
        "policy": report.policy, "measures": dict(report.measures), "evidence": dict(report.evidence),
        "label": report.label, "operational_authorization": report.operational_authorization,
        "permits_promotion": report.permits_promotion,
        "permits_paper_or_trading": report.permits_paper_or_trading,
        "limitations": list(report.limitations),
    }


def report_sha256(payload: dict) -> str:
    return content_hash(REPORT_DOMAIN, payload)


def _report(decision, reasons, study_id, selected, checks, policy, measures, evidence):
    draft = StrategyGateReport(decision, tuple(dict.fromkeys(reasons)), study_id, selected,
                               tuple(checks), policy, measures, evidence, "0" * 64)
    payload = report_payload(draft)
    return StrategyGateReport(draft.decision, draft.reasons, study_id, selected, draft.checks,
                              policy, measures, evidence, report_sha256(payload))


def _raw_policy(preregistration):
    try:
        copy = parse_json_strict(canonical_bytes(preregistration).decode("utf-8"))
    except (CanonicalError, TypeError):
        return None
    return copy.get(POLICY_KEY) if type(copy) is dict else None


def evaluate_strategy_gate(registry: TrialRegistry, ledger: HoldoutLedger, *, study_id: str,
                           selected_trial_id: str, results, holdout_report, preregistration,
                           manifest, panel, history, holdout_rows) -> StrategyGateReport:
    """Research approval decision for one selected trial of one registered study."""
    if not isinstance(registry, TrialRegistry) or not isinstance(ledger, HoldoutLedger):
        raise GateError("a TrialRegistry and a HoldoutLedger are required")
    registry_head, ledger_head = registry.verify().head_hash, ledger.head()
    reasons: list[str] = []
    measures: dict = {}
    policy = None
    try:
        policy = parse_approval_policy(preregistration)
    except GateError as exc:
        reasons.append(str(exc))

    # Evidence completeness: the registry decides what COMPLETED, FAILED and open mean.
    results = tuple(results)
    counts = registry.status_counts(study_id)
    if counts.get("FAILED"):
        reasons.append("failed_attempts_present")
    if counts.get("STARTED") or counts.get("ABORTED") or counts.get("INTERRUPTED_UNRESOLVED"):
        reasons.append("open_or_interrupted_attempts")
    completed = counts.get("COMPLETED", 0)
    measures["completed_trials"] = completed
    if policy is not None and completed < policy["min_completed_trials"]:
        reasons.append("too_few_completed_trials")
    if len(results) != completed:
        reasons.append("completed_results_missing_or_extra")
    verified = []
    for result in results:
        report = verify_walk_forward(registry, result, study_id=study_id, panel=panel,
                                     preregistration=preregistration, manifest=manifest)
        if report.status != VALID:
            reasons.append("walk_forward_not_valid:" + ",".join(report.reasons))
        verified.append(report)
    trial_ids = [report.trial_id for report in verified]
    if len(set(trial_ids)) != len(trial_ids):
        reasons.append("duplicate_result")
    selected = None
    for result in results:
        try:
            if result.trial_id == selected_trial_id:
                selected = result
        except AttributeError:
            continue
    if selected is None:
        reasons.append("selected_trial_not_in_results")

    # The sealed holdout must be the selected trial's, and must re-verify.
    holdout_verified = None
    try:
        holdout_candidate = holdout_report.metrics["candidate"]["trial_id"]
    except (AttributeError, KeyError, TypeError):
        holdout_candidate = None
    if holdout_candidate != selected_trial_id:
        reasons.append("selected_not_holdout_candidate")
    if selected is not None:
        holdout_verified = verify_holdout(ledger, registry, holdout_report, candidate=selected,
                                          preregistration=preregistration, manifest=manifest,
                                          history=history, holdout_rows=holdout_rows)
        if holdout_verified.status != VALID:
            reasons.append("holdout_not_valid:" + ",".join(holdout_verified.reasons))

    dsr_report = pbo_report = None
    if not reasons:
        dsr_report = deflated_sharpe(registry, study_id, results, selected_trial_id=selected_trial_id)
        if dsr_report.status != VALID:
            reasons.append("dsr_not_valid:" + ",".join(dsr_report.reasons))
        metrics = [report.metrics for report in verified]
        if len({m["observation_index_sha256"] for m in metrics}) != 1:
            reasons.append("unaligned_oos")
        else:
            bounds = metrics[0]["config"]["walk_forward"]["oos_boundaries_utc_ns"]
            first, last = int(bounds[0]), int(bounds[-1])
            stamps = tuple(row.decision_utc_ns for row in panel if first <= row.decision_utc_ns < last)
            if any(len(m["excess_returns"]) != len(stamps) for m in metrics):
                reasons.append("unaligned_oos")
            else:
                measures["oos_observations"] = len(stamps)
                candidates = tuple(
                    CandidateReturns(report.trial_id, tuple(Decimal(x) for x in report.metrics["excess_returns"]))
                    for report in verified)
                try:
                    pbo_report = estimate_pbo(stamps, candidates, slice_count=policy["pbo_slice_count"],
                                              search_family_size=len(candidates), score=MEAN_RETURN_SCORE)
                except PBOError as exc:
                    reasons.append(f"pbo_not_computable:{exc}")

    if registry.verify().head_hash != registry_head:
        reasons.append("registry_changed_during_read")
    if ledger.head() != ledger_head:
        reasons.append("ledger_changed_during_read")

    evidence = {
        "registry_head": registry_head, "ledger_head": ledger_head,
        "policy_sha256": content_hash(REPORT_DOMAIN + "-policy", _raw_policy(preregistration)),
        "trial_outputs": sorted(h for report in verified for h in report.evidence_hashes
                                if h.startswith("trial:")),
        "holdout_evidence": list(holdout_verified.evidence_hashes) if holdout_verified else [],
    }
    raw_policy = _raw_policy(preregistration) if policy is not None else None
    if reasons:
        return _report(INSUFFICIENT, reasons, study_id, selected_trial_id, (), raw_policy, measures, evidence)

    holdout_metrics = holdout_verified.metrics
    sharpe = holdout_metrics["sharpe"]
    measures.update(
        dsr=str(dsr_report.dsr), pbo=str(pbo_report.pbo),
        holdout_net_sharpe=sharpe,
        holdout_compounded_net_excess_return=holdout_metrics["compounded_net_excess_return"],
    )
    checks = judge(policy, dsr=dsr_report.dsr, pbo=pbo_report.pbo,
                   holdout_sharpe=None if sharpe is None else Decimal(sharpe),
                   holdout_compounded=Decimal(holdout_metrics["compounded_net_excess_return"]))
    failed = [f"criterion_failed:{c['criterion']}" for c in checks if not c["passed"]]
    return _report(REJECTED if failed else APPROVED, failed, study_id, selected_trial_id, checks,
                   raw_policy, measures, evidence)
