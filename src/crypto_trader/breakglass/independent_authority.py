"""Offline reference implementation of a separately retained writer authority.

This is NOT a deployed independent trust boundary. A production service must own
this state outside the journal/ownership/proof backup domain, authenticate callers,
and protect enrollment and its pinned identity. A second local file alone does
not establish that property. No method here enables real order creation.

Writer advances, journal extension checks and intent reservations commit with
SQLite WAL/FULL. A lost response is an unknown outcome, never permission to retry
a reservation. Recovery receipts may only resume the SAME emergency repair.
"""
from contextlib import closing, contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from .errors import OwnershipStateError
from .ownership import OwnerKind, OwnershipRecord


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _scope(scope):
    from crypto_trader.execution.ports import CanonicalWriterScope
    if type(scope) is not CanonicalWriterScope:
        raise OwnershipStateError("independent authority requires a canonical account scope")
    return [scope.venue, scope.environment, scope.account_id]


class SQLiteIndependentAuthority:
    """Open an explicitly provisioned authority; never auto-enroll or recreate it."""

    @staticmethod
    def provision_offline(path, *, authority_id, scope, journal):
        if type(authority_id) is not UUID or type(journal.journal_id()) is not UUID:
            raise OwnershipStateError("authority enrollment needs pinned identities")
        path = Path(path).resolve()
        if path.is_relative_to(Path(journal.path).resolve().parent):
            raise OwnershipStateError("authority must not be inside the local journal directory")
        # Exclusive creation. An existing enrollment can never be overwritten here.
        with path.open("xb"):
            pass
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
            state = dict(version=1, authority_id=str(authority_id), scope=_scope(scope),
                         journal_id=str(journal.journal_id()), writer=None,
                         revision={str(k): v for k, v in journal.revision().entries},
                         attempts={}, recoveries={})
            connection.execute("INSERT INTO state VALUES(1,?)", (_json(state),))
            connection.commit()

    def __init__(self, path, *, authority_id, scope, journal_id):
        if type(authority_id) is not UUID or type(journal_id) is not UUID:
            raise OwnershipStateError("authority needs exact pinned identities")
        self.path = Path(path).resolve()
        self.authority_id = authority_id
        self._scope = _scope(scope)
        self._journal_id = journal_id
        with self._transaction():
            pass

    @contextmanager
    def _transaction(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
            connection.execute("PRAGMA synchronous=FULL")
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                raise OwnershipStateError("independent authority lost WAL durability")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT body FROM state WHERE id=1").fetchone()
            state = json.loads(row[0])
            if (state["version"] != 1 or state["authority_id"] != str(self.authority_id)
                    or state["scope"] != self._scope or state["journal_id"] != str(self._journal_id)):
                raise OwnershipStateError("independent authority identity/scope mismatch")
            yield state
            connection.execute("UPDATE state SET body=? WHERE id=1", (_json(state),))
            connection.commit()
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            raise OwnershipStateError("independent authority unavailable or invalid") from exc
        finally:
            if connection is not None:
                connection.close()  # rolls back any uncommitted transaction

    def _journal_extension(self, state, journal):
        if journal.journal_id() != self._journal_id:
            raise OwnershipStateError("independent journal identity mismatch")
        journal.require_venue_write_ready()
        revision = {str(k): v for k, v in journal.revision().entries}
        if any(revision.get(k) != v for k, v in state["revision"].items()):
            raise OwnershipStateError("independent authority detected journal rollback or rewrite")
        return revision

    @staticmethod
    def _require_writer(state, record):
        if type(record) is not OwnershipRecord or state["writer"] != record.to_record():
            raise OwnershipStateError("independent authority fenced this writer session")

    def current_writer(self):
        with self._transaction() as state:
            return None if state["writer"] is None else OwnershipRecord.from_record(state["writer"])

    def require_current(self, record):
        with self._transaction() as state:
            self._require_writer(state, record)

    def advance_writer(self, record, *, previous):
        with self._transaction() as state:
            current = None if state["writer"] is None else OwnershipRecord.from_record(state["writer"])
            if (record.owner_kind is not OwnerKind.EMERGENCY
                    and state["writer"] != (None if previous is None else previous.to_record())):
                raise OwnershipStateError("independent writer compare-and-advance conflict")
            if (record.journal_id != self._journal_id
                    or record.term <= max(0 if previous is None else previous.term,
                                          0 if current is None else current.term)):
                raise OwnershipStateError("independent writer must advance term with an exact session binding")
            state["writer"] = record.to_record()

    def verify_journal(self, journal):
        with self._transaction() as state:
            self._journal_extension(state, journal)

    def reserve_attempt(self, record, journal, marker):
        from crypto_trader.events import OrderSubmissionAttempted
        if type(marker) is not OrderSubmissionAttempted or record.owner_kind is not OwnerKind.NORMAL:
            raise OwnershipStateError("independent reservation requires normal writer and exact marker")
        with self._transaction() as state:
            self._require_writer(state, record)
            revision = self._journal_extension(state, journal)
            if journal.read_by_id(marker.event_id) != marker:
                raise OwnershipStateError("independent reservation requires committed marker")
            intent, reference = str(marker.payload.intent_id), marker.payload.venue_reference
            if intent in state["attempts"] or any(
                    x["reference"] == reference for x in state["attempts"].values()):
                raise OwnershipStateError("independent attempt already reserved; reconcile, never resend")
            state["attempts"][intent] = dict(reference=reference, marker=str(marker.event_id),
                marker_sha256=hashlib.sha256(marker.to_json().encode()).hexdigest(),
                writer=record.to_record())
            state["revision"] = revision

    def recover_high_water(self, record, journal, *, recovery_id, expected_sha256, observed, reason):
        if (type(recovery_id) is not UUID or type(expected_sha256) is not str
                or len(expected_sha256) != 64 or any(c not in '0123456789abcdef' for c in expected_sha256)
                or type(reason) is not str or not reason.strip() or len(reason) > 1024):
            raise OwnershipStateError("explicit recovery identity, evidence digest and reason required")
        with self._transaction() as state:
            revision = self._journal_extension(state, journal)
            receipt = state["recoveries"].get(str(recovery_id))
            if receipt is not None:
                # A response/local write may have been lost. Only finish this repair;
                # never replay it over a subsequent takeover or another local record.
                target = OwnershipRecord.from_record(receipt["target"])
                if (receipt["damage"] != expected_sha256 or receipt["reason"] != reason
                        or state["writer"] != receipt["target"]
                        or record.to_record() not in (receipt["previous"], receipt["target"])
                        or observed not in (expected_sha256, hashlib.sha256(str(target.term).encode()).hexdigest())):
                    raise OwnershipStateError("recovery receipt is stale or mismatched")
                return target
            self._require_writer(state, record)
            if record.owner_kind is not OwnerKind.EMERGENCY or observed != expected_sha256:
                raise OwnershipStateError("recovery requires current emergency writer and exact damaged bytes")
            target = replace(record, term=record.term + 1, session_id=uuid4(),
                             reason_code="independent_high_water_recovery")
            state["writer"] = target.to_record()
            state["revision"] = revision
            state["recoveries"][str(recovery_id)] = dict(damage=expected_sha256, reason=reason,
                previous=record.to_record(), target=target.to_record())
            return target
