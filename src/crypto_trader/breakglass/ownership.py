"""Writer ownership, fencing and takeover semantics for the break-glass path (Phase 13).

There is exactly ONE authoritative mutable trading-state writer at any moment. The record below is
that single writer's lease, and its ``term`` is a monotonic fencing token:

* during normal operation the runtime holds a ``normal`` lease;
* an operator break-glass action performs an explicit **takeover**: the term is incremented and the
  lease changes to ``emergency``, so every token minted before the takeover is stale;
* a writer whose token is stale cannot write: :meth:`WriterFence.require_current` raises
  :class:`StaleWriterFenceError`. That is what makes "no concurrent normal-writer + emergency-writer
  behaviour" mechanically true rather than a convention;
* returning to ``normal`` is itself an explicit takeover back to a ``normal`` lease with a fresh
  term - it is never implicit, and never happens because a command or a restart "looked idle".

The store is a single small JSON file written atomically. A corrupt/unreadable record fails CLOSED
(:class:`OwnershipStateError`): no writer may trade, and the error is surfaced rather than silently
corrected. An absent record means "no writer", which also refuses writes.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .errors import BreakGlassConfigError, OwnershipStateError, StaleWriterFenceError

__all__ = [
    "OWNERSHIP_SCHEMA_VERSION",
    "OwnerKind",
    "OwnershipRecord",
    "OwnershipStore",
    "WriterFence",
    "WriterGuard",
]

#: Schema version of the persisted ownership record (not of any event).
OWNERSHIP_SCHEMA_VERSION = 1


class OwnerKind(str, Enum):
    """Who currently holds the authoritative trading-state writer lease."""

    NORMAL = "normal"
    EMERGENCY = "emergency"


@dataclass(frozen=True)
class WriterFence:
    """The fencing token a writer must present before mutating trading state."""

    term: int
    owner_id: str
    owner_kind: OwnerKind

    def __post_init__(self) -> None:
        if not isinstance(self.term, int) or self.term < 0:
            raise OwnershipStateError("a writer fence requires a non-negative integer term.")
        if not isinstance(self.owner_id, str) or not self.owner_id.strip():
            raise OwnershipStateError("a writer fence requires a non-empty owner id.")
        if not isinstance(self.owner_kind, OwnerKind):
            raise OwnershipStateError("a writer fence requires an OwnerKind.")

    @property
    def allows_new_risk(self) -> bool:
        """Only the normal owner may create new risk, and only while its term is current."""
        return self.owner_kind is OwnerKind.NORMAL


@dataclass(frozen=True)
class OwnershipRecord:
    """The durable, single-writer ownership record."""

    term: int
    owner_id: str
    owner_kind: OwnerKind
    acquired_at: datetime
    previous_owner_id: str | None
    reason_code: str
    session_id: UUID
    schema_version: int = OWNERSHIP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OWNERSHIP_SCHEMA_VERSION:
            raise OwnershipStateError(
                f"unsupported ownership schema {self.schema_version}; refusing to guess."
            )
        if not isinstance(self.term, int) or self.term < 1:
            raise OwnershipStateError("an ownership record requires term >= 1.")
        if not isinstance(self.owner_id, str) or not self.owner_id.strip():
            raise OwnershipStateError("an ownership record requires a non-empty owner_id.")
        if not isinstance(self.owner_kind, OwnerKind):
            raise OwnershipStateError("an ownership record requires an OwnerKind.")
        if not isinstance(self.reason_code, str) or not self.reason_code.strip():
            raise OwnershipStateError("an ownership record requires a reason code.")
        if self.acquired_at.tzinfo is None or self.acquired_at.utcoffset() is None:
            raise OwnershipStateError("acquired_at must be timezone-aware.")

    @property
    def fence(self) -> WriterFence:
        return WriterFence(term=self.term, owner_id=self.owner_id, owner_kind=self.owner_kind)

    def to_record(self) -> dict[str, Any]:
        """A JSON-able, secret-free record."""
        return {
            "schema_version": self.schema_version,
            "term": self.term,
            "owner_id": self.owner_id,
            "owner_kind": self.owner_kind.value,
            "acquired_at": self.acquired_at.astimezone(timezone.utc).isoformat(),
            "previous_owner_id": self.previous_owner_id,
            "reason_code": self.reason_code,
            "session_id": str(self.session_id),
        }

    @classmethod
    def from_record(cls, payload: Mapping[str, Any]) -> "OwnershipRecord":
        """Rebuild from persisted JSON, failing closed on anything unexpected."""
        try:
            if not isinstance(payload, Mapping):
                raise ValueError("ownership record must be a mapping")
            record = cls(
                schema_version=int(payload["schema_version"]),
                term=int(payload["term"]),
                owner_id=str(payload["owner_id"]),
                owner_kind=OwnerKind(str(payload["owner_kind"])),
                acquired_at=datetime.fromisoformat(str(payload["acquired_at"])),
                previous_owner_id=(
                    None if payload.get("previous_owner_id") is None
                    else str(payload["previous_owner_id"])
                ),
                reason_code=str(payload["reason_code"]),
                session_id=UUID(str(payload["session_id"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OwnershipStateError(f"the ownership record is not usable: {exc}") from exc
        if record.acquired_at.tzinfo is None or record.acquired_at.utcoffset() is None:
            raise OwnershipStateError("the persisted ownership record has a naive timestamp.")
        return record


class OwnershipStore:
    """Single-writer ownership/fencing store backed by one atomically written JSON file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if not self._path.name:
            raise BreakGlassConfigError("the ownership store requires an explicit file path.")

    @property
    def path(self) -> Path:
        return self._path

    # --- reads ---

    def read(self) -> OwnershipRecord | None:
        """Return the current record, ``None`` when absent, or fail closed when unusable."""
        if not self._path.exists():
            return None
        try:
            raw = self._path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise OwnershipStateError(f"the ownership record could not be read: {exc}") from exc
        return OwnershipRecord.from_record(payload)

    def current_fence(self) -> WriterFence | None:
        record = self.read()
        return None if record is None else record.fence

    def next_term(self) -> int:
        """The fencing term a takeover would mint NOW (read-only; used for atomic recordings)."""
        record = self.read()
        return 1 if record is None else record.term + 1

    def require_fence(self) -> WriterFence:
        """Return the current fence or fail closed (absent or unusable ownership = no writer)."""
        fence = self.current_fence()
        if fence is None:
            raise OwnershipStateError(
                "no writer owns the trading-state lease; writes are refused until a writer claims "
                "ownership explicitly."
            )
        return fence

    def require_current(self, fence: WriterFence) -> OwnershipRecord:
        """Validate a writer's token against the current record (the fencing check)."""
        if not isinstance(fence, WriterFence):
            raise OwnershipStateError("a WriterFence is required.")
        record = self.read()
        if record is None:
            raise StaleWriterFenceError(
                "no ownership record exists; the presented writer fence cannot be current."
            )
        if (
            record.term != fence.term
            or record.owner_id != fence.owner_id
            or record.owner_kind is not fence.owner_kind
        ):
            raise StaleWriterFenceError(
                f"writer fence term {fence.term} owned by {fence.owner_id!r} "
                f"({fence.owner_kind.value}) is stale: the trading-state lease is held by term "
                f"{record.term} owned by {record.owner_id!r} ({record.owner_kind.value})."
            )
        return record

    # --- writes (explicit takeover only) ---

    def takeover(
        self,
        *,
        owner_id: str,
        owner_kind: OwnerKind,
        acquired_at: datetime,
        reason_code: str,
        session_id: UUID | None = None,
    ) -> OwnershipRecord:
        """Explicitly take the lease over and mint a NEW, higher fencing term.

        Every token minted before this call becomes stale, which is exactly how the normal writer is
        prevented from trading concurrently with emergency control.
        """
        if owner_kind not in (OwnerKind.NORMAL, OwnerKind.EMERGENCY):
            raise OwnershipStateError("takeover requires an OwnerKind.")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise OwnershipStateError("takeover requires a non-empty owner id.")
        if acquired_at.tzinfo is None or acquired_at.utcoffset() is None:
            raise OwnershipStateError("acquired_at must be timezone-aware.")
        previous = self.read()
        previous_owner = None if previous is None else previous.owner_id
        term = 1 if previous is None else previous.term + 1
        record = OwnershipRecord(
            term=term,
            owner_id=owner_id,
            owner_kind=owner_kind,
            acquired_at=acquired_at.astimezone(timezone.utc),
            previous_owner_id=previous_owner,
            reason_code=reason_code,
            session_id=uuid4() if session_id is None else session_id,
        )
        self._write(record)
        return record

    def _write(self, record: OwnershipRecord) -> None:
        payload = json.dumps(record.to_record(), indent=2, sort_keys=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=self._path.name, suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except OSError as exc:
            raise OwnershipStateError(f"the ownership record could not be written: {exc}") from exc
        finally:
            if os.path.exists(temporary):  # pragma: no cover - only on a failed replace
                os.unlink(temporary)


class WriterGuard:
    """A normal writer's held token; every write must pass :meth:`require_current`."""

    def __init__(self, store: OwnershipStore, fence: WriterFence) -> None:
        self._store = store
        self._fence = fence

    @property
    def fence(self) -> WriterFence:
        return self._fence

    def require_current(self) -> OwnershipRecord:
        return self._store.require_current(self._fence)

    def may_create_new_risk(self) -> bool:
        """True only when the token is still current AND the normal owner holds the lease."""
        try:
            record = self._store.require_current(self._fence)
        except OwnershipStateError:
            return False
        return record.owner_kind is OwnerKind.NORMAL and self._fence.allows_new_risk
