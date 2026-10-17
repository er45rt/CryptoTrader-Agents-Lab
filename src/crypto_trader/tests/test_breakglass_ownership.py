"""Phase 13: single-writer ownership, fencing tokens and explicit takeover semantics."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from crypto_trader.breakglass import (
    OWNERSHIP_SCHEMA_VERSION,
    OwnerKind,
    OwnershipRecord,
    OwnershipStateError,
    OwnershipStore,
    StaleWriterFenceError,
    WriterFence,
    WriterGuard,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path: Path) -> OwnershipStore:
    return OwnershipStore(tmp_path / "ownership.json")


def test_an_absent_record_means_no_writer_and_refuses_writes(store):
    assert store.read() is None
    with pytest.raises(OwnershipStateError):
        store.require_fence()


def test_first_takeover_mints_term_one_and_the_normal_owner_may_create_risk(store):
    record = store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
    )

    assert record.term == 1
    assert record.previous_owner_id is None
    assert store.require_fence().allows_new_risk is True


def test_emergency_takeover_invalidates_the_normal_writers_token(store):
    normal = store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
    )
    guard = WriterGuard(store, normal.fence)
    assert guard.may_create_new_risk() is True

    emergency = store.takeover(
        owner_id="emergency:jamal",
        owner_kind=OwnerKind.EMERGENCY,
        acquired_at=BASE,
        reason_code="break_glass:cancel_open",
    )

    assert emergency.term == normal.term + 1
    assert emergency.previous_owner_id == normal.owner_id
    assert guard.may_create_new_risk() is False
    with pytest.raises(StaleWriterFenceError):
        guard.require_current()
    with pytest.raises(StaleWriterFenceError):
        store.require_current(normal.fence)


def test_only_the_normal_owner_may_create_new_risk(store):
    record = store.takeover(
        owner_id="emergency:jamal",
        owner_kind=OwnerKind.EMERGENCY,
        acquired_at=BASE,
        reason_code="break_glass:halt_new_risk",
    )

    assert record.fence.allows_new_risk is False
    assert WriterGuard(store, record.fence).may_create_new_risk() is False


def test_a_restore_mints_a_fresh_normal_term_so_the_emergency_token_dies(store):
    store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
    )
    emergency = store.takeover(
        owner_id="emergency:jamal",
        owner_kind=OwnerKind.EMERGENCY,
        acquired_at=BASE,
        reason_code="break_glass:halt_new_risk",
    )
    restored = store.takeover(
        owner_id="normal:jamal",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="operator_restore_normal",
    )

    assert restored.term == emergency.term + 1
    with pytest.raises(StaleWriterFenceError):
        store.require_current(emergency.fence)
    assert WriterGuard(store, restored.fence).may_create_new_risk() is True


def test_terms_increase_monotonically_across_many_takeovers(store):
    terms = [
        store.takeover(
            owner_id=f"owner-{index}",
            owner_kind=OwnerKind.EMERGENCY,
            acquired_at=BASE,
            reason_code="break_glass:halt_new_risk",
        ).term
        for index in range(5)
    ]
    assert terms == [1, 2, 3, 4, 5]
    assert store.next_term() == 6


def test_a_corrupt_ownership_record_fails_closed(store: OwnershipStore):
    store.path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(OwnershipStateError):
        store.read()
    with pytest.raises(OwnershipStateError):
        store.require_fence()


@pytest.mark.parametrize("present", ["journal_path", "journal_realpath"])
def test_a_partially_bound_ownership_record_fails_closed_before_any_change(
    store: OwnershipStore, tmp_path: Path, present: str
):
    # Review N-03: one binding field without the other must never read as an unbound lease,
    # or a takeover could re-bind the state dir to another system's journal.
    payload = {
        "schema_version": OWNERSHIP_SCHEMA_VERSION,
        "term": 1,
        "owner_id": "normal:runtime",
        "owner_kind": "normal",
        "acquired_at": "2026-01-01T12:00:00+00:00",
        "previous_owner_id": None,
        "reason_code": "runtime_started",
        "session_id": "00000000-0000-4000-8000-000000000000",
        present: str(tmp_path / "system-a" / "journal.db"),
    }
    raw = json.dumps(payload)
    store.path.write_text(raw, encoding="utf-8")

    with pytest.raises(OwnershipStateError, match="partially bound to a journal"):
        store.read()
    with pytest.raises(OwnershipStateError, match="partially bound to a journal"):
        store.takeover(
            owner_id="normal:other",
            owner_kind=OwnerKind.NORMAL,
            acquired_at=BASE,
            reason_code="runtime_started",
            journal_path=str(tmp_path / "system-b" / "journal.db"),
        )
    assert store.path.read_text(encoding="utf-8") == raw


def test_an_unknown_ownership_schema_is_refused(store: OwnershipStore):
    store.path.write_text(
        '{"schema_version": 99, "term": 1, "owner_id": "x", "owner_kind": "normal", '
        '"acquired_at": "2026-01-01T12:00:00+00:00", "previous_owner_id": null, '
        '"reason_code": "x", "session_id": "00000000-0000-4000-8000-000000000000"}',
        encoding="utf-8",
    )
    with pytest.raises(OwnershipStateError):
        store.read()


def test_records_round_trip_without_losing_the_fence(store):
    record = store.takeover(
        owner_id="emergency:jamal",
        owner_kind=OwnerKind.EMERGENCY,
        acquired_at=BASE,
        reason_code="break_glass:halt_new_risk",
        session_id=uuid4(),
    )
    restored = OwnershipRecord.from_record(record.to_record())

    assert restored == record
    assert restored.schema_version == OWNERSHIP_SCHEMA_VERSION


def test_a_naive_timestamp_is_never_accepted():
    with pytest.raises(OwnershipStateError):
        OwnershipRecord(
            term=1,
            owner_id="normal:runtime",
            owner_kind=OwnerKind.NORMAL,
            acquired_at=datetime(2026, 1, 1, 12, 0, 0),
            previous_owner_id=None,
            reason_code="runtime_started",
            session_id=uuid4(),
        )


def test_fence_rejects_impossible_values():
    with pytest.raises(OwnershipStateError):
        WriterFence(term=-1, owner_id="x", owner_kind=OwnerKind.NORMAL)
    with pytest.raises(OwnershipStateError):
        WriterFence(term=1, owner_id="", owner_kind=OwnerKind.NORMAL)
