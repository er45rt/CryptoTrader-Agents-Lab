"""P4-02: approved research path registers before evaluation and admits only proven results."""
from __future__ import annotations

from dataclasses import replace

import pytest

from crypto_trader.validation.contracts import RegistryError, StudySpec
from crypto_trader.validation.registry import TrialRegistry
from crypto_trader.validation.research_path import (
    TrialRejected, admit_result, run_registered_trial, study_accounting,
)

STUDY = "11111111-1111-4111-8111-111111111111"
CONFIG = {"lookback": 5}
MANIFEST = {"src/crypto_trader/paper/session.py": "b" * 64}


def _registry(tmp_path, *, capacity=5, name="trials.sqlite"):
    reg = TrialRegistry(tmp_path / name)
    reg.register_study(StudySpec(
        study_id=STUDY, preregistration_hash="a" * 64, n_max=capacity,
        allowed_paths=("src/crypto_trader/paper/session.py",), cutoff="UNKNOWN",
    ))
    return reg


def _ok(receipt, config):
    return {"sharpe": "0.42", "trial": receipt.trial_id}


def _fail(receipt, config):
    raise ValueError("bad fold")


def _trial_ids(reg):
    return [row["trial_id"] for row in reg._db.execute("SELECT trial_id FROM attempts ORDER BY rowid")]


def test_registration_precedes_evaluation_and_completed_result_is_admitted(tmp_path):
    with _registry(tmp_path) as reg:
        seen = []

        def evaluate(receipt, config):
            seen.append(reg.get_status(receipt.trial_id))
            return _ok(receipt, config)

        result = run_registered_trial(reg, STUDY, CONFIG, MANIFEST, evaluate)
        assert seen == ["STARTED"], "the attempt is durable before the evaluator runs"
        assert reg.get_status(result.trial_id) == "COMPLETED"
        assert admit_result(reg, result, study_id=STUDY) is result
        assert result.metrics["sharpe"] == "0.42"
        assert study_accounting(reg, STUDY)["attempts_total"] == 1


def test_failed_evaluation_is_recorded_counted_and_yields_no_result(tmp_path):
    with _registry(tmp_path) as reg:
        with pytest.raises(ValueError):
            run_registered_trial(reg, STUDY, CONFIG, MANIFEST, _fail)
        acc = study_accounting(reg, STUDY)
        assert acc["by_status"] == {"FAILED": 1} and acc["attempts_total"] == 1


def test_interrupted_evaluation_is_recorded_as_aborted(tmp_path):
    with _registry(tmp_path) as reg:
        def interrupted(receipt, config):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            run_registered_trial(reg, STUDY, CONFIG, MANIFEST, interrupted)
        assert study_accounting(reg, STUDY)["by_status"] == {"ABORTED": 1}


def test_invalid_metrics_fail_the_attempt(tmp_path):
    with _registry(tmp_path) as reg:
        with pytest.raises(Exception):
            run_registered_trial(reg, STUDY, CONFIG, MANIFEST, lambda r, c: {"sharpe": float("nan")})
        assert study_accounting(reg, STUDY)["by_status"] == {"FAILED": 1}


def test_precheck_refusal_happens_before_evaluation_and_stays_counted(tmp_path):
    with _registry(tmp_path) as reg:
        calls = []
        with pytest.raises(TrialRejected):
            run_registered_trial(reg, STUDY, CONFIG, MANIFEST,
                                 lambda r, c: calls.append(1) or _ok(r, c),
                                 precheck=lambda config: "lookback too short")
        assert calls == [], "the evaluator never ran"
        acc = study_accounting(reg, STUDY)
        assert acc["precheck_rejections"] == 1 and acc["attempts_total"] == 1


def test_capacity_refusal_never_invokes_the_evaluator(tmp_path):
    with _registry(tmp_path, capacity=1) as reg:
        run_registered_trial(reg, STUDY, CONFIG, MANIFEST, _ok)
        calls = []
        with pytest.raises(RegistryError):
            run_registered_trial(reg, STUDY, CONFIG, MANIFEST,
                                 lambda r, c: calls.append(1) or _ok(r, c))
        assert calls == []


def test_results_without_registration_proof_are_refused(tmp_path):
    with _registry(tmp_path) as reg, _registry(tmp_path, name="other.sqlite") as other:
        good = run_registered_trial(reg, STUDY, CONFIG, MANIFEST, _ok)
        forged = [
            {"trial_id": good.trial_id},  # not an AdmissibleResult
            replace(good, metrics_json='{"sharpe":"9.99","trial":"' + good.trial_id + '"}'),
            replace(good, binding_hash="c" * 64),
            replace(good, output_hash="d" * 64),
        ]
        for candidate in forged:
            with pytest.raises(RegistryError, match="no registration proof"):
                admit_result(reg, candidate, study_id=STUDY)
        with pytest.raises(RegistryError, match="no registration proof"):
            admit_result(other, good, study_id=STUDY)  # proof lives in another registry only
        with pytest.raises(RegistryError, match="no registration proof"):
            admit_result(reg, good, study_id="22222222-2222-4222-8222-222222222222")


def test_result_of_a_non_completed_trial_is_refused(tmp_path):
    with _registry(tmp_path) as reg:
        good = run_registered_trial(reg, STUDY, CONFIG, MANIFEST, _ok)
        receipt = reg.allocate(STUDY, CONFIG, MANIFEST)
        reg.finish(receipt.trial_id, "FAILED", reason="manual")
        stolen = replace(good, trial_id=receipt.trial_id, binding_hash=receipt.binding_hash)
        with pytest.raises(RegistryError, match="no registration proof"):
            admit_result(reg, stolen, study_id=STUDY)


def test_crash_before_terminal_leaves_counted_open_attempt_until_recovery(tmp_path, monkeypatch):
    path = tmp_path / "trials.sqlite"
    reg = _registry(tmp_path)

    def crash(*args, **kwargs):
        raise RuntimeError("process died before the terminal write")

    monkeypatch.setattr(reg, "finish", crash)
    with pytest.raises(RuntimeError):
        run_registered_trial(reg, STUDY, CONFIG, MANIFEST, _ok)
    reg.close()

    with TrialRegistry(path) as reopened:
        acc = study_accounting(reopened, STUDY)
        assert acc["open_attempts"] == 1 and acc["history_status"] == "OPEN_ATTEMPTS"
        assert acc["history_complete"] is False
        assert reopened.recover_interrupted(operator_confirms_no_active_writer=True) == 1
        acc = study_accounting(reopened, STUDY)
        assert acc["by_status"] == {"INTERRUPTED_UNRESOLVED": 1} and acc["attempts_total"] == 1
        assert acc["history_status"] == "UNANCHORED" and acc["history_complete"] is False


def test_retries_and_amendments_never_lower_the_historical_count(tmp_path):
    with _registry(tmp_path) as reg:
        with pytest.raises(ValueError):
            run_registered_trial(reg, STUDY, CONFIG, MANIFEST, _fail)
        assert reg.status_counts(STUDY) == {"FAILED": 1}
        failed_id = _trial_ids(reg)[0]
        retry = run_registered_trial(reg, STUDY, CONFIG, MANIFEST, _ok, parent_trial_id=failed_id)
        reg.amend_study(STUDY, "tighten lookback after review")
        acc = study_accounting(reg, STUDY)
        assert acc["attempts_total"] == 2
        assert acc["by_status"] == {"COMPLETED": 1, "FAILED": 1}
        assert reg.trial_record(failed_id)["status"] == "FAILED"
        assert admit_result(reg, retry, study_id=STUDY) is retry


def test_research_path_imports_no_execution_surface():
    import crypto_trader.validation.research_path as module
    source = open(module.__file__, encoding="utf-8").read()
    for forbidden in ("crypto_trader.execution", "crypto_trader.paper", "crypto_trader.production",
                      "crypto_trader.breakglass", "crypto_trader.mainnet", "binance"):
        assert forbidden not in source
