"""Research-only *refusal* adapter. Stage2 cannot bind or allocate any trial.

A direct caller to the Stage1 registry bypasses this descriptive helper.
It supplies NO runtime execution/admission gate and no attestation verifier.
"""
from __future__ import annotations

from dataclasses import dataclass

from .canonical import content_hash
from .dataset_contracts import AdmissionReport, CausalStatus, DatasetError


@dataclass(frozen=True)
class BindingRefusal:
    study_id: str
    evidence_digest: str
    reason: str = "anchor_missing"
    permits_trial_allocation: bool = False
    permits_causal_use: bool = False
    direct_registry_bypass_uncontrolled: bool = True


def refuse_trial_binding(
    report: AdmissionReport, *, study_id: str,
    registry=None, external_claim=None,
) -> BindingRefusal:
    """Only describe refusal; never inspect/invoke provided opaque arguments.

    Every input, including forged or test-only self-reported trust, is refused
    identically. A production attestation parser/hook intentionally does not
    exist. The optional opaque argument is for inert isolation testing.
    """
    if not isinstance(report, AdmissionReport):
        raise DatasetError("a typed Stage2 report is required")
    if not isinstance(study_id, str) or not study_id or len(study_id)>256:
        raise DatasetError("invalid research study label")
    if report.causal_status is not CausalStatus.NOT_CAUSALLY_ELIGIBLE:
        raise DatasetError("unexpected dataset report")
    return BindingRefusal(
        study_id=study_id, evidence_digest=content_hash(
            "trial-binding-refusal-v1", {
                "study": study_id, "report": report.evidence_digest,
                "reason": "anchor_missing",
                "direct_registry_bypass_uncontrolled": True,
            }
        ),
    )
