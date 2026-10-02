"""Persistent application/domain state: event journal, snapshots and replay (Phase 5).

Engine-independent by design: this package never imports Nautilus or the LLM layer. Nautilus
internal state is not the source of truth for AI decisions, trade intents, risk decisions,
validity dispositions, lifecycle audit, or correlation history — the journal is.
"""

from .errors import (
    EventIdConflictError,
    JournalError,
    JournalRevisionMismatchError,
    JournalSchemaError,
    SnapshotError,
)
from .journal import (
    JOURNAL_SCHEMA_VERSION,
    ConditionalAppendResult,
    EventJournal,
    JournalRevision,
    SQLiteEventJournal,
)
from .replay import IdempotentConsumer, RebuildSummary, rebuild_state
from .snapshots import SNAPSHOT_SCHEMA_VERSION, SnapshotRecord, SnapshotStore

__all__ = [
    "JOURNAL_SCHEMA_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "ConditionalAppendResult",
    "EventIdConflictError",
    "EventJournal",
    "IdempotentConsumer",
    "JournalError",
    "JournalRevision",
    "JournalRevisionMismatchError",
    "JournalSchemaError",
    "RebuildSummary",
    "SQLiteEventJournal",
    "SnapshotError",
    "SnapshotRecord",
    "SnapshotStore",
    "rebuild_state",
]
