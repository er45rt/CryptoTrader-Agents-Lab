"""Durable synthetic-only attempt accounting. Does NOT run strategies or trust local anchors.

SQLite WAL/FULL, one serialized writer, BEGIN IMMEDIATE and append-only hash-linked
evidence. Local administrator rollback cannot be detected without external custody;
therefore all production verification reports say UNANCHORED, never trustworthy GO.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import hmac
from pathlib import Path
from threading import RLock
from time import time_ns
from typing import Mapping
from uuid import uuid4
import secrets
import sqlite3

from .canonical import (
    CanonicalError, canonical_bytes, canonical_uuid, content_hash,
    input_manifest, parse_json_strict, sha256_text, _nfc,
)
from .contracts import RegistryError, StudySpec, TrialReceipt, TrialStatus, Verification

_ALLOWED_TERMINALS = frozenset({
    "REJECTED_PRECHECK", "FAILED", "ABORTED", "COMPLETED", "INTERRUPTED_UNRESOLVED",
})
_STREAMS = frozenset({"fold", "simulator", "bootstrap", "model"})
_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, study_id TEXT NOT NULL,
    payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studies (
    study_id TEXT PRIMARY KEY, spec_json TEXT NOT NULL,
    study_salt TEXT NOT NULL, n_max INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    trial_id TEXT PRIMARY KEY, study_id TEXT NOT NULL, status TEXT NOT NULL,
    seed_hex TEXT NOT NULL, binding_hash TEXT NOT NULL,
    research_label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS amendments (
    study_id TEXT NOT NULL, seq INTEGER NOT NULL, note TEXT NOT NULL,
    post_hoc INTEGER NOT NULL, requested_n_max INTEGER,
    PRIMARY KEY(study_id, seq)
);
"""
_GENESIS = "0" * 64


def _stamp() -> str:
    ns = time_ns()
    dt = datetime.fromtimestamp(ns // 1_000_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ns % 1_000_000_000:09d}Z"


def _reject_secrets(value):
    """Best-effort schema gate, not a production secret scanner."""
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalError("configuration object keys must be strings")
            if any(x in k.lower() for x in ("api_key", "apikey", "password", "credential", "private_key", "access_token", "secret")):
                raise CanonicalError("configuration references a credential field")
            _reject_secrets(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secrets(item)


class TrialRegistry:
    """Record attempts only; no callback, dataset, model or execution entry point.

    The caller must NOT start actual research from a Stage-1 receipt. Independent
    external reservation receipts and OS sandbox are absent by design.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = RLock()
        self._db = sqlite3.connect(self.path, isolation_level=None, timeout=10.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            mode = self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RegistryError("WAL journal mode unavailable")
            self._db.execute("PRAGMA synchronous=FULL")
            if self._db.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise RegistryError("FULL synchronous durability unavailable")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(_DB_SCHEMA)
            # Opening a second connection must NEVER finish another owner's STARTED.
            # Recovery requires a distinct confirmed operator action.
            self._snapshot(lambda: None)
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

    def recover_interrupted(self, *, operator_confirms_no_active_writer: bool) -> int:
        """Explicit, high-trust recovery. Never infer process death from a DB open.

        Human confirmation must be based on out-of-band ownership checks; a
        boolean is NOT cryptographic proof of exclusive process ownership.
        This Stage-1 registry contains no strategy runner or process lease.
        """
        if operator_confirms_no_active_writer is not True:
            raise RegistryError("confirm externally that no active trial owner remains")

        def recover():
            live = self._db.execute(
                "SELECT trial_id, study_id FROM attempts WHERE status='STARTED' ORDER BY rowid"
            ).fetchall()
            for row in live:
                self._append("FINISH", row["study_id"], {
                    "trial_id": row["trial_id"],
                    "status": "INTERRUPTED_UNRESOLVED",
                    "reason": "operator_confirmed_no_active_trial_owner",
                    "output_hash": None,
                })
                self._db.execute(
                    "UPDATE attempts SET status=? WHERE trial_id=?",
                    ("INTERRUPTED_UNRESOLVED", row["trial_id"]),
                )
            return len(live)

        return self._transact(recover)

    def _snapshot(self, read):
        """Verify journal and read its indexes at one consistent SQLite snapshot."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._verify_locked()
                result = read()
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _append(self, kind: str, study_id: str, data: dict) -> None:
        last = self._db.execute("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, _GENESIS)
        payload = canonical_bytes({"at": _stamp(), "data": data}).decode("utf-8")
        digest = content_hash("ledger-event-v1", {
            "seq": seq, "kind": kind, "study_id": study_id,
            "payload": payload, "prev_hash": prev,
        })
        self._db.execute(
            "INSERT INTO events(seq,kind,study_id,payload,prev_hash,hash) VALUES (?,?,?,?,?,?)",
            (seq, kind, study_id, payload, prev, digest),
        )

    def _verify_locked(self) -> tuple[str, int]:
        rows = self._db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        prev = _GENESIS
        study_replay: dict[str, tuple[str, str, int]] = {}
        trials: dict[str, tuple[str, str, str, str, str]] = {}
        amendments: list[tuple[str, int, str, int, int | None]] = []
        for expected_seq, row in enumerate(rows, 1):
            try:
                if row["seq"] != expected_seq or row["prev_hash"] != prev:
                    raise RegistryError("integrity: broken event sequence/chain")
                payload = parse_json_strict(row["payload"])
                if canonical_bytes(payload).decode("utf-8") != row["payload"]:
                    raise RegistryError("integrity: noncanonical event payload")
                expected = content_hash("ledger-event-v1", {
                    "seq": expected_seq, "kind": row["kind"], "study_id": row["study_id"],
                    "payload": row["payload"], "prev_hash": prev,
                })
                if row["hash"] != expected:
                    raise RegistryError("integrity: event hash mismatch")
                data = payload["data"]
                kind, sid = row["kind"], row["study_id"]
                if kind == "STUDY_REGISTERED":
                    if sid in study_replay:
                        raise RegistryError("integrity: duplicate study")
                    study_replay[sid] = (
                        canonical_bytes(data["spec"]).decode("utf-8"),
                        data["salt"], data["spec"]["n_max"],
                    )
                elif kind == "STARTED":
                    trial = data["trial_id"]
                    if trial in trials or sid not in study_replay:
                        raise RegistryError("integrity: duplicate/unknown attempt")
                    trials[trial] = (
                        sid, "STARTED", data["seed_hex"],
                        data["binding_hash"], data["research_label"],
                    )
                elif kind == "FINISH":
                    trial = data["trial_id"]
                    if trial not in trials or trials[trial][0] != sid or trials[trial][1] != "STARTED":
                        raise RegistryError("integrity: invalid finish transition")
                    if data["status"] not in _ALLOWED_TERMINALS:
                        raise RegistryError("integrity: invalid terminal status")
                    old = trials[trial]
                    trials[trial] = (sid, data["status"], old[2], old[3], old[4])
                elif kind == "AMENDMENT_PROPOSED":
                    if sid not in study_replay:
                        raise RegistryError("integrity: amendment with unknown study")
                    amendments.append((sid, expected_seq, data["note"], int(data["post_hoc"]), data["requested_n_max"]))
                elif kind == "UNALLOCATED_ADMISSION_REFUSED":
                    if sid not in study_replay:
                        raise RegistryError("integrity: invalid refusal owner")
                else:
                    raise RegistryError("integrity: unknown ledger event")
                prev = row["hash"]
            except (CanonicalError, KeyError, TypeError, ValueError, IndexError) as exc:
                raise RegistryError("integrity: malformed journal") from exc
        actual_studies = {
            row["study_id"]: (row["spec_json"], row["study_salt"], row["n_max"])
            for row in self._db.execute("SELECT * FROM studies")
        }
        actual_trials = {
            row["trial_id"]: (row["study_id"], row["status"], row["seed_hex"], row["binding_hash"], row["research_label"])
            for row in self._db.execute("SELECT * FROM attempts")
        }
        actual_amendments = [
            (row["study_id"], row["seq"], row["note"], row["post_hoc"], row["requested_n_max"])
            for row in self._db.execute("SELECT * FROM amendments ORDER BY seq")
        ]
        if study_replay != actual_studies or trials != actual_trials or amendments != actual_amendments:
            raise RegistryError("integrity: registry index differs from hash-linked journal")
        return prev, len(rows)

    def verify(self) -> Verification:
        def read():
            row = self._db.execute("SELECT hash,seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            return Verification(
                status="UNANCHORED",
                head_hash=row["hash"] if row else _GENESIS,
                event_count=row["seq"] if row else 0,
                anchored=False,
            )
        return self._snapshot(read)

    def _transact(self, action):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._verify_locked()
                result = action()
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def register_study(self, spec: StudySpec):
        if not isinstance(spec, StudySpec):
            raise RegistryError("typed StudySpec required")
        def apply():
            if self._db.execute("SELECT 1 FROM studies WHERE study_id=?", (spec.study_id,)).fetchone():
                raise RegistryError("study already registered")
            body = asdict(spec)
            # NONSECRET deterministic derivation tied to immutable study identity.
            # No claim of secret seed protection or cross-DB attempt enforcement.
            salt = content_hash("study-salt-v1", body)
            self._append("STUDY_REGISTERED", spec.study_id, {"spec": body, "salt": salt})
            self._db.execute("INSERT INTO studies VALUES (?,?,?,?)", (
                spec.study_id, canonical_bytes(body).decode("utf-8"), salt, spec.n_max,
            ))
        return self._transact(apply)

    def _refusal(self, study_id: str, reason: str):
        self._append("UNALLOCATED_ADMISSION_REFUSED", study_id, {"reason": reason})

    def allocate(
        self, study_id: str, config: Mapping[str, object], manifest: Mapping[str, str],
        *, parent_trial_id: str | None = None, requested_seed: str | None = None,
        preregistration_hash: str | None = None,
    ) -> TrialReceipt:
        canonical_uuid(study_id)
        def apply():
            row = self._db.execute("SELECT * FROM studies WHERE study_id=?", (study_id,)).fetchone()
            if row is None:
                raise RegistryError("unknown study; allocation refused")
            stored_spec = parse_json_strict(row["spec_json"])
            stored_spec["allowed_paths"] = tuple(stored_spec["allowed_paths"])
            spec = StudySpec(**stored_spec)
            try:
                if requested_seed is not None:
                    raise CanonicalError("caller seed forbidden")
                if preregistration_hash is not None and sha256_text(preregistration_hash) != spec.preregistration_hash:
                    raise CanonicalError("preregistration mismatch")
                if not isinstance(config, Mapping) or not config:
                    raise CanonicalError("nonempty candidate configuration required")
                supplied_config = dict(config)
                # Bound type, nesting and Unicode BEFORE any recursive key walk.
                cleaned = canonical_bytes(supplied_config).decode("utf-8")
                _reject_secrets(parse_json_strict(cleaned))
                approved = input_manifest(manifest, allowed_paths=spec.allowed_paths)
                if parent_trial_id is not None:
                    canonical_uuid(parent_trial_id)
                    if self._db.execute("SELECT 1 FROM attempts WHERE trial_id=? AND study_id=?", (parent_trial_id, study_id)).fetchone() is None:
                        raise CanonicalError("unknown parent trial")
                taken = self._db.execute("SELECT COUNT(*) FROM attempts WHERE study_id=?", (study_id,)).fetchone()[0]
                if taken >= spec.n_max:
                    self._refusal(study_id, "CAPACITY_EXHAUSTED")
                    return None, "capacity exhausted"
            except (CanonicalError, TypeError, ValueError) as exc:
                self._refusal(study_id, "INVALID_BINDING")
                return None, "binding or seed refused"
            trial_id = str(uuid4())
            binding = content_hash("trial-binding-v1", {
                "study_id": study_id, "preregistration_hash": spec.preregistration_hash,
                "serializer_id": spec.serializer_id, "config": parse_json_strict(cleaned),
                "manifest": approved, "parent": parent_trial_id,
            })
            seed = hmac.new(bytes.fromhex(row["study_salt"]), ("trial-seed-v1:" + trial_id).encode("ascii"), "sha256").hexdigest()
            label = "EXPLORATORY" if spec.cutoff == "UNKNOWN" else "CUTOFF_DECLARED"
            self._append("STARTED", study_id, {
                "trial_id": trial_id, "seed_hex": seed, "binding_hash": binding,
                "research_label": label, "parent_trial_id": parent_trial_id,
                "preregistration_hash": spec.preregistration_hash,
                "serializer_id": spec.serializer_id,
            })
            self._db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?)", (
                trial_id, study_id, "STARTED", seed, binding, label,
            ))
            return TrialReceipt(trial_id, study_id, "STARTED", seed, label, binding), None
        receipt, reason = self._transact(apply)
        if receipt is None:
            raise RegistryError(reason)
        return receipt

    def count_allocated(self, study_id: str) -> int:
        return self._snapshot(lambda: self._db.execute(
            "SELECT COUNT(*) FROM attempts WHERE study_id=?", (study_id,)
        ).fetchone()[0])

    def count_refusals(self, study_id: str) -> int:
        return self._snapshot(lambda: self._db.execute(
            "SELECT COUNT(*) FROM events WHERE study_id=? AND kind='UNALLOCATED_ADMISSION_REFUSED'",
            (study_id,),
        ).fetchone()[0])

    def get_status(self, trial_id: str) -> str:
        canonical_uuid(trial_id)
        def read():
            row = self._db.execute(
                "SELECT status FROM attempts WHERE trial_id=?", (trial_id,)
            ).fetchone()
            if row is None:
                raise RegistryError("unknown trial")
            return row["status"]
        return self._snapshot(read)

    def finish(self, trial_id: str, status: str, *, reason: str | None = None, output_hash: str | None = None):
        canonical_uuid(trial_id)
        if status not in _ALLOWED_TERMINALS or status == "INTERRUPTED_UNRESOLVED":
            raise RegistryError("invalid caller terminal status")
        if status == "COMPLETED":
            try:
                sha256_text(output_hash)
            except CanonicalError as exc:
                raise RegistryError("completed trial requires output SHA256") from exc
        if reason is not None:
            _nfc(reason)
        def apply():
            row = self._db.execute("SELECT study_id,status FROM attempts WHERE trial_id=?", (trial_id,)).fetchone()
            if row is None or row["status"] != "STARTED":
                raise RegistryError("trial already terminal or absent")
            payload = {"trial_id": trial_id, "status": status, "reason": reason, "output_hash": output_hash}
            self._append("FINISH", row["study_id"], payload)
            self._db.execute("UPDATE attempts SET status=? WHERE trial_id=?", (status, trial_id))
        return self._transact(apply)

    def derive_stream(self, trial_id: str, stream: str) -> str:
        canonical_uuid(trial_id)
        if stream not in _STREAMS:
            raise RegistryError("unsupported RNG stream")
        def read():
            row = self._db.execute(
                "SELECT seed_hex FROM attempts WHERE trial_id=?", (trial_id,)
            ).fetchone()
            if row is None:
                raise RegistryError("unknown trial")
            return hmac.new(
                bytes.fromhex(row["seed_hex"]), ("stream-v1:" + stream).encode("ascii"),
                "sha256",
            ).hexdigest()
        return self._snapshot(read)

    def amend_study(self, study_id: str, note: str, *, requested_n_max: int | None = None):
        canonical_uuid(study_id)
        _nfc(note)
        if not note or (requested_n_max is not None and (type(requested_n_max) is not int or requested_n_max < 1)):
            raise RegistryError("invalid amendment")
        def apply():
            if self._db.execute("SELECT 1 FROM studies WHERE study_id=?", (study_id,)).fetchone() is None:
                raise RegistryError("unknown study")
            # Even failed/aborted attempts may expose information to the researcher.
            post = bool(self._db.execute(
                "SELECT 1 FROM attempts WHERE study_id=? LIMIT 1", (study_id,)
            ).fetchone())
            self._append("AMENDMENT_PROPOSED", study_id, {
                "note": note, "post_hoc": post, "requested_n_max": requested_n_max,
            })
            seq = self._db.execute("SELECT MAX(seq) FROM events").fetchone()[0]
            self._db.execute("INSERT INTO amendments VALUES (?,?,?,?,?)", (study_id, seq, note, int(post), requested_n_max))
        return self._transact(apply)

    def study_amendments(self, study_id: str) -> list[dict]:
        return self._snapshot(lambda: [
            {
                "note": x["note"],
                "post_hoc": bool(x["post_hoc"]),
                "requested_n_max": x["requested_n_max"],
            }
            for x in self._db.execute(
                "SELECT * FROM amendments WHERE study_id=? ORDER BY seq", (study_id,)
            )
        ])
