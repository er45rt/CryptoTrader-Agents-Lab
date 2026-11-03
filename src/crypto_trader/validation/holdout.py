"""P4-05 sealed single-use holdout. Research-only: grants no strategy approval (P4-06),
promotion, Paper or trading permission.

Lifecycle per study, in a separate append-only hash-chained SQLite ledger whose event kinds are
the existing ``HoldoutAuditType`` values (meanings unchanged):
ACCESS_REQUESTED -> GRANTED -> DISCLOSED -> RETIRED, with DENIED logging every refusal.
* Commitment: the study's preregistration document (P4-04 domain) carries holdout
  {start_utc_ns, end_utc_ns} and holdout_commitment {rows_sha256, row_count}; it is proven to be
  the registered study's by recomputing the registry binding of the candidate.
* Candidate freeze: one request names one registered COMPLETED walk-forward result; its trial_id,
  binding_hash, output_hash and frozen configuration hash are recorded and must be identical at
  evaluation. One study has at most one pending request and at most one GRANTED, ever.
* Grant: requires ``owner_confirms=True``, a caller declaration, not owner authentication.
* Consume before exposure: every check that needs no callback runs first (a failure logs DENIED
  and consumes nothing); then DISCLOSED is committed durably BEFORE fit or decide receive any
  holdout data. Success, failure or interruption ends RETIRED; a crash after DISCLOSED leaves the
  study DISCLOSED, which refuses any further evaluation. Single use is decided inside
  BEGIN IMMEDIATE transactions that replay and verify the whole ledger.
* The final evaluation is not a registry attempt (DSR M is unchanged) and cannot be repeated.

Evaluation (same causal interface and accounting as P4-04, re-implemented here because the
validation sandbox forbids importing walk_forward): one fit at cutoff c = holdout_start - purge
on history features of rows decided by c and outcomes matured by c (decision >= anchor_start);
then per holdout step decide sees the frozen state, a fresh config copy and the features of rows
decided by that step, never a realized return. Single asset, position in [-1, 1], start flat,
close after the last step, cost = turnover * (fee_bps + slippage_bps) / 10000 once per traded
side, net = gross - cost, mean, (T-1) Sharpe, prod(1 + net) - 1.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from decimal import (
    Context, Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext,
)
from pathlib import Path
from threading import RLock
from time import time_ns
from uuid import uuid4

from .canonical import (
    SERIALIZER_ID, CanonicalError, _nfc, canonical_bytes, canonical_uuid, content_hash,
    input_manifest, parse_json_strict, sha256_text,
)
from .contracts import HoldoutAuditType, RegistryError
from .registry import TrialRegistry

RESULT_DOMAIN = "research-result-v1"                     # = research_path._RESULT_DOMAIN (tested)
BINDING_DOMAIN = "trial-binding-v1"                      # = registry binding domain (tested)
PREREGISTRATION_DOMAIN = "walk-forward-preregistration-v1"  # = walk_forward's (tested)
CANDIDATE_SCHEMA = "walk-forward-oos-v1"
SCHEMA = "holdout-result-v1"
LABEL = "SEALED_SINGLE_USE_UNANCHORED"
MIN_HOLDOUT_OBSERVATIONS = 30
MAX_ROWS = 20000
VALID = "VALID_RESEARCH_RESULT"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

_ROWS_DOMAIN = "holdout-rows-v1"
_RESULT_HASH_DOMAIN = "holdout-result-v1"
_EVENT_DOMAIN = "holdout-ledger-event-v1"
_CONFIG_DOMAIN = "holdout-candidate-config-v1"
_STATE_DOMAIN = "holdout-fit-state-v1"
_GENESIS = "0" * 64
_MAX_NS = 2**63 - 1
_NS = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
_DEC = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_BPS = Decimal(10000)
_ZERO, _ONE = Decimal(0), Decimal(1)
_PLAN_KEYS = frozenset({
    "warmup_start_utc_ns", "anchor_start_utc_ns", "oos_boundaries_utc_ns", "purge_ns",
    "period", "fee_bps", "slippage_bps",
})
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS holdout_events (
    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, study_id TEXT NOT NULL,
    payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
"""
_REQUESTED, _GRANTED, _DENIED = "REQUESTED", "GRANTED", "DENIED"
_DISCLOSED, _RETIRED = "DISCLOSED", "RETIRED"

ASSUMPTIONS = (
    "single asset; position is a fraction of capital in [-1, 1]; excess return applies linearly",
    "the holdout window starts flat and is closed after its last step",
    "cost = turnover * (fee_bps + slippage_bps) / 10000, once per traded side",
    "one fit at holdout_start - purge on matured history, by the candidate's frozen configuration",
)
LIMITATIONS = (
    "the holdout ledger is local SQLite and not anchored externally; an administrator can "
    "rewrite it and its hash chain",
    "owner_confirms=True is a caller declaration, not authentication of the owner",
    "hashes show the supplied rows match the commitment, not that nobody saw the holdout data",
    "no guarantee against disclosure or ledger edits outside the interface of this module; "
    "callbacks reading closures or global state are not contained",
    "synthetic test results do not admit or validate any real market data source "
    "(anchor_missing stands)",
    "verification recomputes metrics given the recorded positions and fit-state hash; it does "
    "not re-run fit or decide",
    "costs are research assumptions, not observed execution prices",
    "a research result grants no strategy approval, promotion, Paper, Testnet or Live permission",
)


class HoldoutError(RegistryError):
    """Refuse; nothing partial is returned. The message starts with a fixed reason code."""


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
    """A matured history outcome, handed to fit only."""
    row_id: str
    decision_utc_ns: int
    exposure_end_utc_ns: int
    excess_return: str


@dataclass(frozen=True)
class HoldoutReport:
    status: str
    reasons: tuple[str, ...]
    study_id: object
    request_id: object
    evidence_hashes: tuple[str, ...] = ()
    metrics: dict | None = None
    label: str = LABEL
    limitations: tuple[str, ...] = LIMITATIONS
    permits_strategy_approval: bool = False
    permits_promotion: bool = False
    permits_paper_or_trading: bool = False


@dataclass(frozen=True)
class _Plan:
    warmup_start: int
    anchor_start: int
    purge: int
    period: str
    cost_rate: Decimal


@dataclass(frozen=True)
class _Candidate:
    trial_id: str
    binding_hash: str
    output_hash: str
    config: dict
    config_sha256: str
    preregistration_sha256: str
    holdout: tuple[int, int]
    commitment: dict
    registry_head: str

    def frozen(self) -> dict:
        return {"trial_id": self.trial_id, "binding_hash": self.binding_hash,
                "output_hash": self.output_hash, "config_sha256": self.config_sha256}


@dataclass(frozen=True)
class _Layout:
    cutoff: int
    features: tuple[FeatureRow, ...]
    labels: tuple[LabelRow, ...]
    steps: tuple[PanelRow, ...]
    by_decision: tuple[FeatureRow, ...]
    history_rows: int
    history_sha256: str
    holdout_sha256: str


def _ctx() -> Context:
    return Context(prec=50, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])


# ---------------------------------------------------------------- ledger
class HoldoutLedger:
    """Append-only, hash-chained holdout access ledger. UNANCHORED local evidence."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = RLock()
        self._db = sqlite3.connect(self.path, isolation_level=None, timeout=30.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            if str(self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() != "wal":
                raise HoldoutError("ledger_unavailable: WAL journal mode")
            self._db.execute("PRAGMA synchronous=FULL")
            if self._db.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise HoldoutError("ledger_unavailable: FULL synchronous")
            self._db.executescript(_SCHEMA_SQL)
            self._read(lambda head, studies: None)
        except BaseException:
            self._db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self._lock:
            self._db.close()

    def _replay(self):
        """Verify the chain and every lifecycle transition; return (head, studies)."""
        prev, studies = _GENESIS, {}
        for expected_seq, row in enumerate(self._db.execute("SELECT * FROM holdout_events ORDER BY seq"), 1):
            try:
                if row["seq"] != expected_seq or row["prev_hash"] != prev:
                    raise HoldoutError("integrity: broken holdout event chain")
                payload = parse_json_strict(row["payload"])
                if canonical_bytes(payload).decode("utf-8") != row["payload"]:
                    raise HoldoutError("integrity: noncanonical holdout event")
                expected = content_hash(_EVENT_DOMAIN, {
                    "seq": expected_seq, "kind": row["kind"], "study_id": row["study_id"],
                    "payload": row["payload"], "prev_hash": prev,
                })
                if row["hash"] != expected:
                    raise HoldoutError("integrity: holdout event hash mismatch")
                data = payload["data"]
                if data["study_id"] != row["study_id"]:
                    raise HoldoutError("integrity: holdout event study mismatch")
                _apply(studies.setdefault(row["study_id"], _new_study()),
                       HoldoutAuditType(row["kind"]), data)
                prev = row["hash"]
            except (CanonicalError, KeyError, TypeError, ValueError) as exc:
                raise HoldoutError("integrity: malformed holdout ledger") from exc
        return prev, studies

    def _append(self, kind: HoldoutAuditType, study_id: str, data: dict) -> None:
        last = self._db.execute("SELECT seq,hash FROM holdout_events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, _GENESIS)
        payload = canonical_bytes({"at_utc_ns": str(time_ns()), "data": data}).decode("utf-8")
        digest = content_hash(_EVENT_DOMAIN, {
            "seq": seq, "kind": kind.value, "study_id": study_id, "payload": payload, "prev_hash": prev,
        })
        self._db.execute(
            "INSERT INTO holdout_events(seq,kind,study_id,payload,prev_hash,hash) VALUES (?,?,?,?,?,?)",
            (seq, kind.value, study_id, payload, prev, digest),
        )

    def _read(self, read):
        with self._lock:
            self._db.execute("BEGIN")
            try:
                head, studies = self._replay()
                result = read(head, studies)
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _run(self, action):
        """One serialized write: BEGIN IMMEDIATE, full replay, then action(studies) -> (value, refusal).
        A refusal is committed (its DENIED event stays) and then raised."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                _, studies = self._replay()
                value, refusal = action(studies)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        if refusal is not None:
            raise HoldoutError(refusal)
        return value

    def _deny(self, study_id: str, request_id, reason: str) -> None:
        def apply(studies):
            known = request_id is not None and request_id in studies.get(study_id, _new_study())["requests"]
            self._append(HoldoutAuditType.DENIED, study_id, {
                "study_id": study_id, "request_id": request_id if known else None, "reason": reason})
            return None, None
        self._run(apply)

    def head(self) -> str:
        return self._read(lambda head, studies: head)

    def study_state(self, study_id: str) -> str:
        return self._read(lambda head, studies: _state(studies.get(study_id, _new_study())))

    def events(self, study_id: str) -> list[tuple[str, dict]]:
        def read(head, studies):
            return [(row["kind"], parse_json_strict(row["payload"])["data"]) for row in self._db.execute(
                "SELECT kind,payload FROM holdout_events WHERE study_id=? ORDER BY seq", (study_id,))]
        return self._read(read)

    def _request(self, request_id: str):
        def read(head, studies):
            for study_id, study in studies.items():
                if request_id in study["requests"]:
                    return study_id, dict(study["requests"][request_id])
            return None, None
        return self._read(read)


def _new_study() -> dict:
    return {"requests": {}, "granted": None}


def _state(study: dict) -> str:
    if study["granted"] is not None:
        return study["requests"][study["granted"]]["status"]
    if any(r["status"] == _REQUESTED for r in study["requests"].values()):
        return _REQUESTED
    return "OPEN"


def _apply(study: dict, kind: HoldoutAuditType, data: dict) -> None:
    """The single-use state machine, enforced on every replay."""
    requests = study["requests"]
    rid = data["request_id"]
    if kind is HoldoutAuditType.ACCESS_REQUESTED:
        if rid in requests or study["granted"] is not None or _state(study) == _REQUESTED:
            raise HoldoutError("integrity: request while pending or consumed")
        requests[rid] = {"status": _REQUESTED, "candidate": data["candidate"],
                         "preregistration_sha256": data["preregistration_sha256"],
                         "commitment": data["commitment"], "outcome": None, "result_sha256": None}
    elif kind is HoldoutAuditType.GRANTED:
        if rid not in requests or requests[rid]["status"] != _REQUESTED or study["granted"] is not None:
            raise HoldoutError("integrity: invalid grant")
        requests[rid]["status"] = _GRANTED
        study["granted"] = rid
    elif kind is HoldoutAuditType.DENIED:
        if rid is not None:
            if rid not in requests:
                raise HoldoutError("integrity: denial of unknown request")
            if requests[rid]["status"] == _REQUESTED:
                requests[rid]["status"] = _DENIED
    elif kind is HoldoutAuditType.DISCLOSED:
        if rid is None or rid != study["granted"] or requests[rid]["status"] != _GRANTED:
            raise HoldoutError("integrity: disclosure without grant")
        requests[rid]["status"] = _DISCLOSED
    elif kind is HoldoutAuditType.RETIRED:
        if rid not in requests or requests[rid]["status"] != _DISCLOSED:
            raise HoldoutError("integrity: retirement without disclosure")
        if data["outcome"] not in ("COMPLETED", "FAILED", "ABORTED"):
            raise HoldoutError("integrity: invalid retirement outcome")
        requests[rid].update(status=_RETIRED, outcome=data["outcome"], result_sha256=data["result_sha256"])


# ---------------------------------------------------------------- inputs
def _ns(value, what: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_NS:
        raise HoldoutError(f"corrupt_timestamp:{what}")
    return value


def _ns_text(value, what: str) -> int:
    if type(value) is not str or not _NS.fullmatch(value) or int(value) > _MAX_NS:
        raise HoldoutError(f"corrupt_timestamp:{what}")
    return int(value)


def _decimal(value, what: str) -> Decimal:
    if type(value) is not str or not _DEC.fullmatch(value):
        raise HoldoutError(f"invalid_decimal:{what}")
    return Decimal(value)


def _position(value) -> Decimal:
    if type(value) is not str or not _DEC.fullmatch(value) or not -_ONE <= Decimal(value) <= _ONE:
        raise HoldoutError("invalid_position")
    return Decimal(value)


def _validated_rows(rows, what: str) -> tuple[PanelRow, ...]:
    if type(rows) is not tuple or len(rows) > MAX_ROWS:
        raise HoldoutError(f"invalid_panel:{what}")
    ids: set[str] = set()
    last = None
    for row in rows:
        if type(row) is not PanelRow:
            raise HoldoutError(f"invalid_panel:{what}")
        try:
            _nfc(row.row_id)
        except CanonicalError as exc:
            raise HoldoutError("duplicate_or_empty_row_id") from exc
        if not row.row_id or row.row_id in ids:
            raise HoldoutError("duplicate_or_empty_row_id")
        ids.add(row.row_id)
        decision = _ns(row.decision_utc_ns, "decision")
        available = _ns(row.feature_available_utc_ns, "feature_available")
        revision = _ns(row.revision_first_seen_utc_ns, "revision_first_seen")
        if available > decision or revision > decision:
            raise HoldoutError("point_in_time_violation")
        if _ns(row.exposure_end_utc_ns, "exposure_end") <= decision:
            raise HoldoutError("invalid_exposure")
        if last is not None and decision <= last:
            raise HoldoutError("unsorted_or_duplicate_decision")
        last = decision
        if _decimal(row.excess_return, "excess_return") <= -_ONE:
            raise HoldoutError("invalid_excess_return")
        features = row.features
        if (type(features) is not tuple
                or any(type(p) is not tuple or len(p) != 2 or type(p[0]) is not str
                       or type(p[1]) is not str for p in features)):
            raise HoldoutError("invalid_features")
        keys = [k for k, _ in features]
        if keys != sorted(set(keys)):
            raise HoldoutError("invalid_features")
        try:
            for key, value in features:
                _nfc(key)
                _nfc(value)
        except CanonicalError as exc:
            raise HoldoutError("invalid_features") from exc
    return rows


def holdout_rows_sha256(rows) -> str:
    """Commitment digest of holdout rows (to be placed in the preregistration document)."""
    return content_hash(_ROWS_DOMAIN, [{
        "row_id": r.row_id, "decision_utc_ns": str(r.decision_utc_ns),
        "feature_available_utc_ns": str(r.feature_available_utc_ns),
        "revision_first_seen_utc_ns": str(r.revision_first_seen_utc_ns),
        "exposure_end_utc_ns": str(r.exposure_end_utc_ns),
        "excess_return": r.excess_return, "features": [list(p) for p in r.features],
    } for r in _validated_rows(rows, "holdout")])


def _preregistration(document):
    try:
        prereg = content_hash(PREREGISTRATION_DOMAIN, document)
        copy = parse_json_strict(canonical_bytes(document).decode("utf-8"))
    except (CanonicalError, TypeError) as exc:
        raise HoldoutError("holdout_unproven") from exc
    holdout = copy.get("holdout") if type(copy) is dict else None
    if type(holdout) is not dict or set(holdout) != {"start_utc_ns", "end_utc_ns"}:
        raise HoldoutError("holdout_unproven")
    try:
        start = _ns_text(holdout["start_utc_ns"], "holdout")
        end = _ns_text(holdout["end_utc_ns"], "holdout")
    except HoldoutError as exc:
        raise HoldoutError("holdout_unproven") from exc
    if start >= end:
        raise HoldoutError("holdout_unproven")
    commitment = copy.get("holdout_commitment")
    if (type(commitment) is not dict or set(commitment) != {"rows_sha256", "row_count"}
            or type(commitment["row_count"]) is not int):
        raise HoldoutError("holdout_commitment_missing")
    try:
        sha256_text(commitment["rows_sha256"])
    except CanonicalError as exc:
        raise HoldoutError("holdout_commitment_missing") from exc
    if commitment["row_count"] < MIN_HOLDOUT_OBSERVATIONS:
        raise HoldoutError("insufficient_holdout_observations")
    return prereg, (start, end), commitment


def _binding(study_id: str, prereg: str, config, manifest, parent) -> str:
    """The registry's trial binding (registry.allocate), recomputed from the preregistration."""
    return content_hash(BINDING_DOMAIN, {
        "study_id": study_id, "preregistration_hash": prereg, "serializer_id": SERIALIZER_ID,
        "config": config, "manifest": manifest, "parent": parent,
    })


def _candidate(registry, study_id: str, candidate, preregistration, manifest, parent) -> _Candidate:
    """Registration proof of a COMPLETED walk-forward candidate bound to the preregistration."""
    if not isinstance(registry, TrialRegistry):
        raise RegistryError("a TrialRegistry is required")
    head = registry.verify().head_hash
    try:
        trial_id, rstudy, binding = candidate.trial_id, candidate.study_id, candidate.binding_hash
        metrics_json, output_hash = candidate.metrics_json, candidate.output_hash
        metrics = parse_json_strict(metrics_json)
        expected = content_hash(RESULT_DOMAIN, {
            "trial_id": trial_id, "study_id": rstudy, "binding_hash": binding, "metrics": metrics,
        })
        record = registry.trial_record(trial_id)
    except (AttributeError, CanonicalError, RegistryError, TypeError, ValueError) as exc:
        raise HoldoutError("candidate_without_registration_proof") from exc
    if (rstudy != study_id or record["study_id"] != study_id or record["status"] != "COMPLETED"
            or record["binding_hash"] != binding or output_hash != expected
            or record["output_hash"] != expected):
        raise HoldoutError("candidate_without_registration_proof")
    if type(metrics) is not dict or metrics.get("schema") != CANDIDATE_SCHEMA:
        raise HoldoutError("candidate_not_walk_forward")
    prereg, holdout, commitment = _preregistration(preregistration)
    try:
        approved = input_manifest(manifest, allowed_paths=tuple(manifest))
    except (CanonicalError, TypeError) as exc:
        raise HoldoutError("invalid_manifest") from exc
    config = metrics.get("config")
    if (_binding(study_id, prereg, config, approved, parent) != binding
            or metrics.get("preregistration_sha256") != prereg
            or metrics.get("holdout") != {"start_utc_ns": str(holdout[0]), "end_utc_ns": str(holdout[1])}):
        raise HoldoutError("binding_or_preregistration_mismatch")
    if registry.verify().head_hash != head:
        raise HoldoutError("registry_changed_during_read")
    return _Candidate(trial_id, binding, output_hash, config, content_hash(_CONFIG_DOMAIN, config),
                      prereg, holdout, commitment, head)


def _plan(config) -> _Plan:
    plan = config.get("walk_forward") if type(config) is dict else None
    if type(plan) is not dict or set(plan) != _PLAN_KEYS:
        raise HoldoutError("invalid_plan")
    warmup = _ns_text(plan["warmup_start_utc_ns"], "warmup_start")
    anchor = _ns_text(plan["anchor_start_utc_ns"], "anchor_start")
    purge = _ns_text(plan["purge_ns"], "purge")
    period = plan["period"]
    if not warmup <= anchor or type(period) is not str or not period:
        raise HoldoutError("invalid_plan")
    fee = _decimal(plan["fee_bps"], "fee_bps")
    slippage = _decimal(plan["slippage_bps"], "slippage_bps")
    if not (_ZERO <= fee <= _BPS and _ZERO <= slippage <= _BPS):
        raise HoldoutError("invalid_cost_model")
    with localcontext(_ctx()):
        rate = (fee + slippage) / _BPS
    return _Plan(warmup, anchor, purge, period, rate)


def _rows_hash(rows) -> str:
    return content_hash(_ROWS_DOMAIN, [{
        "row_id": r.row_id, "decision_utc_ns": str(r.decision_utc_ns),
        "feature_available_utc_ns": str(r.feature_available_utc_ns),
        "revision_first_seen_utc_ns": str(r.revision_first_seen_utc_ns),
        "exposure_end_utc_ns": str(r.exposure_end_utc_ns),
        "excess_return": r.excess_return, "features": [list(p) for p in r.features],
    } for r in rows])


def _feature_row(row: PanelRow) -> FeatureRow:
    return FeatureRow(row.row_id, row.decision_utc_ns, row.feature_available_utc_ns, row.features)


def _layout(history, holdout_rows, plan: _Plan, holdout: tuple[int, int], commitment: dict) -> _Layout:
    start, end = holdout
    past = _validated_rows(history, "history")
    rows = _validated_rows(holdout_rows, "holdout")
    for r in past:
        if r.decision_utc_ns >= start or r.exposure_end_utc_ns > start:
            raise HoldoutError("history_overlaps_holdout")
    if _rows_hash(rows) != commitment["rows_sha256"] or len(rows) != commitment["row_count"]:
        raise HoldoutError("holdout_data_mismatch")
    if any(not start <= r.decision_utc_ns < end for r in rows):
        raise HoldoutError("holdout_row_outside_window")
    if len(rows) < MIN_HOLDOUT_OBSERVATIONS:
        raise HoldoutError("insufficient_holdout_observations")
    for i, r in enumerate(rows):
        limit = rows[i + 1].decision_utc_ns if i + 1 < len(rows) else end
        if r.exposure_end_utc_ns > limit:
            raise HoldoutError("overlapping_or_overrunning_exposure")
    cutoff = start - plan.purge
    if not plan.anchor_start < cutoff:
        raise HoldoutError("invalid_plan")
    used = tuple(r for r in past if r.decision_utc_ns >= plan.warmup_start)
    labels = tuple(LabelRow(r.row_id, r.decision_utc_ns, r.exposure_end_utc_ns, r.excess_return)
                   for r in used
                   if r.decision_utc_ns >= plan.anchor_start and r.exposure_end_utc_ns <= cutoff)
    if not labels:
        raise HoldoutError("empty_training")
    by_decision = tuple(_feature_row(r) for r in used + rows)   # decision-sorted across the boundary
    features = tuple(f for f in by_decision if f.decision_utc_ns <= cutoff)
    return _Layout(cutoff, features, labels, rows, by_decision, len(used), _rows_hash(used),
                   commitment["rows_sha256"])


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


def _metrics(study_id: str, facts: _Candidate, plan: _Plan, layout: _Layout, positions, state: str) -> dict:
    if type(positions) is not list or len(positions) != len(layout.steps):
        raise HoldoutError("invalid_positions")
    sha256_text(state)
    with localcontext(_ctx()):
        previous = turnover_sum = cost_sum = _ZERO
        gross, costs, nets = [], [], []
        for i, (step, text) in enumerate(zip(layout.steps, positions)):
            p = _position(text)
            turnover = abs(p - previous) + (abs(p) if i == len(layout.steps) - 1 else _ZERO)
            cost = turnover * plan.cost_rate
            g = p * Decimal(step.excess_return)
            net = g - cost
            if _ONE + net <= 0:
                raise HoldoutError("invalid_compounding")
            gross.append(g)
            costs.append(cost)
            nets.append(net)
            turnover_sum += turnover
            cost_sum += cost
            previous = p
        stats = _stats(nets)
    start, end = facts.holdout
    return {
        "schema": SCHEMA, "label": LABEL, "study_id": study_id, "candidate": facts.frozen(),
        "preregistration_sha256": facts.preregistration_sha256,
        "holdout": {"start_utc_ns": str(start), "end_utc_ns": str(end)},
        "commitment": dict(facts.commitment),
        "data": {"holdout_rows_sha256": layout.holdout_sha256, "holdout_rows": len(layout.steps),
                 "history_rows_sha256": layout.history_sha256, "history_rows": layout.history_rows,
                 "source_status": "UNANCHORED_COMMITTED_NOT_ADMITTED"},
        "period": plan.period, "cost_rate": str(plan.cost_rate),
        "train_cutoff_utc_ns": str(layout.cutoff), "train_label_rows": len(layout.labels),
        "fit_feature_rows": len(layout.features), "fit_state_sha256": state,
        "positions": list(positions), "gross_excess_returns": [str(x) for x in gross],
        "costs": [str(x) for x in costs], "net_excess_returns": [str(x) for x in nets],
        "turnover": str(turnover_sum), "total_cost": str(cost_sum), **stats,
        "assumptions": list(ASSUMPTIONS), "limitations": list(LIMITATIONS),
        "permits_strategy_approval": False, "permits_promotion": False,
        "permits_paper_or_trading": False,
    }


# ---------------------------------------------------------------- lifecycle
def request_access(ledger: HoldoutLedger, registry: TrialRegistry, *, study_id: str, candidate,
                   preregistration, manifest, parent_trial_id=None) -> str:
    """Freeze one registered COMPLETED walk-forward candidate; return the request id."""
    try:
        canonical_uuid(study_id)
    except CanonicalError as exc:
        raise HoldoutError("invalid_study") from exc
    try:
        facts = _candidate(registry, study_id, candidate, preregistration, manifest, parent_trial_id)
    except HoldoutError as exc:
        ledger._deny(study_id, None, str(exc).split(":")[0])
        raise
    request_id = str(uuid4())
    def apply(studies):
        study = studies.get(study_id, _new_study())
        state = _state(study)
        reason = ("holdout_consumed" if study["granted"] is not None
                  else "request_pending" if state == _REQUESTED else None)
        if reason is not None:
            ledger._append(HoldoutAuditType.DENIED, study_id,
                           {"study_id": study_id, "request_id": None, "reason": reason})
            return None, reason
        ledger._append(HoldoutAuditType.ACCESS_REQUESTED, study_id, {
            "study_id": study_id, "request_id": request_id, "candidate": facts.frozen(),
            "preregistration_sha256": facts.preregistration_sha256,
            "commitment": dict(facts.commitment), "registry_head": facts.registry_head,
        })
        return request_id, None
    return ledger._run(apply)


def grant(ledger: HoldoutLedger, request_id: str, *, owner_confirms) -> None:
    """Grant the single access. owner_confirms must be exactly True (a declaration, not auth)."""
    try:
        canonical_uuid(request_id)
    except CanonicalError as exc:
        raise HoldoutError("unknown_request") from exc
    def apply(studies):
        for study_id, study in studies.items():
            if request_id in study["requests"]:
                break
        else:
            return None, "unknown_request"
        reason = None
        if study["requests"][request_id]["status"] != _REQUESTED:
            reason = "request_not_pending"
        elif owner_confirms is not True:
            reason = "owner_not_confirmed"
        elif study["granted"] is not None:
            reason = "holdout_consumed"
        if reason is not None:
            ledger._append(HoldoutAuditType.DENIED, study_id,
                           {"study_id": study_id, "request_id": request_id, "reason": reason})
            return None, reason
        ledger._append(HoldoutAuditType.GRANTED, study_id, {"study_id": study_id, "request_id": request_id})
        return None, None
    ledger._run(apply)


def _retire(ledger: HoldoutLedger, study_id: str, request_id: str, outcome: str, result_sha256) -> None:
    def apply(studies):
        ledger._append(HoldoutAuditType.RETIRED, study_id, {
            "study_id": study_id, "request_id": request_id, "outcome": outcome,
            "result_sha256": result_sha256,
        })
        return None, None
    ledger._run(apply)


def evaluate_holdout(ledger: HoldoutLedger, registry: TrialRegistry, request_id: str, *, candidate,
                     preregistration, manifest, history, holdout_rows, fit, decide,
                     parent_trial_id=None) -> HoldoutReport:
    """The one final evaluation of the frozen candidate on the committed holdout rows."""
    if not callable(fit) or not callable(decide):
        raise HoldoutError("fit_and_decide_required")
    try:
        canonical_uuid(request_id)
    except CanonicalError as exc:
        raise HoldoutError("unknown_request") from exc
    study_id, request = ledger._request(request_id)
    if study_id is None:
        raise HoldoutError("unknown_request")
    try:
        if request["status"] != _GRANTED:
            raise HoldoutError("not_granted_or_already_disclosed")
        facts = _candidate(registry, study_id, candidate, preregistration, manifest, parent_trial_id)
        if (facts.frozen() != request["candidate"]
                or facts.preregistration_sha256 != request["preregistration_sha256"]
                or dict(facts.commitment) != request["commitment"]):
            raise HoldoutError("candidate_changed")
        plan = _plan(facts.config)
        layout = _layout(history, holdout_rows, plan, facts.holdout, facts.commitment)
    except HoldoutError as exc:
        ledger._deny(study_id, request_id, str(exc).split(":")[0])
        raise

    # Consume before exposure: DISCLOSED is committed before any callback sees holdout data.
    def disclose(studies):
        if studies[study_id]["requests"][request_id]["status"] != _GRANTED:
            reason = "not_granted_or_already_disclosed"
            ledger._append(HoldoutAuditType.DENIED, study_id,
                           {"study_id": study_id, "request_id": request_id, "reason": reason})
            return None, reason
        ledger._append(HoldoutAuditType.DISCLOSED, study_id, {
            "study_id": study_id, "request_id": request_id, "registry_head": facts.registry_head,
            "holdout_rows_sha256": layout.holdout_sha256, "history_rows_sha256": layout.history_sha256,
        })
        return None, None
    ledger._run(disclose)

    try:
        frozen = canonical_bytes(facts.config).decode("utf-8")
        state = fit(layout.features, layout.labels, parse_json_strict(frozen))
        try:
            state_json = canonical_bytes(state).decode("utf-8")
        except CanonicalError as exc:
            raise HoldoutError("noncanonical_fit_state") from exc
        state_hash = content_hash(_STATE_DOMAIN, parse_json_strict(state_json))
        positions, visible, i = [], layout.by_decision, 0
        for step in layout.steps:
            while i < len(visible) and visible[i].decision_utc_ns <= step.decision_utc_ns:
                i += 1
            p = decide(parse_json_strict(state_json), step.decision_utc_ns, visible[:i],
                       parse_json_strict(frozen))
            _position(p)
            positions.append(p)
        metrics = _metrics(study_id, facts, plan, layout, positions, state_hash)
        result_sha256 = content_hash(_RESULT_HASH_DOMAIN, metrics)
    except Exception:
        _retire(ledger, study_id, request_id, "FAILED", None)
        raise
    except BaseException:
        _retire(ledger, study_id, request_id, "ABORTED", None)
        raise
    _retire(ledger, study_id, request_id, "COMPLETED", result_sha256)
    return HoldoutReport(VALID, (), study_id, request_id,
                         (f"holdout_result:{result_sha256}", f"candidate:{facts.output_hash}"), metrics)


def verify_holdout(ledger: HoldoutLedger, registry: TrialRegistry, report, *, candidate,
                   preregistration, manifest, history, holdout_rows,
                   parent_trial_id=None) -> HoldoutReport:
    """Re-derive a holdout result. Evidence problems return INSUFFICIENT_EVIDENCE."""
    ledger_head = ledger.head()
    reasons: list[str] = []
    study_id = request_id = facts = None
    evidence = (f"ledger_head:{ledger_head}",)
    try:
        study_id, request_id, metrics = report.study_id, report.request_id, report.metrics
        owner, request = ledger._request(request_id)
        if (owner != study_id or request["status"] != _RETIRED or request["outcome"] != "COMPLETED"
                or request["result_sha256"] != content_hash(_RESULT_HASH_DOMAIN, metrics)):
            reasons.append("ledger_record_mismatch")
        else:
            facts = _candidate(registry, study_id, candidate, preregistration, manifest, parent_trial_id)
            if facts.frozen() != request["candidate"] or dict(facts.commitment) != request["commitment"]:
                reasons.append("candidate_changed")
            else:
                plan = _plan(facts.config)
                layout = _layout(history, holdout_rows, plan, facts.holdout, facts.commitment)
                rebuilt = _metrics(study_id, facts, plan, layout, metrics["positions"],
                                   metrics["fit_state_sha256"])
                if canonical_bytes(rebuilt) != canonical_bytes(metrics):
                    reasons.append("metrics_not_reproducible")
                evidence += (f"holdout_result:{request['result_sha256']}",
                             f"registry_head:{facts.registry_head}")
    except HoldoutError as exc:
        reasons.append(str(exc).split(":")[0])
    except (AttributeError, CanonicalError, KeyError, TypeError, RegistryError):
        reasons.append("invalid_report")
    # Re-check both heads after re-derivation: success needs unchanged state for every read.
    if ledger.head() != ledger_head:
        reasons.append("ledger_changed_during_read")
    if facts is not None and registry.verify().head_hash != facts.registry_head:
        reasons.append("registry_changed_during_read")
    if reasons:
        return HoldoutReport(INSUFFICIENT, tuple(dict.fromkeys(reasons)), study_id, request_id, evidence)
    return HoldoutReport(VALID, (), study_id, request_id, evidence, report.metrics)
