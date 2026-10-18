"""Phase 13: single-writer ownership, fencing tokens and explicit takeover semantics."""

import json
import os
import re
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


@pytest.mark.parametrize("present", ["journal_path", "journal_realpath", "journal_id"])
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
        present: (
            "00000000-0000-4000-8000-0000000000aa" if present == "journal_id"
            else str(tmp_path / "system-a" / "journal.db")
        ),
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


def test_a_takeover_cannot_switch_the_lease_to_another_spelling_of_its_journal(
    store: OwnershipStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Review N-02: a link or junction and its target share one real path, but a writer and its
    # readers share -wal/-shm through one spelling, so a takeover may not switch the bound one.
    from crypto_trader.breakglass import ownership

    bound = tmp_path / "journal.db"
    alias = tmp_path / "alias" / "journal.db"
    resolve = ownership.journal_realpath

    def same_file(path: str) -> str:
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(alias)):
            return resolve(str(bound))
        return resolve(path)

    monkeypatch.setattr(ownership, "journal_realpath", same_file)
    store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
        journal_path=str(bound),
    )
    raw = store.path.read_text(encoding="utf-8")

    with pytest.raises(OwnershipStateError, match=re.escape(
        f"the trading-state lease is bound to journal {bound}, but {alias} is another spelling of "
        "that file; refusing to switch the journal path a writer and its readers share (-wal/-shm) "
        "while the lease is bound to it."
    )):
        store.takeover(
            owner_id="emergency:jamal",
            owner_kind=OwnerKind.EMERGENCY,
            acquired_at=BASE,
            reason_code="break_glass:halt_new_risk",
            journal_path=str(alias),
        )
    assert store.path.read_text(encoding="utf-8") == raw


def test_a_takeover_keeps_the_recorded_journal_id_and_never_drops_or_switches_it(
    store: OwnershipStore, tmp_path: Path
):
    # Review F-02/F02-R1: a new binding records its journal's identity; a takeover through the bound
    # path must present that identity, and a lease bound without one is never given one.
    bound = str(tmp_path / "journal.db")
    first, other = uuid4(), uuid4()
    legacy_store = OwnershipStore(tmp_path / "legacy" / "ownership.json")
    legacy = legacy_store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
        journal_path=bound,
    )
    assert legacy.journal_id is None
    legacy_raw = legacy_store.path.read_text(encoding="utf-8")
    assert "journal_id" not in json.loads(legacy_raw)
    with pytest.raises(OwnershipStateError, match=re.escape(
        f"the trading-state lease is bound to journal {bound} without a journal_id, but the "
        f"journal open at {bound} has journal_id {first}; refusing to pair a journal with another "
        "system's ownership state."
    )):
        legacy_store.takeover(
            owner_id="normal:runtime",
            owner_kind=OwnerKind.NORMAL,
            acquired_at=BASE,
            reason_code="runtime_started",
            journal_path=bound,
            journal_id=first,
        )
    assert legacy_store.path.read_text(encoding="utf-8") == legacy_raw

    upgraded = store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
        journal_path=bound,
        journal_id=first,
    )
    assert upgraded.journal_id == first
    carried = store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
    )
    assert carried.journal_id == first
    assert store.read() == carried
    raw = store.path.read_text(encoding="utf-8")

    for presented in (None, other):
        found = "no journal_id" if presented is None else f"journal_id {presented}"
        with pytest.raises(OwnershipStateError, match=re.escape(
            f"the trading-state lease is bound to journal {bound} (journal_id {first}), but the "
            f"journal open at {bound} has {found}; refusing to pair a journal with another "
            "system's ownership state."
        )):
            store.takeover(
                owner_id="emergency:jamal",
                owner_kind=OwnerKind.EMERGENCY,
                acquired_at=BASE,
                reason_code="break_glass:halt_new_risk",
                journal_path=bound,
                journal_id=presented,
            )
    assert store.path.read_text(encoding="utf-8") == raw


def test_a_lease_bound_to_a_journal_identity_is_schema_2_which_older_builds_refuse(
    store: OwnershipStore, tmp_path: Path
):
    # Review F02-R2: a build before journal identities reads schema 1, ignores journal_id and
    # rewrites the lease without it on its next takeover; it refuses any other schema, so an
    # identity-bound lease is written as schema 2. A lease without an identity stays schema 1.
    bound = str(tmp_path / "journal.db")
    id_less = OwnershipStore(tmp_path / "id-less" / "ownership.json")
    id_less.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
        journal_path=bound,
    )
    assert json.loads(id_less.path.read_text(encoding="utf-8"))["schema_version"] == 1

    journal_id = uuid4()
    store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
        journal_path=bound,
        journal_id=journal_id,
    )
    assert json.loads(store.path.read_text(encoding="utf-8"))["schema_version"] == 2
    carried = store.takeover(
        owner_id="emergency:jamal",
        owner_kind=OwnerKind.EMERGENCY,
        acquired_at=BASE,
        reason_code="break_glass:halt_new_risk",
    )
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert (payload["schema_version"], payload["journal_id"]) == (2, str(journal_id))
    assert store.read() == carried
    assert carried.schema_version == 2


@pytest.mark.parametrize(
    ("schema_version", "journal_id", "reason"),
    [
        (1, "00000000-0000-4000-8000-0000000000aa", "ownership schema 1 never carries a journal_id"),
        (2, None, "ownership schema 2 requires a journal_id"),
    ],
)
def test_an_ownership_schema_that_disagrees_with_its_journal_id_fails_closed(
    store: OwnershipStore, tmp_path: Path, schema_version: int, journal_id: str | None, reason: str
):
    # Review F02-R2: schema 1 with a journal_id (as 2aae436 wrote it) is readable by older builds
    # that would drop it; schema 2 without one is not an identity-bound lease. Neither is repaired.
    journal = str(tmp_path / "journal.db")
    payload = {
        "schema_version": schema_version,
        "term": 1,
        "owner_id": "normal:runtime",
        "owner_kind": "normal",
        "acquired_at": "2026-01-01T12:00:00+00:00",
        "previous_owner_id": None,
        "reason_code": "runtime_started",
        "session_id": "00000000-0000-4000-8000-000000000000",
        "journal_path": journal,
        "journal_realpath": journal,
        **({} if journal_id is None else {"journal_id": journal_id}),
    }
    raw = json.dumps(payload)
    store.path.write_text(raw, encoding="utf-8")

    with pytest.raises(OwnershipStateError, match=reason):
        store.read()
    with pytest.raises(OwnershipStateError, match=reason):
        store.takeover(
            owner_id="emergency:jamal",
            owner_kind=OwnerKind.EMERGENCY,
            acquired_at=BASE,
            reason_code="break_glass:halt_new_risk",
        )
    assert store.path.read_text(encoding="utf-8") == raw


def test_a_malformed_persisted_journal_id_fails_closed(store: OwnershipStore, tmp_path: Path):
    store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
        journal_path=str(tmp_path / "journal.db"),
        journal_id=uuid4(),
    )
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    for malformed in ("not-a-uuid", 7):
        payload["journal_id"] = malformed
        store.path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(OwnershipStateError, match="the ownership record is not usable"):
            store.read()


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
