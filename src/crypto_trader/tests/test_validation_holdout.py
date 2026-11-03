"""P4-05: research-only sealed single-use holdout (ledger, gate and one causal evaluation)."""
from __future__ import annotations

import ast
import dataclasses
import json
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from dataclasses import replace

import pytest

from crypto_trader.validation import holdout as ho
from crypto_trader.validation import walk_forward as wf
from crypto_trader.validation.canonical import content_hash
from crypto_trader.validation.contracts import RegistryError, StudySpec
from crypto_trader.validation.registry import TrialRegistry
from crypto_trader.validation.research_path import run_registered_trial

STUDY = "11111111-1111-4111-8111-111111111111"
PATHS = ("src/crypto_trader/paper/session.py",)
MANIFEST = {PATHS[0]: "b" * 64}
DAY = 86_400 * 10**9
BASE = 1_735_689_600 * 10**9  # 2025-01-01T00:00:00Z
HOLD = (160, 190)             # holdout window in days, after the candidate's plan (ends day 130)


def day(i):
    return BASE + i * DAY


def make_rows(cls, n=200, seed=5, mean=0.0005):
    """Daily rows; the feature of row i is the realized return of row i-1."""
    rng = random.Random(seed)
    returns = [f"{rng.gauss(mean, 0.01):.8f}" for _ in range(n)]
    return tuple(cls(
        row_id=f"r{i:05d}", decision_utc_ns=day(i), feature_available_utc_ns=day(i),
        revision_first_seen_utc_ns=day(i), exposure_end_utc_ns=day(i + 1),
        excess_return=returns[i], features=(("signal", returns[i - 1] if i else "0"),),
    ) for i in range(n))


ROWS = make_rows(ho.PanelRow)
HISTORY = ROWS[:HOLD[0]]
HOLDOUT = ROWS[HOLD[0]:HOLD[1]]


def make_plan(bounds=(40, 70, 100, 130)):
    return {
        "warmup_start_utc_ns": str(day(0)), "anchor_start_utc_ns": str(day(10)),
        "oos_boundaries_utc_ns": [str(day(d)) for d in bounds], "purge_ns": str(DAY),
        "period": "1d", "fee_bps": "10", "slippage_bps": "5",
    }


def make_doc(holdout_rows=HOLDOUT, window=HOLD, commitment=True):
    doc = {"study": "p4-05 synthetic", "holdout": {
        "start_utc_ns": str(day(window[0])), "end_utc_ns": str(day(window[1]))}}
    if commitment:
        doc["holdout_commitment"] = {"rows_sha256": ho.holdout_rows_sha256(holdout_rows),
                                     "row_count": len(holdout_rows)}
    return doc


DOC = make_doc()


def config(name="sign-momentum", plan=None):
    return {"strategy": {"name": name}, "walk_forward": plan or make_plan()}


def fit_mean(features, labels, cfg):
    total = sum(float(x.excess_return) for x in labels)
    return {"mean": f"{total / len(labels):.10f}"}


def decide_sign(state, now_utc_ns, features, cfg):
    current = max(features, key=lambda f: f.decision_utc_ns)
    return "1" if float(dict(current.features)["signal"]) > 0 else "0"


def wf_rows(n=HOLD[0]):
    return make_rows(wf.PanelRow)[:n]


class Setup:
    """A study with one registered walk-forward candidate and an empty holdout ledger."""

    def __init__(self, path, doc=DOC, name="sign-momentum"):
        self.path = path
        self.doc = doc
        self.registry = TrialRegistry(path / "trials.sqlite")
        self.registry.register_study(StudySpec(
            study_id=STUDY, preregistration_hash=content_hash(ho.PREREGISTRATION_DOMAIN, doc),
            n_max=20, allowed_paths=PATHS))
        self.candidate = self.walk_forward(name)
        self.ledger = ho.HoldoutLedger(path / "holdout.sqlite")

    def walk_forward(self, name):
        runner = wf.WalkForward(wf_rows(), preregistration=self.doc, manifest=MANIFEST,
                                source_sha256="c" * 64, fit=fit_mean, decide=decide_sign)
        return run_registered_trial(self.registry, STUDY, config(name), MANIFEST, runner.evaluate)

    def request(self, candidate=None, ledger=None, history=HISTORY):
        return ho.request_access(ledger or self.ledger, self.registry, study_id=STUDY,
                                 candidate=candidate or self.candidate,
                                 preregistration=self.doc, manifest=MANIFEST, history=history)

    def granted(self):
        rid = self.request()
        ho.grant(self.ledger, rid, owner_confirms=True)
        return rid

    def evaluate(self, rid, *, candidate=None, history=HISTORY, holdout=HOLDOUT,
                 fit=fit_mean, decide=decide_sign, ledger=None):
        return ho.evaluate_holdout(ledger or self.ledger, self.registry, rid,
                                   candidate=candidate or self.candidate, preregistration=self.doc,
                                   manifest=MANIFEST, history=history, holdout_rows=holdout,
                                   fit=fit, decide=decide)

    def verify(self, report, *, history=HISTORY, holdout=HOLDOUT):
        return ho.verify_holdout(self.ledger, self.registry, report, candidate=self.candidate,
                                 preregistration=self.doc, manifest=MANIFEST,
                                 history=history, holdout_rows=holdout)

    def kinds(self):
        return [kind for kind, _ in self.ledger.events(STUDY)]

    def close(self):
        self.ledger.close()
        self.registry.close()


@pytest.fixture
def setup(tmp_path):
    s = Setup(tmp_path)
    yield s
    s.close()


# ---------------------------------------------------------------- happy path and lifecycle
def test_single_use_lifecycle_and_verification(setup):
    rid = setup.granted()
    report = setup.evaluate(rid)
    assert report.status == "VALID_RESEARCH_RESULT" and report.label == "SEALED_SINGLE_USE_UNANCHORED"
    assert setup.kinds() == ["ACCESS_REQUESTED", "GRANTED", "DISCLOSED", "RETIRED"]
    assert setup.ledger.study_state(STUDY) == "RETIRED"
    verified = setup.verify(report)
    assert verified.status == "VALID_RESEARCH_RESULT", verified.reasons
    assert verified.metrics == report.metrics
    m = report.metrics
    assert m["candidate"]["trial_id"] == setup.candidate.trial_id
    assert m["candidate"]["output_hash"] == setup.candidate.output_hash
    assert m["data"]["holdout_rows"] == 30 and m["train_cutoff_utc_ns"] == str(day(HOLD[0]) - DAY)


def test_holdout_evaluation_is_not_a_registry_attempt(setup):
    before = setup.registry.status_counts(STUDY)
    setup.evaluate(setup.granted())
    assert setup.registry.status_counts(STUDY) == before == {"COMPLETED": 1}


def test_metrics_match_float_reference(setup):
    report = setup.evaluate(setup.granted())
    m = report.metrics
    positions = [float(p) for p in m["positions"]]
    returns = [float(r.excess_return) for r in HOLDOUT]
    prev, nets = 0.0, []
    for i, (p, r) in enumerate(zip(positions, returns)):
        turnover = abs(p - prev) + (abs(p) if i == len(positions) - 1 else 0.0)
        nets.append(p * r - turnover * 0.0015)
        prev = p
    assert [float(x) for x in m["net_excess_returns"]] == pytest.approx(nets, abs=1e-15)
    mean = sum(nets) / len(nets)
    sd = math.sqrt(sum((x - mean) ** 2 for x in nets) / (len(nets) - 1))
    assert float(m["sharpe"]) == pytest.approx(mean / sd, abs=1e-9)
    assert float(m["compounded_net_excess_return"]) == pytest.approx(
        math.prod(1 + x for x in nets) - 1, abs=1e-12)


def test_entry_flip_and_close_costs_charge_each_side_once(setup):
    script = {160: "1", 163: "-1", 170: "0", 180: "0.5"}
    last = ["0"]
    def decide(state, now_utc_ns, features, cfg):
        last[0] = script.get((now_utc_ns - BASE) // DAY, last[0])
        return last[0]
    report = setup.evaluate(setup.granted(), decide=decide)
    costs = [float(c) for c in report.metrics["costs"]]
    rate = 0.0015
    assert costs[0] == pytest.approx(rate) and costs[3] == pytest.approx(2 * rate)   # entry, flip
    assert costs[10] == pytest.approx(rate) and costs[20] == pytest.approx(0.5 * rate)
    assert costs[29] == pytest.approx(0.5 * rate)                                   # closing
    assert float(report.metrics["turnover"]) == pytest.approx(1 + 2 + 1 + 0.5 + 0.5)


def test_equivalent_to_a_matching_walk_forward_window(tmp_path):
    """Same rows, purge, costs and callbacks: the holdout window [160, 190) equals walk-forward
    fold [160, 190) of an independent study whose plan ends there (no holdout.py import of it)."""
    s = Setup(tmp_path)
    try:
        report = s.evaluate(s.granted())
    finally:
        s.close()
    reg = TrialRegistry(tmp_path / "wf.sqlite")
    doc = {"holdout": {"start_utc_ns": str(day(300)), "end_utc_ns": str(day(400))}}
    reg.register_study(StudySpec(study_id=STUDY, n_max=5, allowed_paths=PATHS,
                                 preregistration_hash=content_hash(wf.PREREGISTRATION_DOMAIN, doc)))
    with reg:
        runner = wf.WalkForward(make_rows(wf.PanelRow), preregistration=doc, manifest=MANIFEST,
                                source_sha256="c" * 64, fit=fit_mean, decide=decide_sign)
        result = run_registered_trial(reg, STUDY, config(plan=make_plan((100, 130, 160, 190))),
                                      MANIFEST, runner.evaluate)
    fold = result.metrics["folds"][2]
    m = report.metrics
    assert fold["oos_start_utc_ns"] == str(day(160)) and fold["train_cutoff_utc_ns"] == m["train_cutoff_utc_ns"]
    for key in ("positions", "gross_excess_returns", "costs", "net_excess_returns", "turnover",
                "total_cost", "mean", "sharpe", "compounded_net_excess_return", "train_label_rows",
                "fit_feature_rows"):
        assert fold[key] == m[key], key


# ---------------------------------------------------------------- atomic single use (A, B)
def test_second_evaluation_is_refused_and_logged(setup):
    rid = setup.granted()
    setup.evaluate(rid)
    with pytest.raises(ho.HoldoutError, match="not_granted_or_already_disclosed"):
        setup.evaluate(rid)
    assert setup.kinds()[-1] == "DENIED"
    with pytest.raises(ho.HoldoutError, match="holdout_consumed"):
        setup.request()


def test_concurrent_requests_yield_one_pending_request(setup):
    ledgers = [ho.HoldoutLedger(setup.path / "holdout.sqlite") for _ in range(2)]
    barrier, outcomes = threading.Barrier(2), []
    def worker(ledger):
        barrier.wait()
        try:
            outcomes.append(("ok", setup.request(ledger=ledger)))
        except ho.HoldoutError as exc:
            outcomes.append(("refused", str(exc)))
    threads = [threading.Thread(target=worker, args=(lg,)) for lg in ledgers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for lg in ledgers:
        lg.close()
    assert sorted(o[0] for o in outcomes) == ["ok", "refused"]
    assert "request_pending" in [o[1] for o in outcomes if o[0] == "refused"][0]
    assert sorted(setup.kinds()) == ["ACCESS_REQUESTED", "DENIED"]


def test_concurrent_evaluations_disclose_once(setup):
    rid = setup.granted()
    ledgers = [ho.HoldoutLedger(setup.path / "holdout.sqlite") for _ in range(2)]
    barrier, outcomes = threading.Barrier(2), []
    def worker(ledger):
        barrier.wait()
        try:
            outcomes.append(("ok", setup.evaluate(rid, ledger=ledger).status))
        except ho.HoldoutError as exc:
            outcomes.append(("refused", str(exc)))
    threads = [threading.Thread(target=worker, args=(lg,)) for lg in ledgers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for lg in ledgers:
        lg.close()
    assert sorted(o[0] for o in outcomes) == ["ok", "refused"]
    kinds = setup.kinds()
    assert kinds.count("DISCLOSED") == 1 and kinds.count("RETIRED") == 1 and kinds.count("DENIED") == 1


def test_double_grant_is_refused(setup):
    rid = setup.granted()
    with pytest.raises(ho.HoldoutError, match="request_not_pending"):
        ho.grant(setup.ledger, rid, owner_confirms=True)
    assert setup.kinds() == ["ACCESS_REQUESTED", "GRANTED", "DENIED"]


_CRASH = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
import test_validation_holdout as t
from crypto_trader.validation import holdout as ho
from crypto_trader.validation.registry import TrialRegistry
from crypto_trader.validation.research_path import AdmissibleResult
from pathlib import Path
path = Path(sys.argv[2])
candidate = AdmissibleResult(**json.loads((path / "candidate.json").read_text()))
def crash(state, now_utc_ns, features, cfg):
    os._exit(3)   # abrupt process death after DISCLOSED: no RETIRED can be written
with TrialRegistry(path / "trials.sqlite") as reg, ho.HoldoutLedger(path / "holdout.sqlite") as led:
    ho.evaluate_holdout(led, reg, sys.argv[3], candidate=candidate, preregistration=t.DOC,
                        manifest=t.MANIFEST, history=t.HISTORY, holdout_rows=t.HOLDOUT,
                        fit=t.fit_mean, decide=crash)
"""


def test_crash_after_disclosure_consumes_the_holdout(setup):
    rid = setup.granted()
    (setup.path / "candidate.json").write_text(json.dumps(dataclasses.asdict(setup.candidate)))
    script = setup.path / "crash.py"
    script.write_text(_CRASH)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-B", str(script), os.path.dirname(__file__),
                           str(setup.path), rid], env=env, capture_output=True, timeout=120)
    assert done.returncode == 3, done.stderr.decode(errors="replace")[-2000:]
    setup.ledger.close()
    setup.ledger = ho.HoldoutLedger(setup.path / "holdout.sqlite")
    assert setup.kinds() == ["ACCESS_REQUESTED", "GRANTED", "DISCLOSED"]
    assert setup.ledger.study_state(STUDY) == "DISCLOSED"
    with pytest.raises(ho.HoldoutError, match="not_granted_or_already_disclosed"):
        setup.evaluate(rid)
    with pytest.raises(ho.HoldoutError, match="holdout_consumed"):
        setup.request()


@pytest.mark.parametrize("error, outcome", [(ValueError, "FAILED"), (KeyboardInterrupt, "ABORTED")])
def test_failure_after_disclosure_retires_and_consumes(setup, error, outcome):
    rid = setup.granted()
    def broken(features, labels, cfg):
        raise error("fit broke")
    with pytest.raises(error):
        setup.evaluate(rid, fit=broken)
    events = setup.ledger.events(STUDY)
    assert [k for k, _ in events] == ["ACCESS_REQUESTED", "GRANTED", "DISCLOSED", "RETIRED"]
    assert events[-1][1]["outcome"] == outcome and events[-1][1]["result_sha256"] is None
    with pytest.raises(ho.HoldoutError, match="not_granted_or_already_disclosed"):
        setup.evaluate(rid)


def test_disclosed_is_durable_before_any_callback_sees_holdout_data(setup):
    rid = setup.granted()
    seen = []
    def spy(features, labels, cfg):
        with closing(sqlite3.connect(setup.path / "holdout.sqlite")) as other:
            seen.append([r[0] for r in other.execute("SELECT kind FROM holdout_events ORDER BY seq")])
        return {}
    setup.evaluate(rid, fit=spy)
    assert seen == [["ACCESS_REQUESTED", "GRANTED", "DISCLOSED"]]


# ---------------------------------------------------------------- grant and candidate freeze (C)
@pytest.mark.parametrize("confirm", [False, "yes", 1, None])
def test_grant_requires_explicit_owner_confirmation(setup, confirm):
    rid = setup.request()
    with pytest.raises(ho.HoldoutError, match="owner_not_confirmed"):
        ho.grant(setup.ledger, rid, owner_confirms=confirm)
    assert setup.kinds() == ["ACCESS_REQUESTED", "DENIED"]
    with pytest.raises(ho.HoldoutError, match="not_granted_or_already_disclosed"):
        setup.evaluate(rid)
    assert setup.ledger.study_state(STUDY) == "OPEN"   # nothing consumed; a new request may follow


def test_changing_the_candidate_after_grant_is_refused_without_consuming(setup):
    rid = setup.granted()
    other = setup.walk_forward("always-long")
    with pytest.raises(ho.HoldoutError, match="candidate_changed"):
        setup.evaluate(rid, candidate=other)
    assert setup.ledger.study_state(STUDY) == "GRANTED"
    assert setup.evaluate(rid).status == "VALID_RESEARCH_RESULT"


def test_forged_or_unregistered_candidates_are_refused(setup):
    forged = replace(setup.candidate, metrics_json=setup.candidate.metrics_json.replace('"1d"', '"1h"'))
    with pytest.raises(ho.HoldoutError, match="candidate_without_registration_proof"):
        setup.request(candidate=forged)
    with pytest.raises(ho.HoldoutError, match="candidate_without_registration_proof"):
        setup.request(candidate=object())
    assert setup.kinds() == ["DENIED", "DENIED"]


def test_candidate_must_be_a_walk_forward_result(setup):
    plain = run_registered_trial(setup.registry, STUDY, {"other": 1}, MANIFEST,
                                 lambda receipt, cfg: {"score": "1"})
    with pytest.raises(ho.HoldoutError, match="candidate_not_walk_forward"):
        setup.request(candidate=plain)


def test_missing_or_mismatched_commitment_is_refused(tmp_path):
    s = Setup(tmp_path, doc=make_doc(commitment=False))
    try:
        with pytest.raises(ho.HoldoutError, match="holdout_commitment_missing"):
            s.request()
    finally:
        s.close()


def test_preregistration_other_than_the_studys_is_refused(setup):
    other = make_doc()
    other["study"] = "edited after registration"
    with pytest.raises(ho.HoldoutError, match="binding_or_preregistration_mismatch"):
        ho.request_access(setup.ledger, setup.registry, study_id=STUDY, candidate=setup.candidate,
                          preregistration=other, manifest=MANIFEST, history=HISTORY)


# ---------------------------------------------------------------- data commitment and causality (E)
def test_holdout_rows_must_match_the_commitment(setup):
    rid = setup.granted()
    tampered = HOLDOUT[:5] + (replace(HOLDOUT[5], excess_return="0.05"),) + HOLDOUT[6:]
    with pytest.raises(ho.HoldoutError, match="holdout_data_mismatch"):
        setup.evaluate(rid, holdout=tampered)
    with pytest.raises(ho.HoldoutError, match="holdout_data_mismatch"):
        setup.evaluate(rid, holdout=HOLDOUT[:-1])
    assert setup.ledger.study_state(STUDY) == "GRANTED"   # refused before any exposure


def test_history_reaching_into_the_holdout_is_refused(setup):
    leaky = HISTORY[:-1] + (replace(HISTORY[-1], exposure_end_utc_ns=day(HOLD[0]) + 1),)
    with pytest.raises(ho.HoldoutError, match="history_overlaps_holdout"):
        setup.request(history=leaky)                      # refused at the request
    rid = setup.granted()
    with pytest.raises(ho.HoldoutError, match="history_changed"):
        setup.evaluate(rid, history=leaky)                # and never accepted later
    assert setup.ledger.study_state(STUDY) == "GRANTED"


# ---------------------------------------------------------------- R8: frozen training history
def test_history_swapped_after_the_request_gives_no_new_result(setup):
    """Sol P4-05 R8: history returns 0.02 and features signal 1 after the grant."""
    rid = setup.granted()
    swapped = tuple(replace(r, excess_return="0.02", features=(("signal", "1"),)) for r in HISTORY)
    with pytest.raises(ho.HoldoutError, match="history_changed"):
        setup.evaluate(rid, history=swapped)
    assert setup.ledger.study_state(STUDY) == "GRANTED"            # nothing disclosed
    report = setup.evaluate(rid)
    assert "history_changed" in setup.verify(report, history=swapped).reasons
    assert report.metrics["data"]["history_commitment"] == setup.ledger.events(STUDY)[0][1]["history"]


@pytest.mark.parametrize("mutate", [
    lambda h: h[:50] + (replace(h[50], row_id="x00050"),) + h[51:],
    lambda h: h[:50] + (replace(h[50], feature_available_utc_ns=day(50) - 1),) + h[51:],
    lambda h: h[:50] + (replace(h[50], revision_first_seen_utc_ns=day(50) - 1),) + h[51:],
    lambda h: h[:50] + (replace(h[50], exposure_end_utc_ns=day(51) - 1),) + h[51:],
    lambda h: h[:50] + (replace(h[50], excess_return="0.0001"),) + h[51:],
    lambda h: h[:50] + (replace(h[50], features=(("signal", "9"),)),) + h[51:],
    lambda h: h[:50] + (replace(h[50], features=h[50].features + (("z", "1"),)),) + h[51:],
    lambda h: h[1:],
    lambda h: h + (),
], ids=["row_id", "feature_available", "revision", "exposure_end", "return", "feature_value",
        "extra_feature", "dropped_row", "identical"])
def test_history_commitment_covers_order_and_every_field(setup, mutate):
    rid = setup.granted()
    changed = mutate(HISTORY)
    if changed == HISTORY:
        assert setup.evaluate(rid, history=changed).status == "VALID_RESEARCH_RESULT"
        return
    with pytest.raises(ho.HoldoutError, match="history_changed"):
        setup.evaluate(rid, history=changed)
    assert setup.ledger.study_state(STUDY) == "GRANTED"


def test_history_order_is_committed():
    swapped = HISTORY[:10] + (HISTORY[11], HISTORY[10]) + HISTORY[12:]
    assert ho._history_commitment(swapped) != ho._history_commitment(HISTORY)
    assert ho._history_commitment(HISTORY) == {"rows_sha256": ho._history_commitment(HISTORY)["rows_sha256"],
                                               "row_count": len(HISTORY)}


# ---------------------------------------------------------------- R9: nothing undisclosed before DISCLOSED
def test_sol_probe_sequence_reveals_nothing_about_the_holdout(setup):
    """Sol P4-05 R9: history=() then original / modified / original holdout under one grant."""
    rid = setup.granted()
    modified = HOLDOUT[:5] + (replace(HOLDOUT[5], excess_return="0.05"),) + HOLDOUT[6:]
    reasons = []
    for hold in (HOLDOUT, modified, HOLDOUT):
        with pytest.raises(ho.HoldoutError) as info:
            setup.evaluate(rid, history=(), holdout=hold)
        reasons.append(str(info.value))
    assert reasons == ["history_changed"] * 3                # independent of the holdout input
    assert setup.ledger.study_state(STUDY) == "GRANTED"


@pytest.mark.parametrize("supplied", [
    HOLDOUT[:5] + (replace(HOLDOUT[5], excess_return="0.05"),) + HOLDOUT[6:],
    HOLDOUT[:-1],
    HOLDOUT[1:] + HOLDOUT[:1],                                          # unsorted
    HOLDOUT[:5] + (replace(HOLDOUT[5], feature_available_utc_ns=day(200)),) + HOLDOUT[6:],
    HOLDOUT[:5] + (replace(HOLDOUT[5], decision_utc_ns=day(300)),) + HOLDOUT[6:],   # outside window
    (),
    list(HOLDOUT),
    "not rows",
], ids=["return", "short", "unsorted", "point_in_time", "outside_window", "empty", "list", "text"])
def test_any_non_matching_holdout_input_gives_one_reason_before_disclosure(setup, supplied):
    rid = setup.granted()
    with pytest.raises(ho.HoldoutError) as info:
        setup.evaluate(rid, holdout=supplied)
    assert str(info.value) == "holdout_data_mismatch"
    assert setup.ledger.study_state(STUDY) == "GRANTED"


def test_committed_holdout_content_is_checked_only_after_disclosure(tmp_path):
    """A committed holdout whose content is invalid is detected after DISCLOSED; it is consumed."""
    broken = HOLDOUT[:10] + (replace(HOLDOUT[10], exposure_end_utc_ns=day(172)),) + HOLDOUT[11:]
    s = Setup(tmp_path, doc=make_doc(holdout_rows=broken))
    try:
        rid = s.granted()
        with pytest.raises(ho.HoldoutError, match="overlapping_or_overrunning_exposure"):
            s.evaluate(rid, holdout=broken)
        events = s.ledger.events(STUDY)
        assert [k for k, _ in events] == ["ACCESS_REQUESTED", "GRANTED", "DISCLOSED", "RETIRED"]
        assert events[-1][1]["outcome"] == "FAILED"
        with pytest.raises(ho.HoldoutError, match="not_granted_or_already_disclosed"):
            s.evaluate(rid, holdout=broken)
    finally:
        s.close()


# ---------------------------------------------------------------- R7: single use is per ledger file
def test_single_use_is_scoped_to_one_ledger_file_documented_limit(setup):
    """Copying the file at GRANTED, or a fresh ledger, permits another evaluation: not prevented."""
    rid = setup.granted()
    setup.ledger.close()
    copy_path = setup.path / "copy.sqlite"
    copy_path.write_bytes((setup.path / "holdout.sqlite").read_bytes())
    setup.ledger = ho.HoldoutLedger(setup.path / "holdout.sqlite")
    assert setup.evaluate(rid).status == "VALID_RESEARCH_RESULT"
    with ho.HoldoutLedger(copy_path) as copy:
        assert setup.evaluate(rid, ledger=copy).status == "VALID_RESEARCH_RESULT"
    with ho.HoldoutLedger(setup.path / "fresh.sqlite") as fresh:
        again = setup.request(ledger=fresh)
        ho.grant(fresh, again, owner_confirms=True)
        assert setup.evaluate(again, ledger=fresh).status == "VALID_RESEARCH_RESULT"
    assert any("within this ledger file only" in item for item in ho.LIMITATIONS)
    assert not re.search(r"\bever\b", ho.__doc__) and "cannot be repeated" not in ho.__doc__


def test_too_few_holdout_observations_are_refused(tmp_path):
    short = HOLDOUT[:29]
    s = Setup(tmp_path, doc=make_doc(holdout_rows=short, window=(160, 189)))
    try:
        with pytest.raises(ho.HoldoutError, match="holdout_commitment_missing|insufficient_holdout"):
            s.request()
    finally:
        s.close()


def test_callbacks_see_only_matured_history_and_rows_decided_by_now(setup):
    rid = setup.granted()
    cutoff = day(HOLD[0]) - DAY
    log = {"fit": [], "decide": []}
    def fit(features, labels, cfg):
        log["fit"].append((max(f.decision_utc_ns for f in features), max(x.exposure_end_utc_ns for x in labels)))
        return {}
    def decide(state, now_utc_ns, features, cfg):
        assert all(type(f) is ho.FeatureRow for f in features)
        for f in features:
            with pytest.raises(AttributeError):
                f.excess_return
        log["decide"].append((now_utc_ns, max(f.decision_utc_ns for f in features)))
        return "0"
    setup.evaluate(rid, fit=fit, decide=decide)
    assert log["fit"] == [(cutoff, cutoff)]
    assert [now for now, _ in log["decide"]] == [r.decision_utc_ns for r in HOLDOUT]
    assert all(latest == now for now, latest in log["decide"])
    assert {f.name for f in dataclasses.fields(ho.FeatureRow)} == {
        "row_id", "decision_utc_ns", "feature_available_utc_ns", "features"}


def test_holdout_row_declaring_early_features_stays_hidden_until_its_decision(tmp_path):
    """A committed holdout row claims features available on day 0; it is first seen at its
    decision (day 165), so neither fit nor any earlier decide may receive it."""
    early = HOLDOUT[:5] + (replace(HOLDOUT[5], feature_available_utc_ns=day(0)),) + HOLDOUT[6:]
    s = Setup(tmp_path, doc=make_doc(holdout_rows=early))
    try:
        rid = s.granted()
        fit_ids, first_seen = [], []
        def fit(features, labels, cfg):
            fit_ids.extend(f.row_id for f in features)
            return {}
        def decide(state, now_utc_ns, features, cfg):
            if any(f.row_id == "r00165" for f in features):
                first_seen.append(now_utc_ns)
            return "0"
        s.evaluate(rid, holdout=early, fit=fit, decide=decide)
    finally:
        s.close()
    assert not {r.row_id for r in HOLDOUT} & set(fit_ids)
    assert min(first_seen) == day(165)


@pytest.mark.parametrize("position", ["1.5", 1, "1e0"])
def test_invalid_positions_fail_and_retire(setup, position):
    rid = setup.granted()
    with pytest.raises(ho.HoldoutError, match="invalid_position"):
        setup.evaluate(rid, decide=lambda s, n, f, c: position)
    assert setup.ledger.study_state(STUDY) == "RETIRED"


# ---------------------------------------------------------------- ledger integrity and verification
def test_ledger_tampering_is_detected(setup):
    setup.evaluate(setup.granted())
    setup.ledger.close()
    def raw_sql(*statements):
        with closing(sqlite3.connect(setup.path / "holdout.sqlite")) as raw:
            for sql in statements:
                raw.execute(sql)
            raw.commit()
    raw_sql("UPDATE holdout_events SET payload=replace(payload,'COMPLETED','FAILED') WHERE kind='RETIRED'")
    with pytest.raises(ho.HoldoutError, match="integrity"):
        ho.HoldoutLedger(setup.path / "holdout.sqlite")
    raw_sql("UPDATE holdout_events SET payload=replace(payload,'FAILED','COMPLETED') WHERE kind='RETIRED'",
            "DELETE FROM holdout_events WHERE kind='DISCLOSED'")
    with pytest.raises(ho.HoldoutError, match="integrity"):
        ho.HoldoutLedger(setup.path / "holdout.sqlite")
    setup.ledger = ho.HoldoutLedger(setup.path / "other.sqlite")


def test_tampered_reports_or_panels_are_refused(setup):
    report = setup.evaluate(setup.granted())
    metrics = json.loads(json.dumps(report.metrics))
    metrics["sharpe"] = "9"
    forged = replace(report, metrics=metrics)
    assert "ledger_record_mismatch" in setup.verify(forged).reasons
    tampered = HOLDOUT[:5] + (replace(HOLDOUT[5], excess_return="0.05"),) + HOLDOUT[6:]
    assert "holdout_data_mismatch" in setup.verify(report, holdout=tampered).reasons
    assert "invalid_report" in setup.verify(object()).reasons
    history = HISTORY[:100] + (replace(HISTORY[100], excess_return="0.05"),) + HISTORY[101:]
    assert "history_changed" in setup.verify(report, history=history).reasons


def test_rederivation_that_differs_from_the_record_is_refused(setup, monkeypatch):
    """Every input is now bound, so re-derivation is the check against a changed computation."""
    report = setup.evaluate(setup.granted())
    original = ho._metrics
    def drifted(*args, **kwargs):
        rebuilt = original(*args, **kwargs)
        rebuilt["turnover"] = "0"
        return rebuilt
    monkeypatch.setattr(ho, "_metrics", drifted)
    assert "metrics_not_reproducible" in setup.verify(report).reasons


def test_registry_or_ledger_change_during_verification_is_refused(setup, monkeypatch):
    report = setup.evaluate(setup.granted())
    original = ho._metrics
    def rebuild_then_write(*args, **kwargs):
        rebuilt = original(*args, **kwargs)
        setup.registry.allocate(STUDY, {"racing": 1}, MANIFEST)
        return rebuilt
    monkeypatch.setattr(ho, "_metrics", rebuild_then_write)
    report_b = setup.verify(report)
    assert report_b.status == "INSUFFICIENT_EVIDENCE" and "registry_changed_during_read" in report_b.reasons
    monkeypatch.setattr(ho, "_metrics", original)
    def rebuild_then_ledger_write(*args, **kwargs):
        rebuilt = original(*args, **kwargs)
        with pytest.raises(ho.HoldoutError):
            setup.request()            # logs a DENIED event: the ledger head moves
        return rebuilt
    monkeypatch.setattr(ho, "_metrics", rebuild_then_ledger_write)
    report_c = setup.verify(report)
    assert report_c.status == "INSUFFICIENT_EVIDENCE" and "ledger_changed_during_read" in report_c.reasons


def test_reruns_in_separate_studies_give_identical_metrics(tmp_path):
    reports = []
    for name in ("a", "b"):
        path = tmp_path / name
        path.mkdir()
        s = Setup(path)
        try:
            reports.append(s.evaluate(s.granted()))
        finally:
            s.close()
    a, b = (dict(r.metrics) for r in reports)
    assert a.pop("candidate")["trial_id"] != b.pop("candidate")["trial_id"]
    assert a == b


# ---------------------------------------------------------------- limitations, permissions, isolation (D)
def test_limitations_state_the_evidence_boundaries(setup):
    report = setup.evaluate(setup.granted())
    text = " ".join(ho.LIMITATIONS)
    for phrase in ("local SQLite", "not anchored", "owner_confirms", "not authentication",
                   "match the commitment", "not that nobody saw", "outside the interface",
                   "synthetic", "not re-run fit or decide", "within this ledger file only",
                   "unchanged since the request", "only holdout signal"):
        assert phrase in text, phrase
    assert report.limitations == ho.LIMITATIONS and report.metrics["limitations"] == list(ho.LIMITATIONS)
    m = report.metrics
    assert m["permits_strategy_approval"] is m["permits_promotion"] is m["permits_paper_or_trading"] is False
    assert report.permits_strategy_approval is report.permits_promotion is report.permits_paper_or_trading is False


def test_domains_match_the_closed_modules():
    from crypto_trader.validation import research_path
    assert ho.RESULT_DOMAIN == research_path._RESULT_DOMAIN
    assert ho.PREREGISTRATION_DOMAIN == wf.PREREGISTRATION_DOMAIN
    assert ho.BINDING_DOMAIN == wf.BINDING_DOMAIN


def test_holdout_imports_only_the_sandboxed_local_modules():
    tree = ast.parse(open(ho.__file__, encoding="utf-8").read())
    local = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level}
    absolute = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and not n.level}
    absolute |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert local == {"canonical", "contracts", "registry"}
    assert not any(name.startswith("crypto_trader") for name in absolute)


def test_holdout_error_is_a_registry_error():
    assert issubclass(ho.HoldoutError, RegistryError)
