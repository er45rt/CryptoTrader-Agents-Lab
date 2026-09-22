"""Snapshot storage for derived application state (Phase 5).

Snapshots are an ACCELERATION only: the journal stays the authoritative history. Every
snapshot records the position it was derived from (last applied event id, its ordering key,
and the last sequence number) so a rebuild can replay only the tail.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from .errors import SnapshotError

__all__ = ["SNAPSHOT_SCHEMA_VERSION", "SnapshotRecord", "SnapshotStore"]

SNAPSHOT_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    snapshot_schema       INTEGER NOT NULL,
    state_json            TEXT NOT NULL,
    last_event_id         TEXT,
    last_occurred_at_ns   INTEGER,
    last_sequence_number  INTEGER,
    created_at_iso        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_name ON snapshots (name, snapshot_id DESC);
"""


@dataclass(frozen=True)
class SnapshotRecord:
    """A snapshot plus the exact journal position it represents."""

    name: str
    snapshot_schema: int
    state: dict[str, Any]
    last_event_id: UUID | None
    last_occurred_at_ns: int | None
    last_sequence_number: int | None
    created_at_iso: str


class SnapshotStore:
    """SQLite-backed snapshot store (JSON state only; never pickle)."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 10.0) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, timeout=timeout_seconds)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

    def save(
        self,
        name: str,
        state: Mapping[str, Any],
        *,
        last_event_id: UUID | None = None,
        last_occurred_at_ns: int | None = None,
        last_sequence_number: int | None = None,
    ) -> SnapshotRecord:
        """Store a snapshot of derived state at a recorded journal position."""
        if not name:
            raise SnapshotError("snapshot name must not be empty.")
        try:
            state_json = json.dumps(dict(state), sort_keys=True, default=str)
        except (TypeError, ValueError) as exc:
            raise SnapshotError(f"snapshot state is not JSON-serializable: {exc}") from exc

        created_at = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO snapshots (
                    name, snapshot_schema, state_json, last_event_id,
                    last_occurred_at_ns, last_sequence_number, created_at_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    SNAPSHOT_SCHEMA_VERSION,
                    state_json,
                    None if last_event_id is None else str(last_event_id),
                    last_occurred_at_ns,
                    last_sequence_number,
                    created_at,
                ),
            )
        return SnapshotRecord(
            name=name,
            snapshot_schema=SNAPSHOT_SCHEMA_VERSION,
            state=json.loads(state_json),
            last_event_id=last_event_id,
            last_occurred_at_ns=last_occurred_at_ns,
            last_sequence_number=last_sequence_number,
            created_at_iso=created_at,
        )

    def latest(self, name: str) -> SnapshotRecord | None:
        row = self._conn.execute(
            "SELECT * FROM snapshots WHERE name = ? ORDER BY snapshot_id DESC LIMIT 1", (name,)
        ).fetchone()
        if row is None:
            return None
        if int(row["snapshot_schema"]) != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotError(
                f"snapshot schema {row['snapshot_schema']} is not supported "
                f"(expected {SNAPSHOT_SCHEMA_VERSION})."
            )
        return SnapshotRecord(
            name=str(row["name"]),
            snapshot_schema=int(row["snapshot_schema"]),
            state=json.loads(row["state_json"]),
            last_event_id=None if row["last_event_id"] is None else UUID(row["last_event_id"]),
            last_occurred_at_ns=row["last_occurred_at_ns"],
            last_sequence_number=row["last_sequence_number"],
            created_at_iso=str(row["created_at_iso"]),
        )

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS c FROM snapshots").fetchone()["c"])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SnapshotStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
