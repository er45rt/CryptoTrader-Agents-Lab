"""Stage-1 synthetic-only registry contract regressions. No strategy or venue execution."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from uuid import UUID
import json
import sqlite3

import pytest

from crypto_trader.validation.canonical import (
    CanonicalError, canonical_bytes, decimal_string, parse_json_strict,
    utc_nanoseconds, canonical_uuid, input_manifest,
)
from crypto_trader.validation.contracts import StudySpec, RegistryError
from crypto_trader.validation.registry import TrialRegistry


def _spec(*, capacity=3):
    return StudySpec(
        study_id="11111111-1111-4111-8111-111111111111",
        preregistration_hash="a" * 64,
        n_max=capacity,
        allowed_paths=("src/crypto_trader/paper/session.py",),
        cutoff="UNKNOWN",
    )


def _manifest():
    return {"src/crypto_trader/paper/session.py": "b" * 64}


def _create(tmp_path, *, capacity=3):
    reg = TrialRegistry(tmp_path / "trials.sqlite")
    reg.register_study(_spec(capacity=capacity))
    return reg


def test_canonical_vectors_and_strict_json():
    assert canonical_bytes({"a": "x", "b": ["y"]}) == canonical_bytes({"b": ["y"], "a": "x"})
    assert decimal_string("1.0", quantum="0.01") == "1.00"
    assert decimal_string("1.00", quantum="0.01") == "1.00"
    for value in ("1E0", "-0", "NaN", "sNaN", "Infinity", "1.001", "+2", " 2"):
        with pytest.raises(CanonicalError):
            decimal_string(value, quantum="0.01")
    for value in (float("nan"), float("inf"), 0.1):
        with pytest.raises(CanonicalError):
            canonical_bytes({"unsafe": value})
    for item in ('{"a":1,"a":2}', '{"a":NaN}', '{"é":1,"é":2}'):
        with pytest.raises(CanonicalError):
            parse_json_strict(item)
    with pytest.raises(CanonicalError):
        canonical_bytes({"s": "e\u0301"})
    assert parse_json_strict('{"b":"B","a":"A"}') == {"a": "A", "b": "B"}


def test_timestamp_uuid_and_manifest_strictness():
    assert utc_nanoseconds("2026-01-01T00:00:00.123456789Z") == "2026-01-01T00:00:00.123456789Z"
    for bad in ("2026-01-01T00:00:00Z", "2026-01-01T03:00:00.123456789+03:00",
                "2026-02-30T00:00:00.000000000Z"):
        with pytest.raises(CanonicalError):
            utc_nanoseconds(bad)
    assert canonical_uuid("11111111-1111-4111-8111-111111111111") == "11111111-1111-4111-8111-111111111111"
    with pytest.raises(CanonicalError):
        canonical_uuid("11111111-1111-4111-8111-11111111111A")
    assert input_manifest(_manifest(), allowed_paths=_spec().allowed_paths) == _manifest()
    for bad in ({".env": "b"*64}, {"../private": "b"*64}, {"src/crypto_trader/paper/session.py": "invalid"}):
        with pytest.raises(CanonicalError):
            input_manifest(bad, allowed_paths=_spec().allowed_paths)


@pytest.mark.parametrize("fraction", (
    "١٢٣٤٥٦٧٨٩", "۱۲۳۴۵۶۷۸۹", "१२३४५६७८९", "１２３４５６７８９",
    *("123456789"[:position] + "١" + "123456789"[position + 1:] for position in range(9)),
))
def test_v8_utc9_rejects_non_ascii_fractional_digits(fraction):
    with pytest.raises(CanonicalError, match="require UTC nanosecond timestamp"):
        utc_nanoseconds("2026-01-01T00:00:00." + fraction + "Z")


@pytest.mark.parametrize("timestamp", (
    "2026-01-01T00:00:00.000000000Z", "2026-01-01T00:00:00.999999999Z",
    "2024-02-29T23:59:59.123456789Z",
))
def test_v8_utc9_preserves_exact_ascii_representation(timestamp):
    assert utc_nanoseconds(timestamp) == timestamp


@pytest.mark.parametrize("timestamp", (
    "2026-01-01T00:00:00.12345678Z", "2026-01-01T00:00:00.1234567890Z",
    "2026-01-01T00:00:00.123456789Z\n", "2026-02-29T00:00:00.123456789Z",
    "2026-01-01T00:00:60.123456789Z", "２０２６-01-01T00:00:00.123456789Z",
    "2026-٠١-01T00:00:00.123456789Z", "2026-01-01T٠٠:00:00.123456789Z",
))
def test_v8_utc9_still_rejects_calendar_width_and_non_ascii_fields(timestamp):
    with pytest.raises(CanonicalError):
        utc_nanoseconds(timestamp)


def test_study_allocations_budget_and_unrefunded_retries(tmp_path):
    reg = _create(tmp_path, capacity=2)
    a = reg.allocate(_spec().study_id, {"alpha": "1"}, _manifest())
    assert a.status == "STARTED" and a.research_label == "EXPLORATORY"
    assert reg.count_allocated(_spec().study_id) == 1
    reg.finish(a.trial_id, "FAILED", reason="synthetic error")
    b = reg.allocate(_spec().study_id, {"alpha": "2"}, _manifest(), parent_trial_id=a.trial_id)
    assert b.trial_id != a.trial_id
    assert reg.count_allocated(_spec().study_id) == 2
    with pytest.raises(RegistryError, match="capacity"):
        reg.allocate(_spec().study_id, {"alpha": "3"}, _manifest())
    assert reg.count_allocated(_spec().study_id) == 2
    assert reg.count_refusals(_spec().study_id) == 1
    assert reg.verify().status == "UNANCHORED"
    reg.close()


def test_bad_preallocation_binding_refused_without_spending_slot(tmp_path):
    reg = _create(tmp_path, capacity=1)
    with pytest.raises(RegistryError):
        reg.allocate(_spec().study_id, {"alpha": "x"}, {"secret.env": "b"*64})
    assert reg.count_refusals(_spec().study_id) == 1
    assert reg.count_allocated(_spec().study_id) == 0
    with pytest.raises(RegistryError, match="seed"):
        reg.allocate(_spec().study_id, {"alpha": "x"}, _manifest(), requested_seed="hijack")
    assert reg.count_allocated(_spec().study_id) == 0
    a=reg.allocate(_spec().study_id, {"alpha": "x"}, _manifest())
    assert a.trial_id
    reg.close()


def test_durable_recovery_and_counting_after_reopen(tmp_path):
    db=tmp_path/"audit.sqlite"
    reg=TrialRegistry(db)
    reg.register_study(_spec(capacity=1))
    a=reg.allocate(_spec().study_id, {"k": "v"}, _manifest())
    reg.close()
    reg=TrialRegistry(db)
    assert reg.get_status(a.trial_id) == "STARTED"
    assert reg.recover_interrupted(operator_confirms_no_active_writer=True) == 1
    assert reg.get_status(a.trial_id) == "INTERRUPTED_UNRESOLVED"
    assert reg.count_allocated(_spec().study_id)==1
    with pytest.raises(RegistryError, match="capacity"):
        reg.allocate(_spec().study_id, {"k": "new"}, _manifest())
    assert reg.verify().status=="UNANCHORED"
    reg.close()


def test_concurrent_last_slot_one_winner(tmp_path):
    reg=_create(tmp_path,capacity=1)
    def try_allocate(i):
        try:
            return reg.allocate(_spec().study_id, {"candidate": str(i)}, _manifest()).trial_id
        except RegistryError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(try_allocate,range(12)))
    assert len([x for x in results if x]) == 1
    assert reg.count_allocated(_spec().study_id)==1
    assert reg.count_refusals(_spec().study_id)==11
    reg.close()


def test_hash_chain_tamper_fails_before_more_allocation(tmp_path):
    reg=_create(tmp_path)
    reg.allocate(_spec().study_id, {"k":"v"}, _manifest())
    reg.close()
    db=tmp_path/"trials.sqlite"
    conn=sqlite3.connect(db)
    conn.execute("UPDATE events SET payload=? WHERE kind='STARTED'", ('{"fake":true}',))
    conn.commit()
    conn.close()
    with pytest.raises(RegistryError,match="integrity|chain"):
        TrialRegistry(db)


def test_amendments_do_not_reset_budget(tmp_path):
    reg=_create(tmp_path,capacity=1)
    reg.amend_study(_spec().study_id, "new objective", requested_n_max=9)
    a=reg.allocate(_spec().study_id, {"k":"v"}, _manifest())
    reg.finish(a.trial_id,"COMPLETED",output_hash="c"*64)
    reg.amend_study(_spec().study_id,"after outcome",requested_n_max=99)
    with pytest.raises(RegistryError,match="capacity"):
        reg.allocate(_spec().study_id,{"k":"v2"},_manifest())
    assert reg.count_allocated(_spec().study_id)==1
    assert reg.study_amendments(_spec().study_id)[-1]["post_hoc"] is True
    reg.close()


def test_trial_seed_unique_domain_separated_and_output_idempotency(tmp_path):
    reg=_create(tmp_path)
    a=reg.allocate(_spec().study_id,{"x":"y"},_manifest())
    b=reg.allocate(_spec().study_id,{"x":"y"},_manifest())
    assert a.seed_hex != b.seed_hex
    assert reg.derive_stream(a.trial_id,"fold") != reg.derive_stream(a.trial_id,"simulator")
    assert reg.derive_stream(a.trial_id,"fold")==reg.derive_stream(a.trial_id,"fold")
    reg.finish(a.trial_id,"COMPLETED",output_hash="d"*64)
    with pytest.raises(RegistryError):
        reg.finish(a.trial_id,"FAILED",reason="late")
    reg.close()


def test_transaction_rollback_if_append_crashes(tmp_path, monkeypatch):
    """A failed durable reservation never consumes capacity or exposes a receipt."""
    reg=_create(tmp_path,capacity=1)
    original=reg._append
    def fail_after_append(kind,study_id,data):
        original(kind,study_id,data)
        if kind == "STARTED":
            raise OSError("injected failure before SQLite COMMIT")
    monkeypatch.setattr(reg,"_append",fail_after_append)
    with pytest.raises(OSError,match="before SQLite"):
        reg.allocate(_spec().study_id,{"alpha":"rollback"},_manifest())
    assert reg.count_allocated(_spec().study_id)==0
    assert reg.verify().status=="UNANCHORED"
    monkeypatch.setattr(reg,"_append",original)
    assert reg.allocate(_spec().study_id,{"alpha":"retry"},_manifest()).status=="STARTED"
    reg.close()


def test_db_full_durability_and_atomic_multi_connection_last_slot(tmp_path):
    db=tmp_path/"shared.sqlite"
    first=TrialRegistry(db)
    first.register_study(_spec(capacity=1))
    second=TrialRegistry(db)
    assert first._db.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert first._db.execute("PRAGMA journal_mode").fetchone()[0].lower()=="wal"
    def allocated(reg,label):
        try:
            return reg.allocate(_spec().study_id, {"id":label},_manifest()).trial_id
        except RegistryError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        one=pool.submit(allocated, first, "a")
        two=pool.submit(allocated, second, "b")
        results=(one.result(),two.result())
    assert sum(x is not None for x in results)==1
    assert first.count_allocated(_spec().study_id)==1
    assert second.count_refusals(_spec().study_id)==1
    second.close()
    first.close()


def test_crash_after_commit_reconciles_durable_started(tmp_path):
    """Child process exits abruptly after durable STARTED; next process counts it."""
    import os
    import subprocess
    import sys

    db=tmp_path/"kill.sqlite"
    code = (
        "import os\n"
        "from crypto_trader.validation.contracts import StudySpec\n"
        "from crypto_trader.validation.registry import TrialRegistry\n"
        "db=r" + repr(str(db)) + "\n"
        "r=TrialRegistry(db)\n"
        "r.register_study(StudySpec(study_id='11111111-1111-4111-8111-111111111111',"
        "preregistration_hash='a'*64,n_max=1,allowed_paths=('src/crypto_trader/paper/session.py',)))\n"
        "r.allocate('11111111-1111-4111-8111-111111111111',{'alpha':'crash'},"
        "{'src/crypto_trader/paper/session.py':'b'*64})\n"
        "os._exit(71)\n"
    )
    env=os.environ.copy()
    completed=subprocess.run([sys.executable,"-B","-c",code],env=env,timeout=12,check=False)
    assert completed.returncode==71
    with TrialRegistry(db) as recovered:
        assert recovered.get_status(recovered._db.execute("SELECT trial_id FROM attempts").fetchone()[0]) == "STARTED"
        assert recovered.recover_interrupted(operator_confirms_no_active_writer=True) == 1
        assert recovered.count_allocated(_spec().study_id)==1
        assert recovered._db.execute("SELECT status FROM attempts").fetchone()[0]=="INTERRUPTED_UNRESOLVED"
        assert recovered.verify().status=="UNANCHORED"


def test_auxiliary_index_corruption_is_detected(tmp_path):
    reg=_create(tmp_path)
    a=reg.allocate(_spec().study_id,{"v":"1"},_manifest())
    reg.close()
    db=tmp_path/"trials.sqlite"
    conn=sqlite3.connect(db)
    conn.execute("UPDATE attempts SET seed_hex=? WHERE trial_id=?",("0"*64,a.trial_id))
    conn.commit()
    conn.close()
    with pytest.raises(RegistryError,match="integrity"):
        TrialRegistry(db)


def test_rejected_precheck_only_after_durable_start(tmp_path):
    reg=_create(tmp_path,capacity=1)
    a=reg.allocate(_spec().study_id,{"check":"invalid after reservation"},_manifest())
    reg.finish(a.trial_id,"REJECTED_PRECHECK",reason="synthetic-only post-start validation")
    assert reg.get_status(a.trial_id)=="REJECTED_PRECHECK"
    with pytest.raises(RegistryError,match="capacity"):
        reg.allocate(_spec().study_id,{"candidate":"other"},_manifest())
    assert reg.count_refusals(_spec().study_id)==1
    reg.close()


def test_second_connection_cannot_interrupt_first_owner(tmp_path):
    """Opening an audit handle must never terminate a trial owned by a live writer."""
    db = tmp_path / "concurrent.sqlite"
    with TrialRegistry(db) as first:
        first.register_study(_spec(capacity=2))
        a = first.allocate(_spec().study_id, {"a": "one"}, _manifest())
        with TrialRegistry(db) as second:
            assert second.get_status(a.trial_id) == "STARTED"
            assert first.get_status(a.trial_id) == "STARTED"
            first.finish(a.trial_id, "COMPLETED", output_hash="c"*64)
            assert second.get_status(a.trial_id) == "COMPLETED"


def test_recovery_requires_explicit_confirmed_operator_action(tmp_path):
    db = tmp_path / "recover.sqlite"
    with TrialRegistry(db) as first:
        first.register_study(_spec(capacity=1))
        a = first.allocate(_spec().study_id, {"a": "one"}, _manifest())
    with TrialRegistry(db) as recovered:
        assert recovered.get_status(a.trial_id) == "STARTED"
        with pytest.raises(RegistryError, match="confirm"):
            recovered.recover_interrupted(operator_confirms_no_active_writer=False)
        assert recovered.get_status(a.trial_id) == "STARTED"
        assert recovered.recover_interrupted(operator_confirms_no_active_writer=True) == 1
        assert recovered.get_status(a.trial_id) == "INTERRUPTED_UNRESOLVED"
        assert recovered.count_allocated(_spec().study_id) == 1
        assert recovered.recover_interrupted(operator_confirms_no_active_writer=True) == 0


def test_failed_attempt_amendment_is_post_hoc(tmp_path):
    with _create(tmp_path) as reg:
        a = reg.allocate(_spec().study_id, {"a": "one"}, _manifest())
        reg.finish(a.trial_id, "FAILED", reason="synthetic experimental failure")
        reg.amend_study(_spec().study_id, "do not retroactively bless it")
        assert reg.study_amendments(_spec().study_id)[-1]["post_hoc"] is True


def test_strict_cutoff_format_and_nondecimal_quantum():
    for invalid in ("unknown", "2025", "2026-02-30T00:00:00.000000000Z"):
        with pytest.raises(RegistryError):
            StudySpec(
                study_id=_spec().study_id, preregistration_hash="a"*64,
                n_max=1, allowed_paths=_spec().allowed_paths, cutoff=invalid,
            )
    StudySpec(
        study_id=_spec().study_id, preregistration_hash="a"*64,
        n_max=1, allowed_paths=_spec().allowed_paths,
        cutoff="2025-12-31T23:59:59.000000001Z",
    )
    with pytest.raises(CanonicalError):
        decimal_string("0.01", quantum="0.05")
    assert decimal_string("0.10", quantum="0.05") == "0.10"


def test_registry_allows_only_audited_bounded_rejection_for_nested_payload(tmp_path):
    with _create(tmp_path) as reg:
        payload = {"leaf": "safe"}
        for _ in range(1100):
            payload = {"nested": payload}
        with pytest.raises(RegistryError):
            reg.allocate(_spec().study_id, payload, _manifest())
        assert reg.count_refusals(_spec().study_id) == 1
        assert reg.count_allocated(_spec().study_id) == 0


def test_study_salt_deterministic_across_clean_registry_copies(tmp_path):
    with TrialRegistry(tmp_path / "one.sqlite") as a:
        a.register_study(_spec())
        salt1 = a._db.execute("SELECT study_salt FROM studies").fetchone()[0]
    with TrialRegistry(tmp_path / "two.sqlite") as b:
        b.register_study(_spec())
        salt2 = b._db.execute("SELECT study_salt FROM studies").fetchone()[0]
    assert salt1 == salt2 and len(salt1) >= 32


def test_verified_reader_uses_one_snapshot_during_external_write(tmp_path, monkeypatch):
    """Verification and returned count cannot see different SQLite revisions."""
    from threading import Event

    db = tmp_path / "snapshot.sqlite"
    with TrialRegistry(db) as writer, TrialRegistry(db) as reader:
        writer.register_study(_spec(capacity=2))
        verified = Event()
        committed = Event()
        original = reader._verify_locked

        def pause_after_verify():
            result = original()
            verified.set()
            assert committed.wait(timeout=8), "writer did not finish its WAL commit"
            return result

        monkeypatch.setattr(reader, "_verify_locked", pause_after_verify)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(reader.count_allocated, _spec().study_id)
            assert verified.wait(timeout=8)
            writer.allocate(_spec().study_id, {"k": "concurrent"}, _manifest())
            committed.set()
            assert result.result(timeout=8) == 0
        monkeypatch.setattr(reader, "_verify_locked", original)
        assert reader.count_allocated(_spec().study_id) == 1


def test_recovery_confirmation_is_exact_true_not_truthy(tmp_path):
    with _create(tmp_path, capacity=1) as registry:
        receipt = registry.allocate(_spec().study_id, {"k": "value"}, _manifest())
        for bad in (None, "yes", 1, [], False):
            with pytest.raises(RegistryError, match="confirm"):
                registry.recover_interrupted(operator_confirms_no_active_writer=bad)
        assert registry.get_status(receipt.trial_id) == "STARTED"
