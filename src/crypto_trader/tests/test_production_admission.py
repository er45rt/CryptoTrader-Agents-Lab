"""Offline tests for bounded serialized production intent admission."""

from __future__ import annotations

import heapq
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import (
    ProductionAdmissionController,
    ProductionStateError,
)
from crypto_trader.tests.production_fixtures import (
    BASE,
    INSTRUMENT,
    RecordingExecutionPort,
    build_runtime,
    intent,
    operator_command,
)


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as active:
        yield active


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _started(journal, state_dir, *, port=None):
    execution = port or RecordingExecutionPort(INSTRUMENT)
    runtime = build_runtime(
        journal=journal,
        state_dir=state_dir,
        execution=execution,
    )
    runtime.start()
    return execution, runtime


def _admission(
    runtime,
    clock,
    *,
    max_size=4,
    max_per_strategy_queued=2,
    max_retired=16,
    max_per_strategy_retired=8,
    max_rejection_strategies=None,
):
    if max_rejection_strategies is None:
        max_rejection_strategies = min(8, max_retired)
    return ProductionAdmissionController(
        runtime=runtime,
        max_size=max_size,
        max_per_strategy_queued=max_per_strategy_queued,
        max_retired=max_retired,
        max_per_strategy_retired=max_per_strategy_retired,
        max_rejection_strategies=max_rejection_strategies,
        clock=clock,
    )


def _intent_at(seed: str, stamp: datetime, **overrides):
    values = {
        "decision_started_at": stamp - timedelta(seconds=1),
        "decision_completed_at": stamp,
        "market_event_ts": stamp,
    }
    values.update(overrides)
    return intent(seed, **values)


def test_runtime_refuses_non_owner_thread_before_any_state_change(journal, state_dir):
    port, runtime = _started(journal, state_dir)
    before_pending = runtime._pending.items()
    before_issued = dict(runtime._issued_preparations)
    failures: list[BaseException] = []

    def foreign_call() -> None:
        try:
            runtime.prepare_intent(intent("foreign-runtime-call"))
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=foreign_call, daemon=True)
    worker.start()
    worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ProductionStateError)
    assert "thread-affine" in str(failures[0])
    assert runtime._pending.items() == before_pending
    assert runtime._issued_preparations == before_issued
    assert port.submit_count == 0


def test_run_once_from_non_owner_thread_is_refused_without_dequeue(journal, state_dir):
    port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, lambda: BASE)
    queued = intent("foreign-run-once")
    assert admission.offer(queued) == (True, "accepted")

    failures: list[BaseException] = []

    def foreign_run() -> None:
        try:
            admission.run_once()
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=foreign_run, daemon=True)
    worker.start()
    worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ProductionStateError)
    assert admission.queued_intent_ids() == (queued.intent_id,)
    assert admission.depth() == 1
    assert port.submit_count == 0


def test_concurrent_producers_never_exceed_global_or_strategy_caps(journal, state_dir):
    port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=4,
        max_per_strategy_queued=2,
    )
    results: list[tuple[str, bool, str]] = []
    result_lock = threading.Lock()

    def producer(index: int) -> None:
        strategy_id = f"strategy.concurrent-{index % 3}"
        offered = intent(
            f"concurrent-{index}",
            strategy_id=strategy_id,
            decision_scope=f"slot-{index}",
        )
        accepted, reason = admission.offer(offered)
        with result_lock:
            results.append((strategy_id, accepted, reason))

    workers = [threading.Thread(target=producer, args=(index,)) for index in range(30)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5.0)

    assert all(not worker.is_alive() for worker in workers)
    accepted = [item for item in results if item[1]]
    assert len(accepted) == 4
    counts = Counter(strategy for strategy, _accepted, _reason in accepted)
    assert max(counts.values()) <= 2
    assert admission.depth() == 4
    assert len(runtime._pending) == 0
    assert runtime._issued_preparations == {}
    assert port.submit_count == 0


def test_per_strategy_cap_cannot_starve_another_strategy(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=3,
        max_per_strategy_queued=1,
    )

    a1 = intent("cap-a1", strategy_id="strategy.a", decision_scope="entry-1")
    a2 = intent("cap-a2", strategy_id="strategy.a", decision_scope="entry-2")
    b1 = intent("cap-b1", strategy_id="strategy.b", decision_scope="entry")

    assert admission.offer(a1) == (True, "accepted")
    assert admission.offer(a2) == (False, "strategy_queue_capacity_exhausted")
    assert admission.offer(b1) == (True, "accepted")
    assert admission.queued_intent_ids() == (a1.intent_id, b1.intent_id)


def test_newest_same_scope_replaces_older_before_strategy_cap(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=3,
        max_per_strategy_queued=2,
    )
    a1 = intent(
        "replace-a1",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=3),
        decision_completed_at=BASE - timedelta(seconds=2),
        market_event_ts=BASE - timedelta(seconds=2),
    )
    other = intent(
        "replace-other",
        strategy_id="strategy.a",
        decision_scope="other",
        decision_completed_at=BASE - timedelta(seconds=1),
        market_event_ts=BASE - timedelta(seconds=2),
    )
    a2 = intent(
        "replace-a2",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_completed_at=BASE,
    )
    delayed_old = intent(
        "replace-delayed-old",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=4),
        decision_completed_at=BASE - timedelta(seconds=3),
        market_event_ts=BASE - timedelta(seconds=4),
    )

    assert admission.offer(a1) == (True, "accepted")
    assert admission.offer(other) == (True, "accepted")
    assert admission.offer(a2) == (True, "accepted")
    assert admission.queued_intent_ids() == (other.intent_id, a2.intent_id)
    assert admission.offer(delayed_old) == (
        False,
        "superseded_by_scope_high_water",
    )
    assert admission.offer(a1) == (False, "duplicate_intent_id")


def test_duplicate_intent_is_rejected_while_queued_and_while_active(journal, state_dir):
    entered = threading.Event()
    release = threading.Event()

    class BlockingPort(RecordingExecutionPort):
        def submit(self, request):  # noqa: ANN001 - deterministic concurrency test double
            entered.set()
            assert release.wait(timeout=5.0)
            return super().submit(request)

    port, runtime = _started(journal, state_dir, port=BlockingPort(INSTRUMENT))
    admission = _admission(runtime, lambda: BASE)
    target = intent("duplicate-active", strategy_id="strategy.a", decision_scope="entry")

    assert admission.offer(target) == (True, "accepted")
    assert admission.offer(target) == (False, "duplicate_intent_id")

    active_result: list[tuple[bool, str]] = []

    def duplicate_during_submit() -> None:
        assert entered.wait(timeout=5.0)
        active_result.append(admission.offer(target))
        release.set()

    producer = threading.Thread(target=duplicate_during_submit, daemon=True)
    producer.start()
    outcome = admission.run_once()
    producer.join(timeout=5.0)

    assert not producer.is_alive()
    assert active_result == [(False, "duplicate_intent_id")]
    assert outcome is not None and outcome.submitted is True
    assert port.submit_count == 1
    assert admission.active_intent_id() is None


def test_processed_intent_id_stays_retired_until_its_deadline(journal, state_dir):
    port, runtime = _started(journal, state_dir)
    clock = MutableClock(BASE)
    admission = _admission(runtime, clock)
    target = intent("retired-after-submit")

    assert admission.offer(target) == (True, "accepted")
    outcome = admission.run_once()
    assert outcome is not None and outcome.submitted is True
    assert port.submit_count == 1
    assert admission.offer(target) == (False, "duplicate_intent_id")

    clock.value = BASE + timedelta(seconds=61)
    assert admission.offer(target) == (False, "max_age_exceeded")


def test_stopped_runtime_refuses_before_dequeue(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, lambda: BASE)
    target = intent("stopped-before-dequeue")
    assert admission.offer(target) == (True, "accepted")

    runtime.stop()
    with pytest.raises(ProductionStateError, match="not running"):
        admission.run_once()

    assert admission.queued_intent_ids() == (target.intent_id,)
    assert admission.active_intent_id() is None


def test_execution_exception_preserves_type_adds_intent_id_and_never_requeues(
    journal, state_dir
):
    class FailingPort(RecordingExecutionPort):
        def submit(self, request):  # noqa: ANN001 - deterministic failure double
            raise RuntimeError("synthetic execution failure")

    _port, runtime = _started(journal, state_dir, port=FailingPort(INSTRUMENT))
    admission = _admission(runtime, lambda: BASE)
    target = intent("execution-failure-retired")

    assert admission.offer(target) == (True, "accepted")
    with pytest.raises(RuntimeError, match="synthetic execution failure") as caught:
        admission.run_once()

    notes = tuple(getattr(caught.value, "__notes__", ()))
    assert any(str(target.intent_id) in note for note in notes)
    assert admission.active_intent_id() is None
    assert admission.depth() == 0
    assert admission.offer(target) == (False, "duplicate_intent_id")


@pytest.mark.parametrize(
    "overrides,reason",
    [
        (
            {
                "decision_started_at": BASE - timedelta(seconds=62),
                "decision_completed_at": BASE - timedelta(seconds=61),
                "market_event_ts": BASE - timedelta(seconds=61),
            },
            "max_age_exceeded",
        ),
        (
            {"market_event_ts": BASE + timedelta(seconds=1)},
            "market_event_ts_in_future",
        ),
        (
            {"valid_until": BASE - timedelta(seconds=1)},
            "valid_until_exceeded",
        ),
    ],
)
def test_temporally_invalid_incoming_intent_never_takes_queue_capacity(
    journal, state_dir, overrides, reason
):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, lambda: BASE, max_size=2, max_per_strategy_queued=1)

    rejected = intent("temporal-reject", **overrides)

    assert admission.offer(rejected) == (False, reason)
    assert admission.depth() == 0


def test_expired_queued_intent_is_evicted_before_capacity_check(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    clock = MutableClock(BASE)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
    )
    old = intent(
        "queued-deadline-old",
        strategy_id="strategy.a",
        valid_until=BASE + timedelta(seconds=1),
    )
    fresh = intent(
        "queued-deadline-fresh",
        strategy_id="strategy.b",
        market_event_ts=BASE,
    )

    assert admission.offer(old) == (True, "accepted")
    clock.value = BASE + timedelta(seconds=2)
    assert admission.offer(fresh) == (True, "accepted")
    assert admission.queued_intent_ids() == (fresh.intent_id,)


def test_supersession_is_scoped_by_strategy_and_decision_scope(journal, state_dir):
    port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=4,
        max_per_strategy_queued=2,
    )
    a1 = intent(
        "scope-a1",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=3),
        decision_completed_at=BASE - timedelta(seconds=2),
        market_event_ts=BASE - timedelta(seconds=2),
    )
    b1 = intent("scope-b1", strategy_id="strategy.b", decision_scope="entry")
    a2 = intent(
        "scope-a2",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=2),
        decision_completed_at=BASE - timedelta(seconds=1),
        market_event_ts=BASE - timedelta(seconds=1),
    )

    assert admission.offer(a1) == (True, "accepted")
    assert admission.offer(b1) == (True, "accepted")
    assert admission.offer(a2) == (True, "accepted")
    assert admission.queued_intent_ids() == (b1.intent_id, a2.intent_id)
    assert admission.offer(a1) == (False, "duplicate_intent_id")

    first = admission.run_once()
    second = admission.run_once()

    assert first is not None and first.submitted is True
    assert second is not None and second.submitted is True
    assert port.submit_count == 2
    assert admission.run_once() is None


def test_processed_newer_scope_blocks_late_older_different_id(journal, state_dir):
    port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, lambda: BASE)
    newer = intent(
        "late-old-newer",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=1),
        decision_completed_at=BASE,
        market_event_ts=BASE,
    )
    older = intent(
        "late-old-older",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=3),
        decision_completed_at=BASE - timedelta(seconds=2),
        market_event_ts=BASE - timedelta(seconds=2),
    )

    assert admission.offer(newer) == (True, "accepted")
    result = admission.run_once()
    assert result is not None and result.submitted is True
    assert admission.offer(older) == (False, "superseded_by_scope_high_water")
    assert port.submit_count == 1


def test_expired_newer_queue_entry_still_blocks_late_older_scope_decision(
    journal, state_dir
):
    _port, runtime = _started(journal, state_dir)
    clock = MutableClock(BASE)
    admission = _admission(runtime, clock)
    newer = intent(
        "expiry-newer",
        strategy_id="strategy.a",
        decision_scope="entry",
        valid_until=BASE + timedelta(seconds=1),
    )
    older = intent(
        "expiry-older",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=3),
        decision_completed_at=BASE - timedelta(seconds=2),
        market_event_ts=BASE - timedelta(seconds=2),
    )

    assert admission.offer(newer) == (True, "accepted")
    clock.value = BASE + timedelta(seconds=2)
    assert admission.offer(older) == (False, "superseded_by_scope_high_water")
    assert newer.intent_id in admission._retired
    assert admission.depth() == 0


def test_equal_scope_decision_evidence_with_new_id_is_ambiguous_before_and_after_dequeue(
    journal, state_dir
):
    port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, lambda: BASE)
    first = intent("equal-evidence-first", strategy_id="strategy.a", decision_scope="entry")
    second = intent("equal-evidence-second", strategy_id="strategy.a", decision_scope="entry")
    third = intent("equal-evidence-third", strategy_id="strategy.a", decision_scope="entry")

    assert first.intent_id != second.intent_id != third.intent_id
    assert admission.offer(first) == (True, "accepted")
    assert admission.offer(second) == (False, "ambiguous_same_scope_decision")
    assert first.intent_id in admission._retired
    assert admission.depth() == 0
    assert admission.run_once() is None
    assert admission.offer(third) == (False, "ambiguous_same_scope_decision")
    assert port.submit_count == 0


def test_full_queue_allows_newer_same_scope_replacement_without_depth_growth(
    journal, state_dir
):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=5,
        max_per_strategy_retired=2,
    )
    a1 = intent(
        "full-replace-a1",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=3),
        decision_completed_at=BASE - timedelta(seconds=2),
        market_event_ts=BASE - timedelta(seconds=2),
    )
    b1 = intent("full-replace-b1", strategy_id="strategy.b", decision_scope="entry")
    a2 = intent(
        "full-replace-a2",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=2),
        decision_completed_at=BASE - timedelta(seconds=1),
        market_event_ts=BASE - timedelta(seconds=1),
    )

    assert admission.offer(a1) == (True, "accepted")
    assert admission.offer(b1) == (True, "accepted")
    assert admission.depth() == 2
    assert admission.offer(a2) == (True, "accepted")
    assert admission.depth() == 2
    assert admission.queued_intent_ids() == (b1.intent_id, a2.intent_id)
    assert a1.intent_id in admission._retired


def test_replay_caps_fail_closed_without_partial_mutation(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
    )
    a1 = intent(
        "replay-cap-a1",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=3),
        decision_completed_at=BASE - timedelta(seconds=2),
        market_event_ts=BASE - timedelta(seconds=2),
    )
    b1 = intent("replay-cap-b1", strategy_id="strategy.b", decision_scope="entry")
    a2 = intent(
        "replay-cap-a2",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=2),
        decision_completed_at=BASE - timedelta(seconds=1),
        market_event_ts=BASE - timedelta(seconds=1),
    )
    c1 = intent("replay-cap-c1", strategy_id="strategy.c", decision_scope="entry")

    assert admission.offer(a1) == (True, "accepted")
    assert admission.offer(b1) == (True, "accepted")
    assert admission.offer(a2) == (True, "accepted")
    before_queue = admission.queued_intent_ids()
    before_retired = dict(admission._retired)
    before_high_water = dict(admission._scope_high_water)

    assert admission.offer(c1) == (False, "replay_capacity_exhausted")
    assert admission.queued_intent_ids() == before_queue
    assert admission._retired == before_retired
    assert admission._scope_high_water == before_high_water
    assert admission._strategy_rejection_floor["strategy.c"].order_key == (
        admission._decision_order_key(c1)
    )
    assert len(admission._retired) + admission.depth() == admission.max_retired


def test_per_strategy_replay_cap_fails_closed_without_partial_mutation(
    journal, state_dir
):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=3,
        max_per_strategy_queued=1,
        max_retired=5,
        max_per_strategy_retired=2,
    )
    a1 = intent(
        "strategy-replay-a1",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=3),
        decision_completed_at=BASE - timedelta(seconds=2),
        market_event_ts=BASE - timedelta(seconds=2),
    )
    a2 = intent(
        "strategy-replay-a2",
        strategy_id="strategy.a",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=2),
        decision_completed_at=BASE - timedelta(seconds=1),
        market_event_ts=BASE - timedelta(seconds=1),
    )
    a3 = intent(
        "strategy-replay-a3",
        strategy_id="strategy.a",
        decision_scope="other",
    )

    assert admission.offer(a1) == (True, "accepted")
    assert admission.offer(a2) == (True, "accepted")
    before_queue = admission.queued_intent_ids()
    before_retired = dict(admission._retired)

    assert admission.offer(a3) == (False, "strategy_replay_capacity_exhausted")
    assert admission.queued_intent_ids() == before_queue
    assert admission._retired == before_retired
    assert admission._strategy_rejection_floor["strategy.a"].order_key == (
        admission._decision_order_key(a3)
    )


def test_brand_new_scope_capacity_rejection_sets_strategy_floor(
    journal, state_dir
):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
        max_rejection_strategies=2,
    )
    old_retired = _intent_at(
        "floor-old-retired",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.old",
        decision_scope="old",
    )
    q1 = _intent_at(
        "floor-q1",
        BASE,
        strategy_id="strategy.q1",
        decision_scope="q1",
    )
    q2 = _intent_at(
        "floor-q2",
        BASE,
        strategy_id="strategy.q2",
        decision_scope="q2",
    )
    rejected = _intent_at(
        "floor-rejected",
        BASE,
        strategy_id="strategy.a",
        decision_scope="brand-new",
    )
    older = _intent_at(
        "floor-older",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.a",
        decision_scope="brand-new",
    )
    equal = _intent_at(
        "floor-equal",
        BASE,
        strategy_id="strategy.a",
        decision_scope="brand-new",
    )

    assert admission.offer(old_retired) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(q1) == (True, "accepted")
    assert admission.offer(q2) == (True, "accepted")
    assert admission.offer(rejected) == (False, "replay_capacity_exhausted")

    floor = admission._strategy_rejection_floor["strategy.a"]
    assert floor.order_key == admission._decision_order_key(rejected)
    assert floor.expires_at == admission._replay_expiry(rejected)
    assert admission.offer(rejected) == (
        False,
        "superseded_by_capacity_rejection_floor",
    )
    assert admission.offer(older) == (
        False,
        "superseded_by_capacity_rejection_floor",
    )
    assert admission.offer(equal) == (
        False,
        "superseded_by_capacity_rejection_floor",
    )


def test_strategy_rejection_floor_strict_expiry_boundary(journal, state_dir):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
        max_rejection_strategies=2,
    )
    old_retired = _intent_at(
        "floor-boundary-old",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.old",
        decision_scope="old",
    )
    q1 = _intent_at(
        "floor-boundary-q1",
        BASE,
        strategy_id="strategy.q1",
        decision_scope="q1",
    )
    q2 = _intent_at(
        "floor-boundary-q2",
        BASE,
        strategy_id="strategy.q2",
        decision_scope="q2",
    )
    rejected = _intent_at(
        "floor-boundary-rejected",
        BASE,
        strategy_id="strategy.a",
        decision_scope="brand-new",
    )
    equal = _intent_at(
        "floor-boundary-equal",
        BASE,
        strategy_id="strategy.a",
        decision_scope="brand-new",
    )

    assert admission.offer(old_retired) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(q1) == (True, "accepted")
    assert admission.offer(q2) == (True, "accepted")
    assert admission.offer(rejected) == (False, "replay_capacity_exhausted")

    clock.value = BASE + timedelta(seconds=60)
    assert admission.offer(equal) == (
        False,
        "superseded_by_capacity_rejection_floor",
    )
    assert "strategy.a" in admission._strategy_rejection_floor

    clock.value = BASE + timedelta(seconds=60, microseconds=1)
    assert admission.offer(equal) == (False, "max_age_exceeded")
    assert "strategy.a" not in admission._strategy_rejection_floor


def test_global_rejection_floor_strict_expiry_boundary(journal, state_dir):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
        max_rejection_strategies=1,
    )
    old_retired = _intent_at(
        "global-boundary-old",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.old",
        decision_scope="old",
    )
    q1 = _intent_at(
        "global-boundary-q1",
        BASE,
        strategy_id="strategy.q1",
        decision_scope="q1",
    )
    q2 = _intent_at(
        "global-boundary-q2",
        BASE,
        strategy_id="strategy.q2",
        decision_scope="q2",
    )
    map_floor = _intent_at(
        "global-boundary-map",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.a",
        decision_scope="new-a",
    )
    overflow = _intent_at(
        "global-boundary-overflow",
        BASE,
        strategy_id="strategy.b",
        decision_scope="new-b",
    )
    equal = _intent_at(
        "global-boundary-equal",
        BASE,
        strategy_id="strategy.c",
        decision_scope="new-c",
    )

    assert admission.offer(old_retired) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(q1) == (True, "accepted")
    assert admission.offer(q2) == (True, "accepted")
    assert admission.offer(map_floor) == (False, "replay_capacity_exhausted")
    assert admission.offer(overflow) == (False, "replay_capacity_exhausted")

    clock.value = BASE + timedelta(seconds=60)
    assert admission.offer(equal) == (
        False,
        "superseded_by_global_capacity_rejection_floor",
    )
    assert admission._global_rejection_floor is not None

    clock.value = BASE + timedelta(seconds=60, microseconds=1)
    assert admission.offer(equal) == (False, "max_age_exceeded")
    assert admission._global_rejection_floor is None


def test_retired_high_water_bypasses_coarse_strategy_floor(journal, state_dir):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=4,
        max_per_strategy_retired=3,
        max_rejection_strategies=2,
    )
    old_unrelated = _intent_at(
        "floor-bypass-retired-old",
        BASE - timedelta(seconds=40),
        strategy_id="strategy.old",
        decision_scope="old",
    )
    precise_high_water = _intent_at(
        "floor-bypass-retired-high-water",
        BASE - timedelta(seconds=10),
        strategy_id="strategy.a",
        decision_scope="precise",
    )
    q1 = _intent_at(
        "floor-bypass-retired-q1",
        BASE,
        strategy_id="strategy.q1",
        decision_scope="q1",
    )
    q2 = _intent_at(
        "floor-bypass-retired-q2",
        BASE,
        strategy_id="strategy.q2",
        decision_scope="q2",
    )
    floor_source = _intent_at(
        "floor-bypass-retired-source",
        BASE,
        strategy_id="strategy.a",
        decision_scope="coarse",
    )

    assert admission.offer(old_unrelated) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(precise_high_water) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(q1) == (True, "accepted")
    assert admission.offer(q2) == (True, "accepted")
    assert admission.offer(floor_source) == (False, "replay_capacity_exhausted")
    assert "strategy.a" in admission._strategy_rejection_floor

    clock.value = BASE + timedelta(seconds=21)
    assert admission.run_once().submitted is True
    between = _intent_at(
        "floor-bypass-retired-between",
        BASE - timedelta(seconds=5),
        strategy_id="strategy.a",
        decision_scope="precise",
    )
    assert admission.offer(between) == (True, "accepted")


def test_strategy_floor_allows_strictly_newer_evidence_after_capacity_frees(
    journal, state_dir
):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
        max_rejection_strategies=2,
    )
    old_retired = _intent_at(
        "floor-newer-old-retired",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.old",
        decision_scope="old",
    )
    q1 = _intent_at(
        "floor-newer-q1",
        BASE,
        strategy_id="strategy.q1",
        decision_scope="q1",
    )
    q2 = _intent_at(
        "floor-newer-q2",
        BASE,
        strategy_id="strategy.q2",
        decision_scope="q2",
    )
    rejected = _intent_at(
        "floor-newer-rejected",
        BASE,
        strategy_id="strategy.a",
        decision_scope="brand-new",
    )

    assert admission.offer(old_retired) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(q1) == (True, "accepted")
    assert admission.offer(q2) == (True, "accepted")
    assert admission.offer(rejected) == (False, "replay_capacity_exhausted")

    clock.value = BASE + timedelta(seconds=31)
    assert admission.run_once().submitted is True
    newer = _intent_at(
        "floor-newer-accepted",
        clock.value,
        strategy_id="strategy.a",
        decision_scope="brand-new",
    )
    assert admission.offer(newer) == (True, "accepted")


def test_strategy_floor_isolated_from_other_strategies(journal, state_dir):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
        max_rejection_strategies=2,
    )
    old_retired = _intent_at(
        "floor-isolation-old",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.old",
        decision_scope="old",
    )
    q1 = _intent_at(
        "floor-isolation-q1",
        BASE,
        strategy_id="strategy.q1",
        decision_scope="q1",
    )
    q2 = _intent_at(
        "floor-isolation-q2",
        BASE,
        strategy_id="strategy.q2",
        decision_scope="q2",
    )
    rejected = _intent_at(
        "floor-isolation-rejected",
        BASE,
        strategy_id="strategy.a",
        decision_scope="brand-new",
    )

    assert admission.offer(old_retired) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(q1) == (True, "accepted")
    assert admission.offer(q2) == (True, "accepted")
    assert admission.offer(rejected) == (False, "replay_capacity_exhausted")

    clock.value = BASE + timedelta(seconds=31)
    assert admission.run_once().submitted is True
    unrelated_older = _intent_at(
        "floor-isolation-unrelated",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.unrelated",
        decision_scope="brand-new",
    )
    assert admission.offer(unrelated_older) == (True, "accepted")


def test_rejection_floor_map_overflow_uses_one_global_floor(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
        max_rejection_strategies=1,
    )
    old_retired = _intent_at(
        "global-floor-old",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.old",
        decision_scope="old",
    )
    q1 = _intent_at(
        "global-floor-q1",
        BASE,
        strategy_id="strategy.q1",
        decision_scope="q1",
    )
    q2 = _intent_at(
        "global-floor-q2",
        BASE,
        strategy_id="strategy.q2",
        decision_scope="q2",
    )
    first = _intent_at(
        "global-floor-first",
        BASE - timedelta(seconds=2),
        strategy_id="strategy.a",
        decision_scope="new-a",
    )
    second = _intent_at(
        "global-floor-second",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.b",
        decision_scope="new-b",
    )
    third = _intent_at(
        "global-floor-third",
        BASE,
        strategy_id="strategy.c",
        decision_scope="new-c",
    )

    assert admission.offer(old_retired) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(q1) == (True, "accepted")
    assert admission.offer(q2) == (True, "accepted")

    assert admission.offer(first) == (False, "replay_capacity_exhausted")
    assert admission.offer(second) == (False, "replay_capacity_exhausted")
    assert admission.offer(third) == (False, "replay_capacity_exhausted")

    assert len(admission._strategy_rejection_floor) == 1
    assert admission._strategy_rejection_floor["strategy.a"].order_key == (
        admission._decision_order_key(first)
    )
    assert admission._global_rejection_floor is not None
    assert admission._global_rejection_floor.order_key == admission._decision_order_key(
        third
    )


def test_global_rejection_floor_blocks_older_unrecorded_strategy_but_allows_newer(
    journal, state_dir
):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
        max_rejection_strategies=1,
    )
    old_retired = _intent_at(
        "global-check-old",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.old",
        decision_scope="old",
    )
    q1 = _intent_at(
        "global-check-q1",
        BASE,
        strategy_id="strategy.q1",
        decision_scope="q1",
    )
    q2 = _intent_at(
        "global-check-q2",
        BASE,
        strategy_id="strategy.q2",
        decision_scope="q2",
    )
    map_floor = _intent_at(
        "global-check-map",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.a",
        decision_scope="new-a",
    )
    overflow = _intent_at(
        "global-check-overflow",
        BASE,
        strategy_id="strategy.b",
        decision_scope="new-b",
    )

    assert admission.offer(old_retired) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(q1) == (True, "accepted")
    assert admission.offer(q2) == (True, "accepted")
    assert admission.offer(map_floor) == (False, "replay_capacity_exhausted")
    assert admission.offer(overflow) == (False, "replay_capacity_exhausted")

    blocked = _intent_at(
        "global-check-blocked",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.unrecorded",
        decision_scope="new-c",
    )
    assert admission.offer(blocked) == (
        False,
        "superseded_by_global_capacity_rejection_floor",
    )

    clock.value = BASE + timedelta(seconds=31)
    assert admission.run_once().submitted is True
    newer = _intent_at(
        "global-check-newer",
        clock.value,
        strategy_id="strategy.unrecorded",
        decision_scope="new-c",
    )
    assert admission.offer(newer) == (True, "accepted")


def test_rejection_floors_expire_and_remain_hard_bounded(journal, state_dir):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
        max_rejection_strategies=2,
    )
    old_retired = _intent_at(
        "floor-expiry-old",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.old",
        decision_scope="old",
    )
    q1 = _intent_at(
        "floor-expiry-q1",
        BASE,
        strategy_id="strategy.q1",
        decision_scope="q1",
    )
    q2 = _intent_at(
        "floor-expiry-q2",
        BASE,
        strategy_id="strategy.q2",
        decision_scope="q2",
    )

    assert admission.offer(old_retired) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(q1) == (True, "accepted")
    assert admission.offer(q2) == (True, "accepted")

    for index, strategy_id in enumerate(("strategy.a", "strategy.b", "strategy.c")):
        rejected = _intent_at(
            f"floor-expiry-{index}",
            BASE - timedelta(seconds=2 - index),
            strategy_id=strategy_id,
            decision_scope=f"new-{index}",
        )
        assert admission.offer(rejected) == (False, "replay_capacity_exhausted")
        assert len(admission._strategy_rejection_floor) <= 2

    assert admission._global_rejection_floor is not None
    clock.value = BASE + timedelta(seconds=61)
    stale_probe = _intent_at(
        "floor-expiry-probe",
        BASE - timedelta(seconds=61),
        strategy_id="strategy.probe",
        decision_scope="probe",
    )
    assert admission.offer(stale_probe)[0] is False
    assert admission._strategy_rejection_floor == {}
    assert admission._global_rejection_floor is None


def test_strategy_floor_does_not_mask_precise_same_scope_replacement(
    journal, state_dir
):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
        max_rejection_strategies=2,
    )
    old_retired = _intent_at(
        "floor-bypass-old-retired",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.old",
        decision_scope="old",
    )
    queued_old = _intent_at(
        "floor-bypass-queued-old",
        BASE - timedelta(seconds=5),
        strategy_id="strategy.a",
        decision_scope="precise",
    )
    waiting = _intent_at(
        "floor-bypass-waiting",
        BASE,
        strategy_id="strategy.b",
        decision_scope="waiting",
    )
    floor_source = _intent_at(
        "floor-bypass-source",
        BASE,
        strategy_id="strategy.a",
        decision_scope="coarse",
    )
    queued_newer = _intent_at(
        "floor-bypass-queued-newer",
        BASE - timedelta(seconds=4),
        strategy_id="strategy.a",
        decision_scope="precise",
    )

    assert admission.offer(old_retired) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(queued_old) == (True, "accepted")
    assert admission.offer(waiting) == (True, "accepted")
    assert admission.offer(floor_source) == (False, "replay_capacity_exhausted")
    assert "strategy.a" in admission._strategy_rejection_floor

    assert admission.offer(queued_newer) == (
        False,
        "superseded_but_replay_capacity_exhausted",
    )
    assert queued_old.intent_id not in admission.queued_intent_ids()
    assert admission._scope_high_water[("strategy.a", "precise")] == (
        queued_newer.intent_id
    )


def test_rejected_older_uuid_flood_does_not_grow_replay_or_high_water(
    journal, state_dir
):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, lambda: BASE, max_retired=8, max_per_strategy_retired=4)
    newer = intent("flood-high-water", strategy_id="strategy.a", decision_scope="entry")

    assert admission.offer(newer) == (True, "accepted")
    result = admission.run_once()
    assert result is not None and result.submitted is True

    retired_before = dict(admission._retired)
    heap_before = tuple(admission._retired_heap)
    high_water_before = dict(admission._scope_high_water)
    for index in range(1000):
        older = intent(
            f"flood-old-{index}",
            strategy_id="strategy.a",
            decision_scope="entry",
            decision_started_at=BASE - timedelta(seconds=3),
            decision_completed_at=BASE - timedelta(seconds=2),
            market_event_ts=BASE - timedelta(seconds=2),
        )
        assert admission.offer(older) == (False, "superseded_by_scope_high_water")

    assert admission._retired == retired_before
    assert tuple(admission._retired_heap) == heap_before
    assert admission._scope_high_water == high_water_before


def test_replay_state_prunes_only_after_expiry_boundary(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    clock = MutableClock(BASE)
    admission = _admission(runtime, clock)
    target = intent("replay-prune-boundary")

    assert admission.offer(target) == (True, "accepted")
    result = admission.run_once()
    assert result is not None and result.submitted is True
    assert len(admission._retired) == 1

    clock.value = BASE + timedelta(seconds=60)
    assert admission.offer(target) == (False, "duplicate_intent_id")
    assert len(admission._retired) == 1

    clock.value = BASE + timedelta(seconds=61)
    expired = intent(
        "replay-prune-trigger",
        strategy_id="strategy.b",
        decision_scope="entry",
        decision_started_at=BASE - timedelta(seconds=62),
        decision_completed_at=BASE - timedelta(seconds=61),
        market_event_ts=BASE - timedelta(seconds=61),
    )
    assert admission.offer(expired) == (False, "max_age_exceeded")
    assert admission._retired == {}
    assert admission._retired_heap == []
    assert admission._scope_high_water == {}


def test_clock_regression_fails_closed_without_mutating_queue(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    clock = MutableClock(BASE)
    admission = _admission(runtime, clock)
    first = intent("clock-first", strategy_id="strategy.a", decision_scope="entry")
    second = intent("clock-second", strategy_id="strategy.b", decision_scope="entry")

    assert admission.offer(first) == (True, "accepted")
    before = admission.queued_intent_ids()
    clock.value = BASE - timedelta(seconds=1)

    with pytest.raises(ProductionStateError, match="clock moved backwards"):
        admission.offer(second)

    assert admission.queued_intent_ids() == before


def test_concurrent_offers_do_not_touch_journal_or_runtime_risk_state(journal, state_dir):
    port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, lambda: BASE, max_size=6, max_per_strategy_queued=2)
    before_revision = journal.revision()
    before_pending = runtime._pending.items()
    before_issued = dict(runtime._issued_preparations)

    results: list[tuple[bool, str]] = []
    result_lock = threading.Lock()

    def producer(index: int) -> None:
        candidate = intent(
            f"pure-offer-{index}",
            strategy_id=f"strategy.{index % 3}",
            decision_scope=f"scope-{index}",
        )
        result = admission.offer(candidate)
        with result_lock:
            results.append(result)

    workers = [threading.Thread(target=producer, args=(index,)) for index in range(18)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5.0)

    assert all(not worker.is_alive() for worker in workers)
    assert journal.revision() == before_revision
    assert runtime._pending.items() == before_pending
    assert runtime._issued_preparations == before_issued
    assert port.submit_count == 0
    assert admission.depth() <= 6
    assert sum(1 for accepted, _reason in results if accepted) <= 6


def test_late_older_new_id_is_blocked_after_newer_processed(journal, state_dir):
    port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, lambda: BASE)
    newer = _intent_at(
        "processed-newer",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    older = _intent_at(
        "late-older-new-id",
        BASE - timedelta(seconds=2),
        strategy_id="strategy.a",
        decision_scope="entry",
    )

    assert admission.offer(newer) == (True, "accepted")
    outcome = admission.run_once()
    assert outcome is not None and outcome.submitted is True
    retired_before = set(admission._retired)

    assert admission.offer(older) == (False, "superseded_by_scope_high_water")
    assert set(admission._retired) == retired_before
    assert port.submit_count == 1


@pytest.mark.parametrize("mode", ["valid_until", "market_age"])
def test_evicted_newer_intent_is_retired_and_still_blocks_older(
    journal, state_dir, mode
):
    _port, runtime = _started(journal, state_dir)
    clock = MutableClock(BASE)
    admission = _admission(runtime, clock)

    if mode == "valid_until":
        newer = _intent_at(
            "evicted-newer-valid-until",
            BASE,
            strategy_id="strategy.a",
            decision_scope="entry",
            valid_until=BASE + timedelta(seconds=1),
        )
        clock_after = BASE + timedelta(seconds=2)
    else:
        newer = _intent_at(
            "evicted-newer-market-age",
            BASE,
            strategy_id="strategy.a",
            decision_scope="entry",
            decision_started_at=BASE - timedelta(seconds=51),
            market_event_ts=BASE - timedelta(seconds=50),
        )
        clock_after = BASE + timedelta(seconds=11)

    older = _intent_at(
        f"evicted-older-{mode}",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.a",
        decision_scope="entry",
    )

    assert admission.offer(newer) == (True, "accepted")
    assert admission.offer(older) == (False, "superseded_by_newer_queued_intent")

    clock.value = clock_after
    assert admission.offer(older) == (False, "superseded_by_scope_high_water")
    assert newer.intent_id in admission._retired
    assert admission.depth() == 0


@pytest.mark.parametrize(
    "first_id,second_id",
    [
        (UUID(int=1), UUID(int=(1 << 128) - 1)),
        (UUID(int=(1 << 128) - 1), UUID(int=1)),
    ],
)
def test_equal_decision_evidence_different_ids_never_execute_twice(
    journal, state_dir, first_id, second_id
):
    port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, lambda: BASE)
    first = _intent_at(
        "equal-first",
        BASE,
        strategy_id="strategy.a",
        decision_scope="entry",
        intent_id=first_id,
    )
    second = _intent_at(
        "equal-second",
        BASE,
        strategy_id="strategy.a",
        decision_scope="entry",
        intent_id=second_id,
    )

    assert admission.offer(first) == (True, "accepted")
    assert admission.offer(second) == (False, "ambiguous_same_scope_decision")
    assert first.intent_id in admission._retired
    assert admission.run_once() is None
    assert admission.offer(second) == (False, "ambiguous_same_scope_decision")
    assert port.submit_count == 0


def test_full_global_replay_capacity_retires_superseded_queued_intent(
    journal, state_dir
):
    port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
    )
    done = _intent_at(
        "global-done",
        BASE - timedelta(seconds=3),
        strategy_id="strategy.b",
        decision_scope="done",
    )
    old = _intent_at(
        "global-old",
        BASE - timedelta(seconds=2),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    waiting = _intent_at(
        "global-waiting",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.c",
        decision_scope="other",
    )
    newer = _intent_at(
        "global-newer",
        BASE,
        strategy_id="strategy.a",
        decision_scope="entry",
    )

    assert admission.offer(done) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(old) == (True, "accepted")
    assert admission.offer(waiting) == (True, "accepted")
    assert len(admission._retired) + admission.depth() == admission.max_retired

    assert admission.offer(newer) == (
        False,
        "superseded_but_replay_capacity_exhausted",
    )
    assert newer.intent_id in admission._retired
    assert old.intent_id not in admission._retired
    assert old.intent_id not in admission.queued_intent_ids()
    assert newer.intent_id not in admission.queued_intent_ids()
    assert admission._scope_high_water[("strategy.a", "entry")] == newer.intent_id

    intermediate = _intent_at(
        "global-intermediate",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    retired_before = dict(admission._retired)
    assert admission.offer(intermediate) == (
        False,
        "superseded_by_scope_high_water",
    )
    assert admission._retired == retired_before
    assert admission.queued_intent_ids() == (waiting.intent_id,)
    assert port.submit_count == 1


def test_full_strategy_replay_capacity_retires_superseded_queued_intent(
    journal, state_dir
):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=3,
        max_per_strategy_queued=2,
        max_retired=6,
        max_per_strategy_retired=3,
    )
    done = _intent_at(
        "strategy-done",
        BASE - timedelta(seconds=3),
        strategy_id="strategy.a",
        decision_scope="done",
    )
    old = _intent_at(
        "strategy-old",
        BASE - timedelta(seconds=2),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    other = _intent_at(
        "strategy-other",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.a",
        decision_scope="other",
    )
    newer = _intent_at(
        "strategy-newer",
        BASE,
        strategy_id="strategy.a",
        decision_scope="entry",
    )

    assert admission.offer(done) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(old) == (True, "accepted")
    assert admission.offer(other) == (True, "accepted")

    assert admission.offer(newer) == (
        False,
        "superseded_but_strategy_replay_capacity_exhausted",
    )
    assert newer.intent_id in admission._retired
    assert old.intent_id not in admission._retired
    assert admission._scope_high_water[("strategy.a", "entry")] == newer.intent_id
    assert admission.queued_intent_ids() == (other.intent_id,)


def test_capacity_rejected_newer_tombstone_blocks_intermediate_older_after_space_frees(
    journal, state_dir
):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
    )
    unrelated = _intent_at(
        "tombstone-unrelated",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.b",
        decision_scope="other",
    )
    old = _intent_at(
        "tombstone-old",
        BASE - timedelta(seconds=10),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    waiting = _intent_at(
        "tombstone-waiting",
        BASE - timedelta(seconds=5),
        strategy_id="strategy.c",
        decision_scope="other",
    )
    newer = _intent_at(
        "tombstone-newer",
        BASE,
        strategy_id="strategy.a",
        decision_scope="entry",
    )

    assert admission.offer(unrelated) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(old) == (True, "accepted")
    assert admission.offer(waiting) == (True, "accepted")
    assert admission.offer(newer) == (
        False,
        "superseded_but_replay_capacity_exhausted",
    )
    assert admission._scope_high_water[("strategy.a", "entry")] == newer.intent_id

    # Free only the older unrelated replay slot. The newer tombstone is still fresh and must
    # continue blocking a decision whose evidence lies between the removed old and rejected newer.
    clock.value = BASE + timedelta(seconds=31)
    intermediate = _intent_at(
        "tombstone-intermediate",
        BASE - timedelta(seconds=2),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    assert admission.offer(intermediate) == (False, "superseded_by_scope_high_water")
    assert admission._scope_high_water[("strategy.a", "entry")] == newer.intent_id


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "expiry_mismatch"])
def test_high_water_advance_fails_before_mutation_on_corrupt_heap(
    journal, state_dir, corruption
):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
    )
    unrelated = _intent_at(
        f"heap-{corruption}-unrelated",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.b",
        decision_scope="other",
    )
    old = _intent_at(
        f"heap-{corruption}-old",
        BASE - timedelta(seconds=10),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    waiting = _intent_at(
        f"heap-{corruption}-waiting",
        BASE,
        strategy_id="strategy.c",
        decision_scope="other",
    )
    first_newer = _intent_at(
        f"heap-{corruption}-first",
        BASE - timedelta(seconds=5),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    second_newer = _intent_at(
        f"heap-{corruption}-second",
        BASE - timedelta(seconds=4),
        strategy_id="strategy.a",
        decision_scope="entry",
    )

    assert admission.offer(unrelated) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(old) == (True, "accepted")
    assert admission.offer(waiting) == (True, "accepted")
    assert admission.offer(first_newer) == (
        False,
        "superseded_but_replay_capacity_exhausted",
    )
    guard_id = admission._scope_high_water[("strategy.a", "entry")]
    guard_before = admission._retired[guard_id]
    matching = [
        entry for entry in admission._retired_heap if entry[2] == guard_id
    ]
    assert len(matching) == 1
    original_entry = matching[0]

    if corruption == "missing":
        admission._retired_heap.remove(original_entry)
    elif corruption == "duplicate":
        admission._retired_heap.append(original_entry)
        heapq.heapify(admission._retired_heap)
    else:
        index = admission._retired_heap.index(original_entry)
        admission._retired_heap[index] = (
            original_entry[0] + timedelta(seconds=1),
            original_entry[1],
            original_entry[2],
        )
        heapq.heapify(admission._retired_heap)

    heap_before_offer = tuple(admission._retired_heap)
    counts_before = dict(admission._strategy_replay_counts)
    with pytest.raises(ProductionStateError):
        admission.offer(second_newer)

    assert admission._retired[guard_id] == guard_before
    assert tuple(admission._retired_heap) == heap_before_offer
    assert admission._strategy_replay_counts == counts_before


def test_repeated_capacity_rejection_advances_existing_retired_high_water(
    journal, state_dir
):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
    )
    unrelated = _intent_at(
        "repeat-cap-unrelated",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.b",
        decision_scope="other",
    )
    old = _intent_at(
        "repeat-cap-old",
        BASE - timedelta(seconds=10),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    waiting = _intent_at(
        "repeat-cap-waiting",
        BASE,
        strategy_id="strategy.c",
        decision_scope="other",
    )
    first_newer = _intent_at(
        "repeat-cap-first-newer",
        BASE - timedelta(seconds=5),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    second_newer = _intent_at(
        "repeat-cap-second-newer",
        BASE - timedelta(seconds=4),
        strategy_id="strategy.a",
        decision_scope="entry",
    )

    assert admission.offer(unrelated) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(old) == (True, "accepted")
    assert admission.offer(waiting) == (True, "accepted")
    assert admission.offer(first_newer) == (
        False,
        "superseded_but_replay_capacity_exhausted",
    )
    guard_id = admission._scope_high_water[("strategy.a", "entry")]
    assert guard_id == first_newer.intent_id

    first_expiry = admission._retired[guard_id].expires_at
    assert admission.offer(second_newer) == (False, "replay_capacity_exhausted")
    assert admission._scope_high_water[("strategy.a", "entry")] == guard_id
    assert admission._retired[guard_id].intent_id == guard_id
    assert admission._retired[guard_id].order_key == admission._decision_order_key(
        second_newer
    )
    assert admission._retired[guard_id].expires_at > first_expiry
    assert second_newer.intent_id not in admission._retired

    # Once an unrelated slot frees, evidence between the first and second rejected decisions
    # must still be blocked by the in-place advanced high-water.
    clock.value = BASE + timedelta(seconds=31)
    intermediate = _intent_at(
        "repeat-cap-intermediate",
        BASE - timedelta(seconds=4, milliseconds=500),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    assert admission.offer(intermediate) == (False, "superseded_by_scope_high_water")
    assert admission.offer(first_newer) == (False, "duplicate_intent_id")
    assert admission.offer(second_newer) == (False, "ambiguous_same_scope_decision")


def test_thousand_newer_capacity_rejections_reuse_one_high_water_slot(
    journal, state_dir
):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
    )
    unrelated = _intent_at(
        "newer-flood-unrelated",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.b",
        decision_scope="other",
    )
    old = _intent_at(
        "newer-flood-old",
        BASE - timedelta(seconds=10),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    waiting = _intent_at(
        "newer-flood-waiting",
        BASE,
        strategy_id="strategy.c",
        decision_scope="other",
    )
    first_newer = _intent_at(
        "newer-flood-first",
        BASE - timedelta(seconds=2),
        strategy_id="strategy.a",
        decision_scope="entry",
    )

    assert admission.offer(unrelated) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(old) == (True, "accepted")
    assert admission.offer(waiting) == (True, "accepted")
    assert admission.offer(first_newer) == (
        False,
        "superseded_but_replay_capacity_exhausted",
    )

    guard_id = admission._scope_high_water[("strategy.a", "entry")]
    baseline_retired = len(admission._retired)
    baseline_heap = len(admission._retired_heap)
    baseline_replay_counts = dict(admission._strategy_replay_counts)

    latest = first_newer
    for index in range(1000):
        latest = _intent_at(
            f"newer-flood-{index}",
            BASE - timedelta(microseconds=1000 - index),
            strategy_id="strategy.a",
            decision_scope="entry",
        )
        assert admission.offer(latest) == (False, "replay_capacity_exhausted")
        assert admission._scope_high_water[("strategy.a", "entry")] == guard_id
        assert len(admission._retired) == baseline_retired
        assert len(admission._retired_heap) == baseline_heap
        assert admission._strategy_replay_counts == baseline_replay_counts

    guard = admission._retired[guard_id]
    assert guard.intent_id == guard_id
    assert guard.order_key == admission._decision_order_key(latest)
    assert guard.expires_at == admission._replay_expiry(latest)
    with admission._lock:
        admission._assert_replay_invariants_locked()


def test_capacity_rejection_advances_active_high_water_without_losing_active_identity(
    journal, state_dir
):
    entered = threading.Event()
    release = threading.Event()

    class BlockingSecondSubmitPort(RecordingExecutionPort):
        def __init__(self, instrument):
            super().__init__(instrument)
            self._submit_calls = 0

        def submit(self, request):  # noqa: ANN001 - deterministic concurrency test double
            self._submit_calls += 1
            if self._submit_calls == 2:
                entered.set()
                assert release.wait(timeout=5.0)
            return super().submit(request)

    port, runtime = _started(
        journal,
        state_dir,
        port=BlockingSecondSubmitPort(INSTRUMENT),
    )
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
    )
    unrelated = _intent_at(
        "active-cap-unrelated",
        BASE - timedelta(seconds=30),
        strategy_id="strategy.b",
        decision_scope="other",
    )
    active = _intent_at(
        "active-cap-base",
        BASE - timedelta(seconds=5),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    waiting = _intent_at(
        "active-cap-waiting",
        BASE,
        strategy_id="strategy.c",
        decision_scope="other",
    )
    newer = _intent_at(
        "active-cap-newer",
        BASE - timedelta(seconds=4),
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    intermediate = _intent_at(
        "active-cap-intermediate",
        BASE - timedelta(seconds=4, milliseconds=500),
        strategy_id="strategy.a",
        decision_scope="entry",
    )

    assert admission.offer(unrelated) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert admission.offer(active) == (True, "accepted")
    assert admission.offer(waiting) == (True, "accepted")

    producer_results: list[tuple[bool, str]] = []

    def producer() -> None:
        assert entered.wait(timeout=5.0)
        producer_results.append(admission.offer(newer))
        producer_results.append(admission.offer(intermediate))
        producer_results.append(admission.offer(active))
        release.set()

    worker = threading.Thread(target=producer, daemon=True)
    worker.start()
    outcome = admission.run_once()
    worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert outcome is not None and outcome.submitted is True
    assert producer_results == [
        (False, "replay_capacity_exhausted"),
        (False, "superseded_by_scope_high_water"),
        (False, "duplicate_intent_id"),
    ]
    assert admission.active_intent_id() is None
    assert admission._scope_high_water[("strategy.a", "entry")] == active.intent_id
    active_guard = admission._retired[active.intent_id]
    assert active_guard.intent_id == active.intent_id
    assert active_guard.order_key == admission._decision_order_key(newer)
    assert active_guard.expires_at == admission._replay_expiry(newer)
    assert newer.intent_id not in admission._retired
    assert port.submit_count == 2


def test_expired_active_heap_head_does_not_block_pruning_other_expired_records(
    journal, state_dir
):
    clock = MutableClock(BASE + timedelta(seconds=1))
    _port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, clock)

    active = _intent_at(
        "active-prune-head",
        BASE,
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    other = _intent_at(
        "active-prune-other",
        BASE + timedelta(seconds=1),
        strategy_id="strategy.b",
        decision_scope="entry",
    )
    assert admission.offer(active) == (True, "accepted")
    assert admission.offer(other) == (True, "accepted")

    with admission._lock:
        admission._convert_queued_to_retired_locked(active)
        admission._active_intent_id = active.intent_id
        admission._convert_queued_to_retired_locked(other)

    clock.value = BASE + timedelta(seconds=62)
    probe = _intent_at(
        "active-prune-probe",
        clock.value,
        strategy_id="strategy.c",
        decision_scope="probe",
    )
    assert admission.offer(probe) == (True, "accepted")
    assert active.intent_id in admission._retired
    assert active.intent_id in [entry[2] for entry in admission._retired_heap]
    assert other.intent_id not in admission._retired
    assert other.intent_id not in [entry[2] for entry in admission._retired_heap]

    with admission._lock:
        admission._active_intent_id = None
        admission._assert_replay_invariants_locked()


def test_replay_boundary_prunes_ledger_and_high_water(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    clock = MutableClock(BASE)
    admission = _admission(runtime, clock)
    target = _intent_at(
        "replay-boundary",
        BASE,
        strategy_id="strategy.a",
        decision_scope="entry",
    )

    assert admission.offer(target) == (True, "accepted")
    assert admission.run_once().submitted is True

    clock.value = BASE + timedelta(seconds=60)
    assert admission.offer(target) == (False, "duplicate_intent_id")
    assert target.intent_id in admission._retired

    clock.value = BASE + timedelta(seconds=60, microseconds=1)
    assert admission.offer(target) == (False, "max_age_exceeded")
    assert admission._retired == {}
    assert admission._retired_heap == []
    assert admission._scope_high_water == {}


def test_rejected_older_uuid_flood_does_not_grow_replay_state(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, lambda: BASE)
    newer = _intent_at(
        "flood-high-water",
        BASE,
        strategy_id="strategy.a",
        decision_scope="entry",
    )
    assert admission.offer(newer) == (True, "accepted")
    assert admission.run_once().submitted is True

    baseline = (
        len(admission._retired),
        len(admission._retired_heap),
        len(admission._scope_high_water),
        admission.depth(),
    )
    for index in range(1000):
        older = _intent_at(
            f"flood-older-{index}",
            BASE - timedelta(seconds=1),
            strategy_id="strategy.a",
            decision_scope="entry",
        )
        assert admission.offer(older) == (False, "superseded_by_scope_high_water")

    assert (
        len(admission._retired),
        len(admission._retired_heap),
        len(admission._scope_high_water),
        admission.depth(),
    ) == baseline


def test_brand_new_scope_capacity_rejection_sets_strategy_floor_and_blocks_older_equal(
    journal, state_dir
):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
        max_rejection_strategies=2,
    )

    for index, stamp in enumerate(
        (
            BASE - timedelta(seconds=30),
            BASE - timedelta(seconds=20),
            BASE - timedelta(seconds=10),
        )
    ):
        queued = _intent_at(
            f"floor-fill-{index}",
            stamp,
            strategy_id=f"strategy.fill-{index}",
            decision_scope=f"scope-{index}",
        )
        assert admission.offer(queued) == (True, "accepted")
        assert admission.run_once().submitted is True

    rejected = _intent_at(
        "floor-new-scope",
        BASE,
        strategy_id="strategy.a",
        decision_scope="brand-new",
    )
    assert admission.offer(rejected) == (False, "replay_capacity_exhausted")
    floor = admission._strategy_rejection_floor["strategy.a"]
    assert floor.order_key == admission._decision_order_key(rejected)
    assert len(admission._strategy_rejection_floor) == 1
    assert admission._global_rejection_floor is None

    clock.value = BASE + timedelta(seconds=31)
    older = _intent_at(
        "floor-older",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.a",
        decision_scope="another-new-scope",
    )
    equal = _intent_at(
        "floor-equal",
        BASE,
        strategy_id="strategy.a",
        decision_scope="yet-another-scope",
    )
    newer = _intent_at(
        "floor-newer",
        clock.value,
        strategy_id="strategy.a",
        decision_scope="fresh-scope",
    )

    assert admission.offer(older) == (
        False,
        "superseded_by_capacity_rejection_floor",
    )
    assert admission.offer(equal) == (
        False,
        "superseded_by_capacity_rejection_floor",
    )
    assert admission.offer(rejected) == (
        False,
        "superseded_by_capacity_rejection_floor",
    )
    assert admission.offer(newer) == (True, "accepted")


def test_rejection_floor_overflow_uses_bounded_global_floor_and_remains_fail_closed(
    journal, state_dir
):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
        max_rejection_strategies=1,
    )

    for index, stamp in enumerate(
        (
            BASE - timedelta(seconds=30),
            BASE - timedelta(seconds=20),
            BASE - timedelta(seconds=10),
        )
    ):
        queued = _intent_at(
            f"global-floor-fill-{index}",
            stamp,
            strategy_id=f"strategy.fill-{index}",
            decision_scope=f"scope-{index}",
        )
        assert admission.offer(queued) == (True, "accepted")
        assert admission.run_once().submitted is True

    strategy_floor_source = _intent_at(
        "global-floor-strategy-a",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.a",
        decision_scope="new-a",
    )
    global_floor_source = _intent_at(
        "global-floor-strategy-b",
        BASE,
        strategy_id="strategy.b",
        decision_scope="new-b",
    )

    assert admission.offer(strategy_floor_source) == (
        False,
        "replay_capacity_exhausted",
    )
    assert admission.offer(global_floor_source) == (
        False,
        "replay_capacity_exhausted",
    )
    assert len(admission._strategy_rejection_floor) == 1
    assert admission._global_rejection_floor is not None
    assert (
        admission._global_rejection_floor.order_key
        == admission._decision_order_key(global_floor_source)
    )

    clock.value = BASE + timedelta(seconds=31)
    lagging = _intent_at(
        "global-floor-lagging",
        BASE - timedelta(milliseconds=500),
        strategy_id="strategy.c",
        decision_scope="new-c",
    )
    fresh = _intent_at(
        "global-floor-fresh",
        clock.value,
        strategy_id="strategy.c",
        decision_scope="fresh-c",
    )
    assert admission.offer(lagging) == (
        False,
        "superseded_by_global_capacity_rejection_floor",
    )
    assert admission.offer(fresh) == (True, "accepted")
    assert len(admission._strategy_rejection_floor) <= admission.max_rejection_strategies


def test_rejection_floors_expire_without_leaking_memory(journal, state_dir):
    clock = MutableClock(BASE)
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=3,
        max_per_strategy_retired=2,
        max_rejection_strategies=1,
    )

    for index, stamp in enumerate(
        (
            BASE - timedelta(seconds=30),
            BASE - timedelta(seconds=20),
            BASE - timedelta(seconds=10),
        )
    ):
        queued = _intent_at(
            f"floor-expiry-fill-{index}",
            stamp,
            strategy_id=f"strategy.fill-{index}",
            decision_scope=f"scope-{index}",
        )
        assert admission.offer(queued) == (True, "accepted")
        assert admission.run_once().submitted is True

    first = _intent_at(
        "floor-expiry-first",
        BASE - timedelta(seconds=1),
        strategy_id="strategy.a",
        decision_scope="a",
    )
    overflow = _intent_at(
        "floor-expiry-overflow",
        BASE,
        strategy_id="strategy.b",
        decision_scope="b",
    )
    assert admission.offer(first) == (False, "replay_capacity_exhausted")
    assert admission.offer(overflow) == (False, "replay_capacity_exhausted")
    assert admission._strategy_rejection_floor
    assert admission._global_rejection_floor is not None

    clock.value = BASE + timedelta(seconds=61)
    fresh = _intent_at(
        "floor-expiry-fresh",
        clock.value,
        strategy_id="strategy.c",
        decision_scope="fresh",
    )
    assert admission.offer(fresh) == (True, "accepted")
    assert admission._strategy_rejection_floor == {}
    assert admission._global_rejection_floor is None


def test_saturated_replay_ledger_rejects_without_exceeding_hard_bounds(
    journal, state_dir
):
    port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=8,
        max_per_strategy_retired=3,
    )

    accepted = 0
    rejected = 0
    for index in range(1000):
        candidate = _intent_at(
            f"saturated-{index}",
            BASE,
            strategy_id=f"strategy.{index % 4}",
            decision_scope=f"scope-{index}",
        )
        result = admission.offer(candidate)
        if result == (True, "accepted"):
            accepted += 1
            outcome = admission.run_once()
            assert outcome is not None and outcome.submitted is True
        else:
            rejected += 1
            assert result[0] is False
            assert result[1] in {
                "replay_capacity_exhausted",
                "superseded_by_capacity_rejection_floor",
                "superseded_by_global_capacity_rejection_floor",
            }

        assert len(admission._retired) + admission.depth() <= admission.max_retired
        assert len(admission._retired_heap) == len(admission._retired)
        assert len(admission._scope_high_water) <= len(admission._retired)
        strategy_counts: dict[str, int] = {}
        for record in admission._retired.values():
            strategy_counts[record.strategy_id] = strategy_counts.get(record.strategy_id, 0) + 1
        assert all(
            count <= admission.max_per_strategy_retired
            for count in strategy_counts.values()
        )

    assert accepted == admission.max_retired
    assert rejected == 1000 - admission.max_retired
    assert len(admission._retired) == admission.max_retired
    assert admission.depth() == 0
    assert port.submit_count == admission.max_retired


def test_sliding_thousand_intents_never_exceed_hard_replay_bounds(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    clock = MutableClock(BASE)
    admission = _admission(
        runtime,
        clock,
        max_size=2,
        max_per_strategy_queued=1,
        max_retired=4,
        max_per_strategy_retired=3,
    )

    for index in range(1000):
        candidate = _intent_at(
            f"sliding-{index}",
            clock.value,
            strategy_id="strategy.a",
            decision_scope="entry",
        )
        assert admission.offer(candidate) == (True, "accepted")
        assert len(admission._retired) + admission.depth() <= admission.max_retired
        assert len(admission._retired_heap) == len(admission._retired)
        assert len(admission._scope_high_water) <= len(admission._retired)
        clock.value += timedelta(seconds=61)


def test_racing_same_scope_producers_always_leave_newer_queued(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=128,
        max_per_strategy_queued=127,
        max_retired=256,
        max_per_strategy_retired=200,
    )

    for index in range(100):
        scope = f"race-{index}"
        older = _intent_at(
            f"race-old-{index}",
            BASE - timedelta(seconds=1),
            strategy_id="strategy.race",
            decision_scope=scope,
        )
        newer = _intent_at(
            f"race-new-{index}",
            BASE,
            strategy_id="strategy.race",
            decision_scope=scope,
        )
        barrier = threading.Barrier(3)

        def producer(candidate):
            barrier.wait(timeout=5.0)
            admission.offer(candidate)

        old_thread = threading.Thread(target=producer, args=(older,), daemon=True)
        new_thread = threading.Thread(target=producer, args=(newer,), daemon=True)
        old_thread.start()
        new_thread.start()
        barrier.wait(timeout=5.0)
        old_thread.join(timeout=5.0)
        new_thread.join(timeout=5.0)

        assert not old_thread.is_alive()
        assert not new_thread.is_alive()
        queued = set(admission.queued_intent_ids())
        assert newer.intent_id in queued
        assert older.intent_id not in queued


def test_active_intent_is_not_pruned_past_replay_expiry(journal, state_dir):
    clock = MutableClock(BASE)
    admission_box: dict[str, ProductionAdmissionController] = {}

    class ExpiryProbePort(RecordingExecutionPort):
        def submit(self, request):  # noqa: ANN001 - deterministic in-flight probe
            admission = admission_box["admission"]
            assert admission.active_intent_id() == request.intent_id
            clock.value = BASE + timedelta(seconds=61)
            probe = _intent_at(
                "active-expiry-probe",
                clock.value,
                strategy_id="strategy.b",
                decision_scope="probe",
            )
            assert admission.offer(probe) == (True, "accepted")
            assert request.intent_id in admission._retired
            assert admission.active_intent_id() == request.intent_id
            return super().submit(request)

    port, runtime = _started(journal, state_dir, port=ExpiryProbePort(INSTRUMENT))
    admission = _admission(runtime, clock)
    admission_box["admission"] = admission
    target = _intent_at(
        "active-expiry-target",
        BASE,
        strategy_id="strategy.a",
        decision_scope="entry",
    )

    assert admission.offer(target) == (True, "accepted")
    outcome = admission.run_once()
    assert outcome is not None and outcome.submitted is True
    assert port.submit_count == 1
    assert admission.active_intent_id() is None

    # Once execution has finished, the next admission pass may prune the expired replay record.
    follow_up = _intent_at(
        "active-expiry-follow-up",
        clock.value,
        strategy_id="strategy.c",
        decision_scope="follow-up",
    )
    assert admission.offer(follow_up) == (True, "accepted")
    assert target.intent_id not in admission._retired
    with admission._lock:
        admission._assert_replay_invariants_locked()


def test_clock_regression_fails_closed_after_replay_prune(journal, state_dir):
    _port, runtime = _started(journal, state_dir)
    clock = MutableClock(BASE)
    admission = _admission(runtime, clock)
    target = _intent_at("clock-regression-target", BASE)

    assert admission.offer(target) == (True, "accepted")
    assert admission.run_once().submitted is True

    clock.value = BASE + timedelta(seconds=61)
    fresh = _intent_at("clock-regression-fresh", clock.value, decision_scope="fresh")
    assert admission.offer(fresh) == (True, "accepted")
    assert target.intent_id not in admission._retired

    before_queue = admission.queued_intent_ids()
    clock.value = BASE + timedelta(seconds=30)
    with pytest.raises(ProductionStateError, match="clock moved backwards"):
        admission.offer(target)
    assert admission.queued_intent_ids() == before_queue


@pytest.mark.parametrize(
    "overrides,reason",
    [
        (
            {"decision_completed_at": BASE + timedelta(seconds=1)},
            "decision_completed_in_future",
        ),
        (
            {
                "decision_started_at": BASE + timedelta(seconds=1),
                "decision_completed_at": BASE,
            },
            "decision_time_order_invalid",
        ),
        (
            {
                "decision_completed_at": BASE - timedelta(seconds=1),
                "market_event_ts": BASE,
            },
            "decision_time_order_invalid",
        ),
    ],
)
def test_decision_chronology_rejections_do_not_take_capacity(
    journal, state_dir, overrides, reason
):
    _port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, lambda: BASE)
    candidate = intent("chronology-reject", **overrides)

    assert admission.offer(candidate) == (False, reason)
    assert admission.depth() == 0
    assert admission._retired == {}


def test_bound_runtime_refuses_second_controller_and_direct_decision_path(
    journal, state_dir
):
    port, runtime = _started(journal, state_dir)
    admission = _admission(runtime, lambda: BASE)
    before_pending = runtime._pending.items()
    before_issued = dict(runtime._issued_preparations)

    with pytest.raises(ProductionStateError, match="already has an admission controller"):
        _admission(runtime, lambda: BASE)

    direct = _intent_at("bound-direct", BASE)
    with pytest.raises(ProductionStateError, match="admission-bound"):
        runtime.prepare_intent(direct)
    with pytest.raises(ProductionStateError, match="admission-bound"):
        runtime.execute_intent(direct)
    with pytest.raises(ProductionStateError, match="admission-bound"):
        runtime.submit_prepared(object())

    assert runtime._pending.items() == before_pending
    assert runtime._issued_preparations == before_issued
    assert port.submit_count == 0

    routed = _intent_at("bound-routed", BASE, decision_scope="routed")
    assert admission.offer(routed) == (True, "accepted")
    assert admission.run_once().submitted is True
    assert port.submit_count == 1


def test_failed_binding_with_outstanding_preparation_leaves_runtime_unbound(
    journal, state_dir
):
    port, runtime = _started(journal, state_dir)
    target = _intent_at("prebind-preparation", BASE)
    preparation = runtime.prepare_intent(target)
    assert preparation.approved is True

    with pytest.raises(ProductionStateError, match="outstanding preparations"):
        _admission(runtime, lambda: BASE)

    assert runtime._admission_token is None
    result = runtime.submit_prepared(preparation)
    assert result.intent_id == target.intent_id
    assert port.submit_count == 1


@pytest.mark.parametrize(
    "max_size,max_per_strategy,max_retired,max_per_strategy_retired,max_rejection_strategies",
    [
        (1, 1, 4, 2, 4),
        (2, 2, 4, 3, 4),
        (3, 3, 6, 4, 4),
        (True, 1, 4, 2, 4),
        (3, True, 6, 4, 4),
        (3, 1, 3, 2, 4),
        (3, 1, True, 2, 4),
        (3, 1, 6, 1, 4),
        (3, 1, 6, 6, 4),
        (3, 1, 6, True, 4),
        (3, 1, 6, 4, 0),
        (3, 1, 6, 4, 7),
        (3, 1, 6, 4, True),
    ],
)
def test_admission_rejects_unsafe_capacity_configuration(
    journal,
    state_dir,
    max_size,
    max_per_strategy,
    max_retired,
    max_per_strategy_retired,
    max_rejection_strategies,
):
    _port, runtime = _started(journal, state_dir)
    with pytest.raises(ProductionStateError):
        ProductionAdmissionController(
            runtime=runtime,
            max_size=max_size,
            max_per_strategy_queued=max_per_strategy,
            max_retired=max_retired,
            max_per_strategy_retired=max_per_strategy_retired,
            max_rejection_strategies=max_rejection_strategies,
            clock=lambda: BASE,
        )


def test_external_breakglass_remains_separate_from_trade_backlog(journal, state_dir):
    port, runtime = _started(journal, state_dir)
    admission = _admission(
        runtime,
        lambda: BASE,
        max_size=2,
        max_per_strategy_queued=1,
    )
    queued = intent("queued-before-halt", strategy_id="strategy.a")
    queued_two = intent(
        "queued-before-halt-two",
        strategy_id="strategy.b",
        decision_scope="other",
    )

    assert admission.offer(queued) == (True, "accepted")
    assert admission.offer(queued_two) == (True, "accepted")
    assert admission.depth() == admission.max_size
    outcome = runtime.control.handle(
        operator_command("halt_new_risk", seed="admission-independent-halt")
    )
    assert outcome.accepted is True

    result = admission.run_once()

    assert result is not None and result.submitted is False
    assert result.blocked_reason_code == "trading_halted"
    assert port.submit_count == 0
