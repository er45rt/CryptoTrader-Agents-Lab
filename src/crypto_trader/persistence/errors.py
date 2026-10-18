"""Typed errors for the persistence layer (Phase 5).

Engine-independent: no Nautilus imports, no LLM imports.
"""

from __future__ import annotations

from uuid import UUID

__all__ = [
    "EventIdConflictError",
    "JournalError",
    "JournalRevisionMismatchError",
    "JournalSchemaError",
    "SnapshotError",
]


class JournalError(RuntimeError):
    """Base class for journal/storage failures."""


class EventIdConflictError(JournalError):
    """The same event_id was appended with different content."""

    def __init__(self, event_id: UUID) -> None:
        self.event_id = event_id
        super().__init__(
            f"event_id {event_id} already exists with different content; refusing to overwrite "
            "an append-only journal entry."
        )


class JournalRevisionMismatchError(JournalError):
    """The journal changed after a caller captured the revision it intended to update."""


class JournalSchemaError(JournalError):
    """The stored schema version is unknown or unsupported."""


class SnapshotError(JournalError):
    """A snapshot could not be stored or read faithfully."""
