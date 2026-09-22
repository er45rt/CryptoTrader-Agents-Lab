"""Bounded pending-intent capacity (Phase 6).

Only bounded queues are allowed: an otherwise-valid intent is rejected with
BACKPRESSURE_REJECTED when capacity is exhausted, and entries whose age (measured from the
ORIGINAL market observation) exceeds the policy are evicted so a stale intent can never wait
indefinitely and execute later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from .errors import ValidityConfigError
from .gate import ValidityRequest
from .policy import ValidityPolicy

__all__ = ["BoundedPendingIntents", "PendingIntent"]


@dataclass(frozen=True)
class PendingIntent:
    request: ValidityRequest
    enqueued_at: datetime


class BoundedPendingIntents:
    """Deterministic bounded queue for intents awaiting admission."""

    def __init__(self, *, max_size: int) -> None:
        if not isinstance(max_size, int) or max_size <= 0:
            raise ValidityConfigError("bounded pending capacity requires max_size > 0.")
        self._max_size = max_size
        self._items: list[PendingIntent] = []

    @property
    def max_size(self) -> int:
        return self._max_size

    def capacity_available(self) -> bool:
        return len(self._items) < self._max_size

    def offer(self, request: ValidityRequest, *, enqueued_at: datetime) -> tuple[bool, str]:
        """Offer an intent; returns (accepted, reason_code)."""
        if enqueued_at.tzinfo is None or enqueued_at.utcoffset() is None:
            raise ValidityConfigError("enqueued_at must be timezone-aware.")
        if not self.capacity_available():
            return False, "bounded_capacity_exhausted"
        self._items.append(PendingIntent(request=request, enqueued_at=enqueued_at))
        return True, "accepted"

    def evict_expired(self, *, now: datetime, policy: ValidityPolicy) -> list[ValidityRequest]:
        """Drop intents whose age from the ORIGINAL market observation exceeds the policy."""
        now_utc = now.astimezone(timezone.utc)
        kept: list[PendingIntent] = []
        evicted: list[ValidityRequest] = []
        for item in self._items:
            age = (now_utc - item.request.market_event_ts.astimezone(timezone.utc)).total_seconds()
            if age > policy.max_age_seconds:
                evicted.append(item.request)
            else:
                kept.append(item)
        self._items = kept
        return evicted

    def remove(self, intent_id: UUID) -> bool:
        for index, item in enumerate(self._items):
            if item.request.intent_id == intent_id:
                del self._items[index]
                return True
        return False

    def intent_ids(self) -> tuple[UUID, ...]:
        return tuple(item.request.intent_id for item in self._items)

    def snapshot(self) -> tuple[PendingIntent, ...]:
        return tuple(self._items)

    def __len__(self) -> int:
        return len(self._items)
