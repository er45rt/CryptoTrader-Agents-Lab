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
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

from crypto_trader.events import EventEnvelope, EventRegistry, decode_event, get_default_registry

from .errors import EventIdConflictError, JournalSchemaError

__all__ = [
    "JOURNAL_SCHEMA_VERSION",
    "EventJournal",
    "SQLiteEventJournal",
]

#: Schema version of the journal database itself (not of any event).
JOURNAL_SCHEMA_VERSION = 1

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


@runtime_checkable
class EventJournal(Protocol):
    """The storage abstraction the application depends on (no SQLite types leak out)."""

    def append(self, event: EventEnvelope) -> bool: ...

    def append_batch(self, events: Sequence[EventEnvelope]) -> int: ...

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
    ) -> None:
        self.path = str(path)
        self._registry = registry if registry is not None else get_default_registry()
        self._conn = sqlite3.connect(self.path, timeout=timeout_seconds)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._ensure_schema()

    # --- schema ---

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
        """Append many events in ONE transaction; returns the number newly journaled."""
        rows = []
        digests = {}
        for event in events:
            payload_json = event.to_json()
            digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            digests[str(event.event_id)] = digest
            rows.append(self._row_for(event, payload_json, digest))

        # Fail closed on conflicts BEFORE writing anything.
        if digests:
            placeholders = ",".join("?" for _ in digests)
            existing = self._conn.execute(
                f"SELECT event_id, payload_sha256 FROM events WHERE event_id IN ({placeholders})",
                tuple(digests),
            ).fetchall()
            for record in existing:
                if record["payload_sha256"] != digests[record["event_id"]]:
                    raise EventIdConflictError(UUID(record["event_id"]))

        inserted = 0
        try:
            with self._conn:
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
        except sqlite3.Error as exc:  # pragma: no cover - transaction rolled back above
            raise JournalSchemaError(f"journal batch failed: {exc}") from exc
        return inserted

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
