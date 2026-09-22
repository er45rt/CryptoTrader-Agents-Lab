"""Phase 9: priority safety-lane tests (bypasses AI backlog, bounded normal lane)."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from crypto_trader.safety import (
    LaneBackpressureError,
    LaneCommand,
    LanePriority,
    PrioritySafetyLane,
    SafetyConfigError,
    SafetyStateError,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def command(kind: str, priority: LanePriority, *, command_id=None) -> LaneCommand:
    return LaneCommand(
        command_id=command_id or uuid4(),
        kind=kind,
        priority=priority,
        enqueued_at=BASE,
    )


def lane(*, max_normal_depth: int = 2) -> PrioritySafetyLane:
    return PrioritySafetyLane(max_normal_depth=max_normal_depth)


def test_normal_lane_requires_bounded_capacity():
    with pytest.raises(SafetyConfigError):
        PrioritySafetyLane(max_normal_depth=0)
    with pytest.raises(SafetyConfigError):
        PrioritySafetyLane(max_normal_depth=-1)  # type: ignore[arg-type]


def test_safety_commands_are_drained_before_normal_work():
    queue = lane()
    normal = command("submit_intent", LanePriority.NORMAL)
    safety = command("cancel_all", LanePriority.SAFETY)
    queue.submit(normal)
    queue.submit(safety)

    assert queue.next_command() is safety
    assert queue.next_command() is normal
    assert queue.next_command() is None


def test_kill_switch_runs_while_the_ai_backlog_is_saturated():
    queue = lane(max_normal_depth=3)
    for index in range(3):
        queue.submit(command(f"ai_intent_{index}", LanePriority.NORMAL))

    kill = command("kill_switch", LanePriority.SAFETY)
    assert queue.submit(kill) is True

    # The safety lane is unblocked by the full/backpressured normal lane.
    assert queue.next_command() is kill
    assert queue.safety_depth == 0
    assert queue.normal_depth == 3


def test_extra_normal_work_fails_closed_with_backpressure():
    queue = lane(max_normal_depth=1)
    queue.submit(command("ai_intent", LanePriority.NORMAL))

    with pytest.raises(LaneBackpressureError):
        queue.submit(command("another_intent", LanePriority.NORMAL))

    # Safety commands are still accepted after normal backpressure.
    assert queue.submit(command("halt", LanePriority.SAFETY)) is True


def test_safety_lane_never_rejects_even_when_normal_is_full():
    queue = lane(max_normal_depth=1)
    queue.submit(command("ai_intent", LanePriority.NORMAL))

    for index in range(50):
        assert queue.submit(command(f"safety_{index}", LanePriority.SAFETY)) is True

    assert queue.safety_depth == 50


def test_duplicate_command_ids_are_deduplicated():
    queue = lane()
    kill = command("kill_switch", LanePriority.SAFETY)

    assert queue.submit(kill) is True
    assert queue.submit(kill) is False
    assert queue.safety_depth == 1


def test_deduplication_holds_across_lanes():
    queue = lane()
    command_id = uuid4()
    queue.submit(command("halt", LanePriority.SAFETY, command_id=command_id))

    assert queue.submit(command("halt", LanePriority.NORMAL, command_id=command_id)) is False
    assert len(queue) == 1


def test_forget_clears_the_idempotency_key_after_processing():
    queue = lane()
    kill = command("kill_switch", LanePriority.SAFETY)
    queue.submit(kill)

    queue.forget(kill.command_id)

    assert queue.submit(kill) is True


def test_drain_respects_priority_order_and_the_limit():
    queue = lane()
    safety_one = command("halt", LanePriority.SAFETY)
    safety_two = command("cancel_all", LanePriority.SAFETY)
    normal_one = command("ai_intent", LanePriority.NORMAL)
    for item in (normal_one, safety_one, safety_two):
        queue.submit(item)

    first = queue.drain(limit=2)
    second = queue.drain(limit=5)

    assert first == [safety_one, safety_two]
    assert second == [normal_one]


def test_drain_with_zero_limit_is_a_no_op():
    queue = lane()
    queue.submit(command("halt", LanePriority.SAFETY))

    assert queue.drain(limit=0) == []
    assert queue.safety_depth == 1


def test_drain_rejects_a_negative_limit():
    with pytest.raises(SafetyStateError):
        lane().drain(limit=-1)


def test_commands_require_a_kind_and_priority():
    with pytest.raises(SafetyStateError):
        LaneCommand(command_id=uuid4(), kind="", priority=LanePriority.SAFETY)
    with pytest.raises(SafetyStateError):
        LaneCommand(command_id=uuid4(), kind="halt", priority="safety")  # type: ignore[arg-type]


def test_submit_requires_a_lane_command():
    with pytest.raises(SafetyStateError):
        lane().submit("halt")  # type: ignore[arg-type]


def test_lane_is_deterministic_for_the_same_command_sequence():
    def run() -> list[str]:
        queue = lane()
        for kind, priority in (
            ("ai_1", LanePriority.NORMAL),
            ("halt", LanePriority.SAFETY),
            ("ai_2", LanePriority.NORMAL),
            ("cancel_all", LanePriority.SAFETY),
        ):
            queue.submit(command(kind, priority))
        return [item.kind for item in queue.drain(limit=10)]

    assert run() == run() == ["halt", "cancel_all", "ai_1", "ai_2"]
