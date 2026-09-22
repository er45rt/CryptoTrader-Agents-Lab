"""Priority safety lane (Phase 9).

Safety commands (halt, cancel-open, reduce-only, kill switch) travel on their own lane and are
always drained BEFORE any normal strategy/AI work. They never wait behind an inference backlog.

Deterministic ordering: within the safety lane, commands are processed in submission order
(FIFO); the normal lane is strictly secondary. Commands are de-duplicated by ``command_id``, so a
re-delivered kill switch collapses into the one already-queued command.

Capacity policy:

* the normal lane is BOUNDED; when it is full the submission fails closed with
  :class:`LaneBackpressureError` (mirroring the intent-level ``BACKPRESSURE_REJECTED``);
* the safety lane never drops or rejects a command: losing a kill switch would be worse than an
  unbounded queue of rare operational commands (the lane depth is exposed for observability).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from .errors import LaneBackpressureError, SafetyConfigError, SafetyStateError

__all__ = ["LaneCommand", "LanePriority", "PrioritySafetyLane"]


class LanePriority(str, Enum):
    SAFETY = "safety"
    NORMAL = "normal"


@dataclass(frozen=True)
class LaneCommand:
    """One queued command. ``command_id`` is the idempotency key."""

    command_id: UUID
    kind: str
    priority: LanePriority
    payload: Mapping[str, Any] = field(default_factory=dict)
    enqueued_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise SafetyStateError("a lane command requires a non-empty kind.")
        if not isinstance(self.priority, LanePriority):
            raise SafetyStateError("a lane command requires a LanePriority.")


class PrioritySafetyLane:
    """Two-lane deterministic queue: safety first, normal second, both de-duplicated."""

    def __init__(self, *, max_normal_depth: int) -> None:
        if not isinstance(max_normal_depth, int) or max_normal_depth <= 0:
            raise SafetyConfigError("the normal lane requires max_normal_depth > 0.")
        self._max_normal_depth = max_normal_depth
        self._safety: list[LaneCommand] = []
        self._normal: list[LaneCommand] = []
        self._seen: set[UUID] = set()

    @property
    def max_normal_depth(self) -> int:
        return self._max_normal_depth

    @property
    def safety_depth(self) -> int:
        return len(self._safety)

    @property
    def normal_depth(self) -> int:
        return len(self._normal)

    def submit(self, command: LaneCommand) -> bool:
        """Queue a command. Returns False when the same ``command_id`` is already queued."""
        if not isinstance(command, LaneCommand):
            raise SafetyStateError("submit expects a LaneCommand.")
        if command.command_id in self._seen:
            return False
        if command.priority is LanePriority.SAFETY:
            self._safety.append(command)
            self._seen.add(command.command_id)
            return True
        if len(self._normal) >= self._max_normal_depth:
            raise LaneBackpressureError(
                f"normal lane is full ({self._max_normal_depth}); the safety lane is unaffected."
            )
        self._normal.append(command)
        self._seen.add(command.command_id)
        return True

    def next_command(self) -> LaneCommand | None:
        """Pop the next command: safety lane first (FIFO), then the normal lane (FIFO)."""
        if self._safety:
            return self._safety.pop(0)
        if self._normal:
            return self._normal.pop(0)
        return None

    def drain(self, *, limit: int) -> list[LaneCommand]:
        """Pop up to ``limit`` commands in deterministic priority order."""
        if not isinstance(limit, int) or limit < 0:
            raise SafetyStateError("drain limit must be a non-negative integer.")
        return [command for _ in range(limit) if (command := self.next_command()) is not None]

    def safety_command_ids(self) -> tuple[UUID, ...]:
        return tuple(command.command_id for command in self._safety)

    def normal_command_ids(self) -> tuple[UUID, ...]:
        return tuple(command.command_id for command in self._normal)

    def forget(self, command_id: UUID) -> None:
        """Drop a completed command's idempotency key once it has been fully processed."""
        self._seen.discard(command_id)

    def __len__(self) -> int:
        return len(self._safety) + len(self._normal)
