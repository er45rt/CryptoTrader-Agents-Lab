"""Durable append-only event journal (Phase 5).

The journal is OUR application/domain history. Nautilus internal state is not the source of
truth for AI decisions, trade intents, risk decisions, validity dispositions, lifecycle audit,
or correlation history — this journal is.

Design:
* SQLite via the standard library ``sqlite3`` (no Redis/Kafka/microservices).
* WAL journalling, foreign keys on, explicit transactions.
* Append-only: ``event_id`` is the primary key; re-appending identical content is a no-op and
  never creates a duplicate row. Re-appending *different* content for the same id fails closed.
* Lossless storage: the full Phase 3 envelope JSON is stored and decoded back through the
  Phase 3 registry, so unknown or malformed events fail visibly instead of being guessed.
* Indexed for correlation, type/version, symbol/order and ordering queries.
* No pickle and no arbitrary code deserialization anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

from crypto_trader.events import EventEnvelope, EventRegistry, decode_event, get_default_registry

from .errors import EventIdConflictError, JournalRevisionMismatchError, JournalSchemaError

__all__ = [
    "JOURNAL_SCHEMA_VERSION",
    "ConditionalAppendResult",
    "EventJournal",
    "JournalRevision",
    "SQLiteEventJournal",
]

#: Schema version of the journal database itself (not of any event).
JOURNAL_SCHEMA_VERSION = 1

# SQLite header marker: CTFG (CryptoTrader FULL Genesis), not a cryptographic signature.
# Trust is limited to the storage boundary, and every send checks the active connection too.
FULL_GENESIS_APPLICATION_ID = 0x43544647

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS journal_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id          TEXT PRIMARY KEY,
    event_type        TEXT NOT NULL,
    schema_version    INTEGER NOT NULL,
    occurred_at_ns    INTEGER NOT NULL,
    occurred_at_iso   TEXT NOT NULL,
    correlation_id    TEXT NOT NULL,
    causation_id      TEXT,
    producer          TEXT NOT NULL,
    strategy_id       TEXT,
    order_id          TEXT,
    symbol            TEXT,
    sequence_number   INTEGER,
    payload_json      TEXT NOT NULL,
    payload_sha256    TEXT NOT NULL,
    ingested_at_iso   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_correlation ON events (correlation_id, occurred_at_ns);
CREATE INDEX IF NOT EXISTS idx_events_type_version ON events (event_type, schema_version);
CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events (occurred_at_ns, event_id);
CREATE INDEX IF NOT EXISTS idx_events_symbol_seq ON events (symbol, sequence_number);
CREATE INDEX IF NOT EXISTS idx_events_order ON events (order_id, occurred_at_ns);
CREATE INDEX IF NOT EXISTS idx_events_causation ON events (causation_id);
"""


@dataclass(frozen=True)
class JournalRevision:
    """Exact append-only journal revision derived from stored payload bytes."""

    entries: tuple[tuple[UUID, str], ...]
    fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise ValueError("journal revision entries must be an immutable tuple.")
        event_ids = tuple(event_id for event_id, _digest in self.entries)
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("journal revision event identities must be unique.")
        if tuple(sorted(event_ids, key=str)) != event_ids:
            raise ValueError("journal revision entries must be sorted by event_id.")
        for event_id, digest in self.entries:
            if not isinstance(event_id, UUID):
                raise ValueError("journal revision event ids must be UUID values.")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
            ):
                raise ValueError("journal revision payload digests must be lowercase SHA-256 hex.")
        if self.fingerprint != _revision_fingerprint(self.entries):
            raise ValueError("journal revision fingerprint does not match its entries.")

    @property
    def event_ids(self) -> tuple[UUID, ...]:
        return tuple(event_id for event_id, _digest in self.entries)

    @property
    def count(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class ConditionalAppendResult:
    """Evidence from one revision-checked atomic append transaction."""

    inserted: int
    before: JournalRevision
    after: JournalRevision


def _revision_fingerprint(entries: Sequence[tuple[UUID, str]]) -> str:
    material = tuple((str(event_id), digest) for event_id, digest in entries)
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@runtime_checkable
class EventJournal(Protocol):
    """The storage abstraction the application depends on (no SQLite types leak out)."""

    def append(self, event: EventEnvelope) -> bool: ...

    def append_batch(self, events: Sequence[EventEnvelope]) -> int: ...

    def revision(self) -> JournalRevision: ...

    def append_batch_if_revision(
        self,
        events: Sequence[EventEnvelope],
        *,
        expected: JournalRevision,
        require_all_new: bool = False,
    ) -> ConditionalAppendResult: ...

    def read_by_id(self, event_id: UUID) -> EventEnvelope | None: ...

    def read_by_correlation(self, correlation_id: UUID, *, limit: int | None = None) -> list[EventEnvelope]: ...

    def read_range(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[EventEnvelope]: ...

    def replay(
        self,
        *,
        after_occurred_at_ns: int | None = None,
        after_event_id: UUID | None = None,
        limit: int | None = None,
    ) -> Iterator[EventEnvelope]: ...

    def latest_sequence_number(self, *, symbol: str | None = None, order_id: UUID | None = None) -> int | None: ...

    def count(self) -> int: ...

    def close(self) -> None: ...


def _to_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise JournalSchemaError("event timestamps must be timezone-aware before journaling.")
    return int(value.timestamp() * 1_000_000_000)


class SQLiteEventJournal:
    """Durable append-only journal backed by SQLite (WAL)."""

    def __init__(
        self,
        path: str | Path,
        *,
        registry: EventRegistry | None = None,
        timeout_seconds: float = 10.0,
        venue_writes: bool = False,
        read_only: bool = False,
    ) -> None:
        self.path = str(path)
        was_new = self.path != ":memory:" and not Path(self.path).exists()
        self._registry = registry if registry is not None else get_default_registry()
        if read_only:
            self._open_read_only(timeout_seconds=timeout_seconds, venue_writes=venue_writes)
            return
        self._conn = sqlite3.connect(self.path, timeout=timeout_seconds)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=FULL;" if venue_writes else "PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        try:
            if venue_writes:
                self._ensure_full_foundation(was_new=was_new)
            else:
                self._ensure_schema()
        except Exception:
            self._conn.close()
            raise

    def _open_read_only(self, *, timeout_seconds: float, venue_writes: bool) -> None:
        """Open an existing journal for reads only: never create, migrate or repair anything."""
        if venue_writes:
            raise JournalSchemaError("a read-only journal cannot be opened for venue writes.")
        try:
            if self.path == ":memory:" or not Path(self.path).is_file():
                raise JournalSchemaError(
                    f"journal {self.path} is not an existing file; a read-only open never creates it."
                )
            # abspath, not resolve(): read the path the writer uses so both share one -wal/-shm.
            uri = Path(os.path.abspath(self.path)).as_uri() + "?mode=ro"
        except (OSError, ValueError) as exc:
            raise JournalSchemaError(
                f"journal {self.path} could not be checked read-only: {exc}"
            ) from exc
        try:
            self._conn = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
        except sqlite3.Error as exc:
            raise JournalSchemaError(
                f"journal {self.path} could not be opened read-only: {exc}"
            ) from exc
        self._conn.row_factory = sqlite3.Row
        try:
            row = self._conn.execute(
                "SELECT value FROM journal_meta WHERE key = 'schema_version'"
            ).fetchone()
            self._conn.execute("SELECT 1 FROM events LIMIT 1").fetchone()
            if row is None:
                raise JournalSchemaError(
                    "read-only journal has no schema version; refusing to open it."
                )
            if row["value"] != str(JOURNAL_SCHEMA_VERSION):
                raise JournalSchemaError(
                    f"journal schema version {row['value']} is not supported by this build "
                    f"(expected {JOURNAL_SCHEMA_VERSION}); refusing to open it."
                )
        except sqlite3.Error as exc:
            self._conn.close()
            raise JournalSchemaError(
                f"read-only journal could not be validated ({exc}); nothing was created or repaired."
            ) from exc
        except BaseException:
            self._conn.close()
            raise

    # --- schema ---

    def has_full_foundation(self) -> bool:
        return self._conn.execute("PRAGMA application_id").fetchone()[0] == FULL_GENESIS_APPLICATION_ID

    def require_venue_write_ready(self) -> None:
        """Prove founding provenance and current writer durability, without changing either."""
        if (
            not self.has_full_foundation()
            or self._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal"
            or self._conn.execute("PRAGMA synchronous").fetchone()[0] != 2
            or self._conn.execute("PRAGMA user_version").fetchone()[0] != 0
        ):
            raise JournalSchemaError("venue POST requires a FULL-founded journal and active WAL/FULL writer.")

    def _ensure_full_foundation(self, *, was_new: bool) -> None:
        if self._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            raise JournalSchemaError("venue journal requires file-backed SQLite WAL.")
        if self._conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
            raise JournalSchemaError("FULL was not established on the founding writer.")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            application = self._conn.execute("PRAGMA application_id").fetchone()[0]
            user_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if user_version != 0:
                raise JournalSchemaError("reserved SQLite user_version; refusing FULL founding.")
            if application == FULL_GENESIS_APPLICATION_ID:
                row = self._conn.execute("SELECT value FROM journal_meta WHERE key='schema_version'").fetchone()
                if row is None or int(row[0]) != JOURNAL_SCHEMA_VERSION:
                    raise JournalSchemaError("FULL-founded journal schema is missing or unsupported.")
            else:
                if (
                    not was_new or application != 0
                    or self._conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is not None
                ):
                    raise JournalSchemaError("refusing retrospective FULL founding of an existing journal.")
                # executescript implicitly commits: execute the existing static DDL statements
                # individually so header, unchanged schema and schema version commit atomically.
                for statement in _SCHEMA_SQL.split(";"):
                    if statement.strip():
                        self._conn.execute(statement)
                self._conn.execute("INSERT INTO journal_meta(key,value) VALUES('schema_version',?)",
                                   (str(JOURNAL_SCHEMA_VERSION),))
                self._conn.execute(f"PRAGMA application_id={FULL_GENESIS_APPLICATION_ID}")
            self.require_venue_write_ready()
            self._conn.commit()
        except BaseException:  # includes KeyboardInterrupt/SystemExit: never leave it open
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        self.require_venue_write_ready()

    def _ensure_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)
            row = self._conn.execute(
                "SELECT value FROM journal_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO journal_meta (key, value) VALUES ('schema_version', ?)",
                    (str(JOURNAL_SCHEMA_VERSION),),
                )
                return
            if int(row["value"]) != JOURNAL_SCHEMA_VERSION:
                raise JournalSchemaError(
                    f"journal schema version {row['value']} is not supported by this build "
                    f"(expected {JOURNAL_SCHEMA_VERSION}); refusing to open it."
                )

    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM journal_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row is not None else JOURNAL_SCHEMA_VERSION

    def journal_mode(self) -> str:
        return str(self._conn.execute("PRAGMA journal_mode;").fetchone()[0])

    def _revision_locked(self) -> JournalRevision:
        rows = self._conn.execute(
            "SELECT event_id, payload_json, payload_sha256 FROM events ORDER BY event_id ASC"
        ).fetchall()
        entries: list[tuple[UUID, str]] = []
        for row in rows:
            payload_json = str(row["payload_json"])
            actual = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            stored = str(row["payload_sha256"])
            if actual != stored:
                raise JournalSchemaError(
                    f"journal payload hash mismatch for event_id {row['event_id']}."
                )
            try:
                event_id = UUID(str(row["event_id"]))
            except (TypeError, ValueError) as exc:
                raise JournalSchemaError(
                    f"journal event_id is malformed: {row['event_id']!r}."
                ) from exc
            entries.append((event_id, actual))
        normalized = tuple(entries)
        try:
            return JournalRevision(
                entries=normalized,
                fingerprint=_revision_fingerprint(normalized),
            )
        except ValueError as exc:
            raise JournalSchemaError("journal revision metadata is malformed.") from exc

    def revision(self) -> JournalRevision:
        """Return an exact-byte revision for the current append-only journal."""
        return self._revision_locked()

    # --- writes ---

    def append(self, event: EventEnvelope) -> bool:
        """Append one event; return True when it was newly journaled, False when idempotent."""
        payload_json = event.to_json()
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        row = self._row_for(event, payload_json, digest)
        try:
            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO events (
                        event_id, event_type, schema_version, occurred_at_ns, occurred_at_iso,
                        correlation_id, causation_id, producer, strategy_id, order_id, symbol,
                        sequence_number, payload_json, payload_sha256, ingested_at_iso
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO NOTHING
                    """,
                    row,
                )
        except sqlite3.Error as exc:  # pragma: no cover - surfaced as a typed error
            raise JournalSchemaError(f"journal write failed: {exc}") from exc

        if cursor.rowcount == 1:
            return True
        existing = self._conn.execute(
            "SELECT payload_sha256 FROM events WHERE event_id = ?", (str(event.event_id),)
        ).fetchone()
        if existing is None:  # pragma: no cover - defensive
            raise JournalSchemaError("journal reported a conflict but the row is missing.")
        if existing["payload_sha256"] != digest:
            raise EventIdConflictError(event.event_id)
        return False

    def append_batch(self, events: Sequence[EventEnvelope]) -> int:
        """Append many events in ONE transaction; returns the number newly journaled.

        BEGIN IMMEDIATE serializes the conflict precheck with every SQLite writer, so no other
        writer can journal a conflicting event_id between the check and the insert.
        """
        rows = []
        digests = {}
        for event in events:
            payload_json = event.to_json()
            digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            event_id = str(event.event_id)
            if event_id in digests and digests[event_id] != digest:
                raise EventIdConflictError(event.event_id)
            digests[event_id] = digest
            rows.append(self._row_for(event, payload_json, digest))

        if not rows:
            return 0
        if self._conn.in_transaction:
            raise JournalSchemaError(
                "append batch refuses to start inside an existing transaction."
            )

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # Fail closed on conflicts BEFORE writing anything, under the same write lock.
            placeholders = ",".join("?" for _ in digests)
            existing = self._conn.execute(
                f"SELECT event_id, payload_sha256 FROM events WHERE event_id IN ({placeholders})",
                tuple(digests),
            ).fetchall()
            for record in existing:
                if record["payload_sha256"] != digests[record["event_id"]]:
                    raise EventIdConflictError(UUID(record["event_id"]))

            inserted = 0
            for row in rows:
                cursor = self._conn.execute(
                    """
                    INSERT INTO events (
                        event_id, event_type, schema_version, occurred_at_ns, occurred_at_iso,
                        correlation_id, causation_id, producer, strategy_id, order_id, symbol,
                        sequence_number, payload_json, payload_sha256, ingested_at_iso
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO NOTHING
                    """,
                    row,
                )
                inserted += cursor.rowcount
            self._conn.commit()
            return inserted
        except (EventIdConflictError, JournalSchemaError):
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        except sqlite3.Error as exc:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise JournalSchemaError(f"journal batch failed: {exc}") from exc
        except BaseException:  # includes KeyboardInterrupt/SystemExit: never leave it open
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

    def append_batch_if_revision(
        self,
        events: Sequence[EventEnvelope],
        *,
        expected: JournalRevision,
        require_all_new: bool = False,
    ) -> ConditionalAppendResult:
        """Atomically compare the exact journal revision and append one reviewed batch.

        BEGIN IMMEDIATE serializes the revision check with every SQLite writer. If the journal
        changed after planning, nothing is written. The returned after revision is computed
        inside the same transaction as the insert.
        """
        if not isinstance(expected, JournalRevision):
            raise JournalSchemaError("conditional append requires a JournalRevision.")

        rows = []
        digests: dict[str, str] = {}
        event_ids: list[str] = []
        for event in events:
            payload_json = event.to_json()
            digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            event_id = str(event.event_id)
            if event_id in digests:
                if digests[event_id] != digest:
                    raise EventIdConflictError(event.event_id)
                raise JournalSchemaError(
                    f"conditional append batch contains duplicate event_id {event_id}."
                )
            digests[event_id] = digest
            event_ids.append(event_id)
            rows.append(self._row_for(event, payload_json, digest))

        if self._conn.in_transaction:
            raise JournalSchemaError(
                "conditional append refuses to start inside an existing transaction."
            )

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            before = self._revision_locked()
            if before != expected:
                raise JournalRevisionMismatchError(
                    "journal revision changed before the conditional append."
                )

            if digests:
                placeholders = ",".join("?" for _ in digests)
                existing = self._conn.execute(
                    f"SELECT event_id, payload_sha256 FROM events WHERE event_id IN ({placeholders})",
                    tuple(event_ids),
                ).fetchall()
                for record in existing:
                    if record["payload_sha256"] != digests[record["event_id"]]:
                        raise EventIdConflictError(UUID(record["event_id"]))
                if require_all_new and existing:
                    raise JournalSchemaError(
                        "conditional append requires every event id to be new."
                    )

            inserted = 0
            for row in rows:
                cursor = self._conn.execute(
                    """
                    INSERT INTO events (
                        event_id, event_type, schema_version, occurred_at_ns, occurred_at_iso,
                        correlation_id, causation_id, producer, strategy_id, order_id, symbol,
                        sequence_number, payload_json, payload_sha256, ingested_at_iso
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO NOTHING
                    """,
                    row,
                )
                inserted += cursor.rowcount

            after = self._revision_locked()
            self._conn.commit()
            return ConditionalAppendResult(inserted=inserted, before=before, after=after)
        except (JournalRevisionMismatchError, EventIdConflictError, JournalSchemaError):
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        except sqlite3.Error as exc:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise JournalSchemaError(f"conditional journal batch failed: {exc}") from exc
        except BaseException:  # includes KeyboardInterrupt/SystemExit: never leave it open
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

    def _row_for(self, event: EventEnvelope, payload_json: str, digest: str) -> tuple:
        return (
            str(event.event_id),
            event.event_type,
            int(event.schema_version),
            _to_ns(event.occurred_at),
            event.occurred_at.astimezone(timezone.utc).isoformat(),
            str(event.correlation_id),
            None if event.causation_id is None else str(event.causation_id),
            event.producer,
            event.strategy_id,
            None if event.order_id is None else str(event.order_id),
            event.symbol,
            event.sequence_number,
            payload_json,
            digest,
            datetime.now(timezone.utc).isoformat(),
        )

    # --- reads ---

    def _decode(self, row: sqlite3.Row) -> EventEnvelope:
        """Decode through the Phase 3 registry so unknown/malformed events fail visibly."""
        return decode_event(row["payload_json"], registry=self._registry)

    def read_by_id(self, event_id: UUID) -> EventEnvelope | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (str(event_id),)
        ).fetchone()
        return None if row is None else self._decode(row)

    def read_by_correlation(self, correlation_id: UUID, *, limit: int | None = None) -> list[EventEnvelope]:
        sql = (
            "SELECT * FROM events WHERE correlation_id = ? "
            "ORDER BY occurred_at_ns ASC, COALESCE(sequence_number, 0) ASC, event_id ASC"
        )
        params: tuple = (str(correlation_id),)
        if limit is not None:
            sql += " LIMIT ?"
            params = params + (int(limit),)
        return [self._decode(row) for row in self._conn.execute(sql, params).fetchall()]

    def read_range(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[EventEnvelope]:
        clauses, params = [], []
        if start is not None:
            clauses.append("occurred_at_ns >= ?")
            params.append(_to_ns(start))
        if end is not None:
            clauses.append("occurred_at_ns <= ?")
            params.append(_to_ns(end))
        sql = "SELECT * FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY occurred_at_ns ASC, COALESCE(sequence_number, 0) ASC, event_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._decode(row) for row in self._conn.execute(sql, tuple(params)).fetchall()]

    def replay(
        self,
        *,
        after_occurred_at_ns: int | None = None,
        after_event_id: UUID | None = None,
        limit: int | None = None,
    ) -> Iterator[EventEnvelope]:
        """Yield events in deterministic order, optionally after a snapshot position."""
        clauses, params = [], []
        if after_occurred_at_ns is not None:
            clauses.append("(occurred_at_ns > ? OR (occurred_at_ns = ? AND event_id > ?))")
            params.extend([int(after_occurred_at_ns), int(after_occurred_at_ns), str(after_event_id or "")])
        sql = "SELECT * FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY occurred_at_ns ASC, COALESCE(sequence_number, 0) ASC, event_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        for row in self._conn.execute(sql, tuple(params)):
            yield self._decode(row)

    def latest_sequence_number(self, *, symbol: str | None = None, order_id: UUID | None = None) -> int | None:
        clauses, params = ["sequence_number IS NOT NULL"], []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if order_id is not None:
            clauses.append("order_id = ?")
            params.append(str(order_id))
        sql = "SELECT MAX(sequence_number) AS latest FROM events WHERE " + " AND ".join(clauses)
        row = self._conn.execute(sql, tuple(params)).fetchone()
        return None if row is None or row["latest"] is None else int(row["latest"])

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteEventJournal":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
