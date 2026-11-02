"""Phase 6: bounded pending capacity and stale-intent eviction."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from crypto_trader.validity import (
    BoundedPendingIntents,
    ValidityConfigError,
    ValidityPolicy,
    ValidityRequest,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
POLICY = ValidityPolicy(policy_id="p", max_age_seconds=5.0, max_market_move_bps=Decimal("25"))


def request(*, market_offset_s: float = 0.0) -> ValidityRequest:
    return ValidityRequest(
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        decision_started_at=BASE + timedelta(seconds=market_offset_s),
        decision_completed_at=BASE + timedelta(seconds=market_offset_s),
        market_event_ts=BASE + timedelta(seconds=market_offset_s),
        reference_price=Decimal("2700.00"),
        market_reference="obs-1",
    )


def test_bounded_queue_rejects_when_full():
    queue = BoundedPendingIntents(max_size=2)

    assert queue.offer(request(), enqueued_at=BASE)[0] is True
    assert queue.offer(request(), enqueued_at=BASE)[0] is True
    accepted, reason = queue.offer(request(), enqueued_at=BASE)

    assert accepted is False
    assert reason == "bounded_capacity_exhausted"
    assert len(queue) == 2
    assert queue.capacity_available() is False


def test_invalid_capacity_fails_closed():
    with pytest.raises(ValidityConfigError):
        BoundedPendingIntents(max_size=0)


def test_stale_intents_are_evicted_so_they_cannot_wait_indefinitely():
    queue = BoundedPendingIntents(max_size=4)
    fresh, stale = request(market_offset_s=1.0), request(market_offset_s=0.0)
    queue.offer(fresh, enqueued_at=BASE + timedelta(seconds=1))
    queue.offer(stale, enqueued_at=BASE)

    evicted = queue.evict_expired(now=BASE + timedelta(seconds=6), policy=POLICY)

    assert [item.intent_id for item in evicted] == [stale.intent_id]
    assert queue.intent_ids() == (fresh.intent_id,)
    assert len(queue) == 1



def test_future_dated_intents_are_evicted_fail_closed():
    queue = BoundedPendingIntents(max_size=2)
    future = request(market_offset_s=10.0)
    queue.offer(future, enqueued_at=BASE)

    evicted = queue.evict_expired(now=BASE, policy=POLICY)

    assert [item.intent_id for item in evicted] == [future.intent_id]
    assert len(queue) == 0


def test_exact_max_age_remains_eligible_until_it_exceeds_policy():
    queue = BoundedPendingIntents(max_size=2)
    boundary = request()
    queue.offer(boundary, enqueued_at=BASE)

    evicted = queue.evict_expired(now=BASE + timedelta(seconds=POLICY.max_age_seconds), policy=POLICY)

    assert evicted == []
    assert queue.intent_ids() == (boundary.intent_id,)

def test_removal_is_explicit_and_idempotent():
    queue = BoundedPendingIntents(max_size=2)
    item = request()
    queue.offer(item, enqueued_at=BASE)

    assert queue.remove(item.intent_id) is True
    assert queue.remove(item.intent_id) is False
    assert len(queue) == 0
