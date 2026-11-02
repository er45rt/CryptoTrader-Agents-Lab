"""P4-02 approved research path: register BEFORE evaluation, admit only proven results.

Research-only. This module grants no Paper, Testnet, Live, allocation or promotion
permission and imports nothing from the trading, venue or operator modules.

Completeness guarantee, and its limits:
* Every evaluation run through ``run_registered_trial`` is durably allocated in the
  TrialRegistry before the evaluator is invoked, and always reaches a terminal status
  (COMPLETED / FAILED / ABORTED / REJECTED_PRECHECK) unless the process dies, in which
  case it stays STARTED and later becomes INTERRUPTED_UNRESOLVED via explicit recovery.
  Attempts are never deleted, so retries, failures and aborts never lower the count.
* ``admit_result`` accepts only an ``AdmissibleResult`` whose trial is COMPLETED in the
  registry with the identical binding and output hash. A result without that proof is
  refused.
* NOT guaranteed: research done outside this path, or direct calls to the registry
  (e.g. ``finish(..., "REJECTED_PRECHECK")`` after looking at performance). The registry
  is UNANCHORED local evidence; history is never declared complete or anchored.
"""
from __future__ import annotations

from typing import Callable, Mapping
from dataclasses import dataclass

from .canonical import CanonicalError, canonical_bytes, content_hash, parse_json_strict
from .contracts import RegistryError, TrialReceipt
from .registry import TrialRegistry

_RESULT_DOMAIN = "research-result-v1"


class TrialRejected(RegistryError):
    """The precheck refused the attempt before any evaluation. The attempt is counted."""


@dataclass(frozen=True)
class AdmissibleResult:
    trial_id: str
    study_id: str
    binding_hash: str
    metrics_json: str
    output_hash: str

    @property
    def metrics(self) -> dict:
        return parse_json_strict(self.metrics_json)


def _output_hash(trial_id: str, study_id: str, binding_hash: str, metrics_json: str) -> str:
    return content_hash(_RESULT_DOMAIN, {
        "trial_id": trial_id, "study_id": study_id,
        "binding_hash": binding_hash, "metrics": parse_json_strict(metrics_json),
    })


def run_registered_trial(
    registry: TrialRegistry,
    study_id: str,
    config: Mapping[str, object],
    manifest: Mapping[str, str],
    evaluate: Callable[[TrialReceipt, Mapping[str, object]], Mapping[str, object]],
    *,
    precheck: Callable[[Mapping[str, object]], str | None] | None = None,
    parent_trial_id: str | None = None,
) -> AdmissibleResult:
    """Allocate first, then evaluate; every outcome is recorded. Refusal raises before evaluation."""
    if not isinstance(registry, TrialRegistry) or not callable(evaluate):
        raise RegistryError("approved research path requires a TrialRegistry and an evaluator")
    if not isinstance(config, Mapping):
        raise RegistryError("configuration must be a mapping")
    # Freeze the configuration once. The registry binds these exact bytes, and the
    # precheck and evaluator each get an independent deep copy decoded from them,
    # so nothing can change what is evaluated after registration.
    frozen = canonical_bytes(dict(config)).decode("utf-8")
    receipt = registry.allocate(study_id, parse_json_strict(frozen), manifest,
                                parent_trial_id=parent_trial_id)

    # Terminal reasons are fixed literals: caller text never reaches the registry,
    # so a reason can never be refused after the attempt was allocated.
    if precheck is not None:
        # The precheck sees only a configuration copy: no receipt, seed, data or metrics.
        try:
            refusal = precheck(parse_json_strict(frozen))
        except Exception:
            registry.finish(receipt.trial_id, "FAILED", reason="precheck_error")
            raise
        except BaseException:
            registry.finish(receipt.trial_id, "ABORTED", reason="precheck_interrupted")
            raise
        if refusal is not None:
            registry.finish(receipt.trial_id, "REJECTED_PRECHECK", reason="precheck_refused")
            raise TrialRejected("precheck refused the attempt before evaluation")

    try:
        metrics = evaluate(receipt, parse_json_strict(frozen))
        if not isinstance(metrics, Mapping) or not metrics:
            raise RegistryError("evaluator must return nonempty metrics")
        metrics_json = canonical_bytes(dict(metrics)).decode("utf-8")
        output_hash = _output_hash(receipt.trial_id, receipt.study_id, receipt.binding_hash, metrics_json)
    except Exception:
        registry.finish(receipt.trial_id, "FAILED", reason="evaluation_error")
        raise
    except BaseException:
        registry.finish(receipt.trial_id, "ABORTED", reason="evaluation_interrupted")
        raise

    registry.finish(receipt.trial_id, "COMPLETED", output_hash=output_hash)
    return AdmissibleResult(receipt.trial_id, receipt.study_id, receipt.binding_hash,
                            metrics_json, output_hash)


def admit_result(registry: TrialRegistry, result: object, *, study_id: str) -> AdmissibleResult:
    """Accept a result only with registration proof; otherwise refuse (fail closed)."""
    if not isinstance(registry, TrialRegistry) or type(result) is not AdmissibleResult:
        raise RegistryError("result has no registration proof")
    try:
        expected = _output_hash(result.trial_id, result.study_id, result.binding_hash, result.metrics_json)
        record = registry.trial_record(result.trial_id)
    except (CanonicalError, RegistryError, TypeError) as exc:
        raise RegistryError("result has no registration proof") from exc
    if (result.study_id != study_id or record["study_id"] != study_id
            or record["status"] != "COMPLETED"
            or record["binding_hash"] != result.binding_hash
            or result.output_hash != expected or record["output_hash"] != expected):
        raise RegistryError("result has no registration proof")
    return result


def study_accounting(registry: TrialRegistry, study_id: str) -> dict:
    """Attempt counts for multiple-testing accounting. Never decreases; never 'complete'."""
    by_status = registry.status_counts(study_id)
    total = sum(by_status.values())
    open_attempts = by_status.get("STARTED", 0)
    return {
        "study_id": study_id,
        "attempts_total": total,
        "by_status": by_status,
        "precheck_rejections": by_status.get("REJECTED_PRECHECK", 0),
        "completed": by_status.get("COMPLETED", 0),
        "open_attempts": open_attempts,
        "history_status": "OPEN_ATTEMPTS" if open_attempts else "UNANCHORED",
        "history_complete": False,
    }
