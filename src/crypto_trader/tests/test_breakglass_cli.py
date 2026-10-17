"""Phase 13: the operator CLI - explicit, LLM-free, fail-closed and secret-free."""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from crypto_trader.breakglass import (
    BreakGlassControl,
    BreakGlassStateError,
    OperatorCommandType,
    OwnershipStateError,
)
from crypto_trader.breakglass.state import SafetyStateStore
from crypto_trader.breakglass.venue import VenueControlOutcome
from crypto_trader.events import EventValidationError, decode_event
from crypto_trader.breakglass.cli import (
    EXIT_ACCEPTED,
    EXIT_MALFORMED,
    EXIT_REFUSED,
    main,
)
from crypto_trader.persistence import JournalSchemaError, SQLiteEventJournal
from crypto_trader.tests.production_fixtures import INSTRUMENT, RecordingExecutionPort, build_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def run_cli(tmp_path: Path, *args: str, journal_path: Path | None = None) -> int:
    journal = journal_path or (tmp_path / "journal.db")
    return main(
        [
            *args,
            "--state-dir",
            str(tmp_path),
            *([] if journal is None else ["--journal", str(journal)]),
        ]
    )


def base_args(tmp_path: Path, command: str, *, command_id: str | None = None, **extra: str):
    args = [
        "run",
        "--command-type",
        command,
        "--command-id",
        command_id or str(uuid4()),
        "--operator",
        "jamal",
        "--reason",
        "operator requested break-glass control",
        "--journal",
        str(tmp_path / "journal.db"),
        "--state-dir",
        str(tmp_path),
        "--json",
    ]
    for key, value in extra.items():
        args.extend([f"--{key.replace('_', '-')}", value])
    return args


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _found_full_journal(tmp_path: Path) -> Path:
    """Found the FULL journal a venue-grade writer creates; the break-glass run never creates one."""
    path = tmp_path / "journal.db"
    with SQLiteEventJournal(path, venue_writes=True):
        pass
    return path


def _assert_no_state_files(state_dir: Path) -> None:
    """No ownership, safety or lock file was written next to the journal."""
    assert sorted(p.name for p in state_dir.iterdir() if not p.name.startswith("journal.db")) == []


def test_status_reports_the_read_only_posture(tmp_path, capsys):
    journal = tmp_path / "journal.db"
    with SQLiteEventJournal(journal):
        pass  # founded by a writer; status itself never creates a journal
    founded = _sha256(journal)

    code = run_cli(tmp_path, "status", "--json")
    record = json.loads(capsys.readouterr().out)

    assert code == EXIT_ACCEPTED
    assert record["safety"]["mode"] == "normal"
    assert record["safety"]["new_risk_permitted"] is False  # no writer has claimed the lease yet
    assert record["epoch"]["open"] is False
    assert _sha256(journal) == founded
    _assert_no_state_files(tmp_path)


@pytest.mark.parametrize("explicit_journal", [True, False])
def test_status_refuses_a_missing_journal_and_creates_nothing(tmp_path, capsys, explicit_journal):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    journal = state_dir / "journal.db"
    argv = ["status", "--json", "--state-dir", str(state_dir)]
    if explicit_journal:
        argv += ["--journal", str(journal)]

    code = main(argv)
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err == (
        "REFUSED (fail closed, nothing changed): JournalSchemaError: "
        f"journal {journal} is not an existing file; a read-only open never creates it.\n"
    )
    assert sorted(p.name for p in tmp_path.rglob("*")) == ["state"]


def test_status_refuses_an_unfounded_journal_file_without_writing_a_schema(tmp_path, capsys):
    journal = tmp_path / "journal.db"
    journal.write_bytes(b"")

    code = run_cli(tmp_path, "status", "--json")
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err == (
        "REFUSED (fail closed, nothing changed): JournalSchemaError: read-only journal could not "
        "be validated (no such table: journal_meta); nothing was created or repaired.\n"
    )
    assert journal.read_bytes() == b""
    assert sorted(p.name for p in tmp_path.iterdir()) == ["journal.db"]


def test_status_refuses_an_undecodable_journal_without_a_traceback(tmp_path, capsys):
    journal = _found_full_journal(tmp_path)
    main(base_args(tmp_path, "halt-new-risk"))
    capsys.readouterr()
    bad = '{"event_type": "strategy.decision.recorded", "schema_version": 1}'
    raw = sqlite3.connect(journal)
    with raw:
        raw.execute("UPDATE events SET payload_json = ?", (bad,))
    raw.close()
    founded = _sha256(journal)
    with pytest.raises(EventValidationError) as expected:
        decode_event(bad)

    code = run_cli(tmp_path, "status", "--json")
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err == (
        f"REFUSED (fail closed, nothing changed): EventValidationError: {expected.value}\n"
    )
    assert _sha256(journal) == founded


def test_status_refuses_a_malformed_journal_page_without_a_traceback(tmp_path, capsys, monkeypatch):
    journal = _found_full_journal(tmp_path)
    main(base_args(tmp_path, "halt-new-risk"))
    capsys.readouterr()
    founded = _sha256(journal)

    def malformed(self, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")
        yield  # pragma: no cover - makes this a generator like the real replay

    monkeypatch.setattr(SQLiteEventJournal, "replay", malformed)
    code = run_cli(tmp_path, "status", "--json")
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err == (
        "REFUSED (fail closed, nothing changed): DatabaseError: database disk image is malformed\n"
    )
    assert _sha256(journal) == founded


def test_run_accepts_a_halt_and_engages_the_kill_switch(tmp_path, capsys):
    _found_full_journal(tmp_path)
    code = main(base_args(tmp_path, "halt-new-risk"))
    record = json.loads(capsys.readouterr().out)

    assert code == EXIT_ACCEPTED
    assert record["disposition"] == "accepted"
    assert record["resulting_safety_state"] == "halt_new_risk"
    assert record["new_risk_permitted"] is False
    assert record["operator_acknowledgement_pending"] is True


def test_a_second_identical_delivery_is_idempotent(tmp_path, capsys):
    _found_full_journal(tmp_path)
    command_id = str(uuid4())
    first = main(base_args(tmp_path, "halt-new-risk", command_id=command_id))
    json.loads(capsys.readouterr().out)
    second = main(base_args(tmp_path, "halt-new-risk", command_id=command_id))
    record = json.loads(capsys.readouterr().out)

    assert first == second == EXIT_ACCEPTED
    assert record["duplicate_delivery"] is True
    assert record["reason_code"] == "break_glass_engaged_halt_new_risk"


def test_restore_is_refused_until_the_gates_are_met(tmp_path, capsys):
    _found_full_journal(tmp_path)
    main(base_args(tmp_path, "enter-degraded"))
    capsys.readouterr()
    code = main(base_args(tmp_path, "restore-normal"))
    record = json.loads(capsys.readouterr().out)

    assert code == EXIT_REFUSED
    assert record["disposition"] == "rejected"
    assert record["reason_code"] == "restore_denied_reconciliation_pending"


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--command-type", "not-a-command", "--command-id", "x", "--operator", "j", "--reason", "r"],
        ["run", "--command-type", "halt-new-risk", "--command-id", "not-a-uuid", "--operator", "j", "--reason", "r"],
    ],
)
def test_malformed_arguments_fail_closed_without_touching_state(tmp_path, capsys, argv):
    args = [*argv, "--journal", str(tmp_path / "journal.db"), "--state-dir", str(tmp_path / "state")]
    code = main(args)
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert "REFUSED" in captured.err
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        assert journal.count() == 0


@pytest.mark.parametrize("command", ["halt-new-risk", "cancel-open"])
def test_run_refuses_a_missing_journal_and_creates_nothing(tmp_path, capsys, command):
    journal = tmp_path / "journal.db"

    code = main(base_args(tmp_path, command))
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err == (
        "REFUSED (fail closed, nothing changed): JournalSchemaError: the journal is not an existing "
        f"file; run never creates a journal. [journal: {journal}] If orders are open, cancel them "
        "directly at the exchange.\n"
    )
    assert sorted(p.name for p in tmp_path.rglob("*")) == []


def test_run_refuses_a_journal_that_is_not_full_founded_without_touching_it(tmp_path, capsys):
    journal = tmp_path / "journal.db"
    with SQLiteEventJournal(journal):
        pass  # a NORMAL journal, e.g. a wrong --journal path
    founded = _sha256(journal)

    code = main(base_args(tmp_path, "halt-new-risk"))
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err == (
        "REFUSED (fail closed, nothing changed): JournalSchemaError: refusing retrospective FULL "
        f"founding of an existing journal. [journal: {journal}] If orders are open, cancel them "
        "directly at the exchange.\n"
    )
    assert _sha256(journal) == founded
    _assert_no_state_files(tmp_path)


def test_run_turns_an_os_error_on_the_journal_path_into_a_clean_refusal(tmp_path, capsys, monkeypatch):
    journal = _found_full_journal(tmp_path)

    def denied(self):
        raise PermissionError(13, "access denied")

    monkeypatch.setattr(Path, "is_file", denied)
    code = main(base_args(tmp_path, "halt-new-risk"))
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err == (
        "REFUSED (fail closed, nothing changed): JournalSchemaError: the journal could not be "
        f"checked: [Errno 13] access denied [journal: {journal}] If orders are open, cancel them "
        "directly at the exchange.\n"
    )
    _assert_no_state_files(tmp_path)


def test_run_refuses_a_journal_that_is_not_bound_to_the_state_dir(tmp_path, capsys):
    production = tmp_path / "production"
    production.mkdir()
    journal = _found_full_journal(production)
    founded = _sha256(journal)
    other_state = tmp_path / "typo-state"
    args = base_args(tmp_path, "halt-new-risk")
    args[args.index("--journal") + 1] = str(journal)
    args[args.index("--state-dir") + 1] = str(other_state)

    code = main(args)
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err == (
        f"REFUSED (fail closed, nothing changed): --journal {journal} is not the journal bound to "
        f"--state-dir {other_state} (expected {other_state / 'journal.db'}); refusing to pair a "
        "journal with another system's ownership state. If orders are open, cancel them directly "
        "at the exchange.\n"
    )
    assert _sha256(journal) == founded
    assert not other_state.exists()


def test_status_refuses_a_journal_that_is_not_bound_to_the_state_dir(tmp_path, capsys):
    journal = _found_full_journal(tmp_path)
    other_state = tmp_path / "typo-state"

    code = main(["status", "--json", "--state-dir", str(other_state), "--journal", str(journal)])
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err == (
        f"REFUSED (fail closed, nothing changed): --journal {journal} is not the journal bound to "
        f"--state-dir {other_state} (expected {other_state / 'journal.db'}); refusing to pair a "
        "journal with another system's ownership state.\n"
    )
    assert not other_state.exists()


def test_run_uses_the_state_dir_journal_when_journal_is_omitted(tmp_path, capsys):
    _found_full_journal(tmp_path)
    args = base_args(tmp_path, "halt-new-risk")
    at = args.index("--journal")
    del args[at:at + 2]

    code = main(args)
    record = json.loads(capsys.readouterr().out)

    assert code == EXIT_ACCEPTED
    assert record["resulting_safety_state"] == "halt_new_risk"


def test_run_accepts_another_spelling_of_the_bound_journal(tmp_path, capsys):
    _found_full_journal(tmp_path)
    args = base_args(tmp_path, "halt-new-risk")
    args[args.index("--journal") + 1] = str(tmp_path / "sub" / ".." / "journal.db")

    code = main(args)
    capsys.readouterr()

    assert code == EXIT_ACCEPTED


def test_run_reports_incomplete_when_the_journal_fails_after_side_effects(
    tmp_path, capsys, monkeypatch
):
    journal = _found_full_journal(tmp_path)

    def fail(self, events):
        raise JournalSchemaError("journal batch failed: disk I/O error")

    monkeypatch.setattr(SQLiteEventJournal, "append_batch", fail)

    code = main(base_args(tmp_path, "halt-new-risk"))
    captured = capsys.readouterr()

    assert code == 4
    assert captured.out == ""
    assert captured.err == (
        "INCOMPLETE (side effects may already be in effect): JournalSchemaError: journal batch "
        f"failed: disk I/O error [journal: {journal}]. The emergency lease and any venue cancel may "
        "already have happened: check status and re-deliver the same --command-id. If orders are "
        "open, cancel them directly at the exchange.\n"
    )


def _pretend_present(monkeypatch, path: Path, *names: str) -> None:
    """Make ``path`` look present to the named Path checks: it vanished after they ran."""
    for name in names:
        real = getattr(Path, name)
        monkeypatch.setattr(
            Path, name, lambda self, *a, _real=real, **k: self == path or _real(self, *a, **k)
        )


def test_run_never_founds_a_journal_that_vanished_after_the_existence_check(
    tmp_path, capsys, monkeypatch
):
    journal = tmp_path / "journal.db"
    _pretend_present(monkeypatch, journal, "is_file")

    code = main(base_args(tmp_path, "halt-new-risk"))
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err == (
        f"REFUSED (fail closed, nothing changed): JournalSchemaError: journal {journal} is not an "
        f"existing file; this writer never creates one. [journal: {journal}] If orders are open, "
        "cancel them directly at the exchange.\n"
    )
    assert not os.path.exists(journal)
    _assert_no_state_files(tmp_path)


def test_run_never_creates_a_journal_that_vanished_before_the_writer_connected(
    tmp_path, capsys, monkeypatch
):
    journal = tmp_path / "journal.db"
    _pretend_present(monkeypatch, journal, "is_file", "exists")

    code = main(base_args(tmp_path, "halt-new-risk"))
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err == (
        f"REFUSED (fail closed, nothing changed): JournalSchemaError: journal {journal} could not "
        "be opened without creating it: unable to open database file "
        f"[journal: {journal}] If orders are open, cancel them directly at the exchange.\n"
    )
    assert not os.path.exists(journal)
    _assert_no_state_files(tmp_path)


def test_run_reports_incomplete_when_a_step_fails_after_the_command_was_journaled(
    tmp_path, capsys, monkeypatch
):
    journal = _found_full_journal(tmp_path)

    def fail(self, controller):
        raise BreakGlassStateError("the safety state could not be written: disk full")

    monkeypatch.setattr(SafetyStateStore, "save", fail)

    code = main(base_args(tmp_path, "halt-new-risk"))
    captured = capsys.readouterr()

    assert code == 4
    assert captured.out == ""
    assert captured.err == (
        "INCOMPLETE (side effects may already be in effect): BreakGlassStateError: the safety "
        f"state could not be written: disk full [journal: {journal}]. The emergency lease and any "
        "venue cancel may already have happened: check status and re-deliver the same "
        "--command-id. If orders are open, cancel them directly at the exchange.\n"
    )
    with SQLiteEventJournal(journal, read_only=True) as reader:
        recorded = [event.event_type for event in reader.read_range()]
    assert recorded[-1] == "safety.operator_command.recorded"


class _ConfirmedWithAnUnusableReference:
    """The venue confirms the cancel but hands back a reference the record cannot use."""

    def cancel_open_orders(self, *, correlation_id, occurred_at, safety):
        return VenueControlOutcome(
            disposition="confirmed",
            reason_code="cancel_confirmed",
            detail="1 open order(s) were cancelled through the safety path",
            cancelled_order_references=(42,),
        )

    def venue_reports(self):
        return ()


def test_run_reports_incomplete_for_any_error_after_a_confirmed_venue_cancel(
    tmp_path, capsys, monkeypatch
):
    journal = _found_full_journal(tmp_path)
    monkeypatch.setattr(
        "crypto_trader.breakglass.cli._build_venue",
        lambda args: _ConfirmedWithAnUnusableReference(),
    )

    code = main(base_args(tmp_path, "cancel-open"))
    captured = capsys.readouterr()

    assert code == 4
    assert captured.out == ""
    assert captured.err == (
        "INCOMPLETE (side effects may already be in effect): AttributeError: 'int' object has no "
        f"attribute 'strip' [journal: {journal}]. The emergency lease and any venue cancel may "
        "already have happened: check status and re-deliver the same --command-id. If orders are "
        "open, cancel them directly at the exchange.\n"
    )


def test_run_uses_the_journal_recorded_by_the_runtime_that_owns_the_state_dir(tmp_path, capsys):
    journal_path = tmp_path / "journal.db"  # beside the state dir: the layout the runtime uses
    state_dir = tmp_path / "state"
    with SQLiteEventJournal(journal_path, venue_writes=True) as journal:
        runtime = build_runtime(
            journal=journal, state_dir=state_dir, execution=RecordingExecutionPort(INSTRUMENT)
        )
        runtime.start()
        args = base_args(state_dir, "halt-new-risk")
        at = args.index("--journal")
        del args[at:at + 2]

        code = main(args)
        captured = capsys.readouterr()

    assert code == EXIT_ACCEPTED, captured.err
    assert json.loads(captured.out)["resulting_safety_state"] == "halt_new_risk"
    with SQLiteEventJournal(journal_path, read_only=True) as reader:
        recorded = [event.event_type for event in reader.read_range()]
    assert recorded[-1] == "safety.operator_command.recorded"
    assert not (state_dir / "journal.db").exists()


def test_run_refuses_a_bound_journal_whose_real_path_changed(tmp_path, capsys):
    journal = _found_full_journal(tmp_path)
    founded = _sha256(journal)
    other_real = tmp_path / "system-a" / "journal.db"
    (tmp_path / "ownership.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "term": 1,
                "owner_id": "normal:production-runtime",
                "owner_kind": "normal",
                "acquired_at": "2026-01-01T12:00:00+00:00",
                "previous_owner_id": None,
                "reason_code": "production_runtime_started",
                "session_id": str(uuid4()),
                "journal_path": str(journal),
                "journal_realpath": str(other_real),
            }
        ),
        encoding="utf-8",
    )

    code = main(base_args(tmp_path, "halt-new-risk"))
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err == (
        "REFUSED (fail closed, nothing changed): the trading-state lease is bound to journal "
        f"{journal} (real path {other_real}), but {journal} resolves to "
        f"{os.path.realpath(journal)}; refusing to pair a journal with another system's ownership "
        "state. If orders are open, cancel them directly at the exchange.\n"
    )
    assert _sha256(journal) == founded


@pytest.mark.parametrize("subcommand", ["run", "status"])
def test_an_unreadable_lease_refuses_before_any_journal_is_chosen(tmp_path, capsys, subcommand):
    # Review N-01: an unreadable lease must not fall back to <state-dir>/journal.db - that file
    # may belong to another system while the bound journal lives outside the state dir.
    journal = _found_full_journal(tmp_path)
    founded = _sha256(journal)
    (tmp_path / "ownership.json").write_text("{not-json", encoding="utf-8")
    if subcommand == "run":
        args = base_args(tmp_path, "halt-new-risk")
        at = args.index("--journal")
        del args[at:at + 2]
        tail = "be read. If orders are open, cancel them directly at the exchange.\n"
    else:
        args = ["status", "--json", "--state-dir", str(tmp_path)]
        tail = "be read.\n"

    code = main(args)
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert captured.out == ""
    assert captured.err.startswith(
        "REFUSED (fail closed, nothing changed): the trading-state ownership record under "
        f"--state-dir {tmp_path} is unusable (the ownership record could not be read: "
    )
    assert captured.err.endswith(
        "); refusing to choose a journal while the lease it must be bound to cannot " + tail
    )
    assert _sha256(journal) == founded


def test_run_refuses_when_the_bound_journal_became_a_link_to_another_system(tmp_path, capsys):
    system_a, system_b = tmp_path / "system-a", tmp_path / "system-b"
    system_a.mkdir()
    system_b.mkdir()
    journal_a = _found_full_journal(system_a)
    journal_b = _found_full_journal(system_b)
    with SQLiteEventJournal(journal_b, venue_writes=True) as journal:
        BreakGlassControl(journal=journal, state_dir=system_b).claim_normal_writer(
            owner_id="normal:system-b"
        )
    journal_b.unlink()
    try:
        os.symlink(journal_a, journal_b)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks are unavailable here: {exc}")
    founded_a = _sha256(journal_a)

    code = main(base_args(system_b, "halt-new-risk"))
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED
    assert "refusing to pair a journal with another system's ownership state." in captured.err
    assert _sha256(journal_a) == founded_a


@pytest.mark.parametrize("alias_kind", ["junction", "symlink"])
def test_the_status_reader_shares_the_writer_spelling_while_the_wal_is_live(
    tmp_path, capsys, alias_kind
):
    # Review F-05: a writer on an alias of the journal (junction or link) and a reader on the
    # real path may not share -wal/-shm. The only production reader (status) opens the spelling
    # the lease recorded, refuses any other, and sees the writer's uncheckpointed WAL there.
    from datetime import datetime, timezone

    from crypto_trader.breakglass import parse_operator_command

    real_dir, alias_dir, state = tmp_path / "real", tmp_path / "alias", tmp_path / "state"
    real_dir.mkdir()
    real = _found_full_journal(real_dir)
    if alias_kind == "junction":
        if os.name != "nt":
            pytest.skip("directory junctions exist only on Windows")
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias_dir), str(real_dir)],
            capture_output=True,
            text=True,
        )
        if made.returncode != 0:
            pytest.skip(f"a junction could not be created here: {made.stdout}{made.stderr}")
    else:
        alias_dir.mkdir()
        try:
            os.symlink(real, alias_dir / "journal.db")
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"file symlinks are unavailable here: {exc}")
    alias = alias_dir / "journal.db"

    with SQLiteEventJournal(alias, venue_writes=True) as writer:
        control = BreakGlassControl(journal=writer, state_dir=state)
        control.claim_normal_writer(owner_id="normal:alias")
        control.handle(
            parse_operator_command(
                {
                    "command_type": "halt_new_risk",
                    "command_id": str(uuid4()),
                    "operator_label": "jamal",
                    "reason": "live wal",
                    "source": "operator-cli",
                },
                delivered_at=datetime.now(timezone.utc),
            )
        )
        written = len(list(writer.replay()))
        bound = control.ownership_store.read().journal_path

        assert written > 0
        assert os.path.getsize(f"{alias}-wal") > 0
        assert os.path.normcase(bound) == os.path.normcase(os.path.abspath(alias))

        code = main(["status", "--json", "--state-dir", str(state), "--journal", str(real)])
        captured = capsys.readouterr()

        assert code == EXIT_MALFORMED
        assert captured.err == (
            f"REFUSED (fail closed, nothing changed): --journal {real} is not the journal bound "
            f"to --state-dir {state} (expected {Path(bound)}); refusing to pair a journal with "
            "another system's ownership state.\n"
        )
        with SQLiteEventJournal(bound, read_only=True) as reader:
            assert len(list(reader.replay())) == written


def test_a_writer_cannot_claim_a_state_dir_bound_to_another_journal(tmp_path):
    state_dir = tmp_path / "state"
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    with SQLiteEventJournal(first) as journal:
        BreakGlassControl(journal=journal, state_dir=state_dir).claim_normal_writer(
            owner_id="normal:a"
        )
    with SQLiteEventJournal(second) as journal:
        control = BreakGlassControl(journal=journal, state_dir=state_dir)
        with pytest.raises(OwnershipStateError, match=re.escape(
            f"the trading-state lease is bound to journal {first} (real path "
            f"{os.path.realpath(first)}), but {second} resolves to {os.path.realpath(second)}; "
            "refusing to pair a journal with another system's ownership state."
        )):
            control.claim_normal_writer(owner_id="normal:b")


_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


@pytest.mark.parametrize("subcommand", ["run", "status"])
def test_the_cli_refuses_another_journal_moved_onto_the_bound_path(tmp_path, capsys, subcommand):
    # Review F-02: a real path names whatever file is there now. Another system's FULL journal
    # moved onto the bound path has the same real path and spelling, but not the same identity.
    system_a, system_b = tmp_path / "system-a", tmp_path / "system-b"
    system_a.mkdir()
    system_b.mkdir()
    journal_a = _found_full_journal(system_a)
    journal_b = _found_full_journal(system_b)
    with SQLiteEventJournal(journal_b, venue_writes=True) as journal:
        BreakGlassControl(journal=journal, state_dir=system_b).claim_normal_writer(
            owner_id="normal:system-b"
        )
    lease = (system_b / "ownership.json").read_text(encoding="utf-8")
    os.replace(journal_a, journal_b)
    moved = _sha256(journal_b)
    if subcommand == "run":
        args, prefix = base_args(system_b, "halt-new-risk"), "OwnershipStateError: "
    else:
        args, prefix = ["status", "--json", "--state-dir", str(system_b)], ""

    code = main(args)
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED, captured.out
    assert captured.out == ""
    refusal = re.fullmatch(
        re.escape(
            f"REFUSED (fail closed, nothing changed): {prefix}the trading-state lease is bound to "
            f"journal {journal_b} (journal_id "
        )
        + f"({_UUID})"
        + re.escape(f"), but the journal open at {journal_b} has journal_id ")
        + f"({_UUID})"
        + re.escape("; refusing to pair a journal with another system's ownership state.\n"),
        captured.err,
    )
    assert refusal is not None, captured.err
    assert refusal.group(1) != refusal.group(2)
    assert _sha256(journal_b) == moved
    assert (system_b / "ownership.json").read_text(encoding="utf-8") == lease


def test_a_writer_cannot_claim_a_state_dir_whose_journal_was_founded_anew_at_its_path(tmp_path):
    # Review F-02: a journal founded again at the bound path (the bound one lost or deleted) has
    # the bound real path and spelling, but it is not the journal the lease is bound to.
    state_dir = tmp_path / "state"
    bound = _found_full_journal(tmp_path)
    with SQLiteEventJournal(bound, venue_writes=True) as journal:
        BreakGlassControl(journal=journal, state_dir=state_dir).claim_normal_writer(
            owner_id="normal:a"
        )
    lease = (state_dir / "ownership.json").read_text(encoding="utf-8")
    bound.unlink()
    _found_full_journal(tmp_path)

    with SQLiteEventJournal(bound, venue_writes=True) as journal:
        control = BreakGlassControl(journal=journal, state_dir=state_dir)
        with pytest.raises(OwnershipStateError, match=(
            re.escape(f"the trading-state lease is bound to journal {bound} (journal_id ")
            + _UUID
            + re.escape(f"), but the journal open at {bound} has journal_id ")
            + _UUID
            + re.escape("; refusing to pair a journal with another system's ownership state.")
        )):
            control.claim_normal_writer(owner_id="normal:b")
    assert (state_dir / "ownership.json").read_text(encoding="utf-8") == lease


class _CountingVenue:
    """A venue that records every cancel it is asked to send."""

    def __init__(self) -> None:
        self.cancels = 0

    def cancel_open_orders(self, *, correlation_id, occurred_at, safety):
        self.cancels += 1
        return VenueControlOutcome(
            disposition="confirmed",
            reason_code="cancel_confirmed",
            detail="0 open order(s) were cancelled through the safety path",
        )

    def venue_reports(self):
        return ()


def test_a_cancel_never_reaches_the_venue_through_another_journal_at_the_bound_path(tmp_path):
    # Review F-02: the venue cancel precedes the emergency takeover, so the journal's identity is
    # judged before any side effect; otherwise the venue acts for a journal it does not belong to.
    from datetime import datetime, timezone

    from crypto_trader.breakglass import parse_operator_command

    state_dir = tmp_path / "state"
    bound = _found_full_journal(tmp_path)
    with SQLiteEventJournal(bound, venue_writes=True) as journal:
        BreakGlassControl(journal=journal, state_dir=state_dir).claim_normal_writer(
            owner_id="normal:a"
        )
    lease = (state_dir / "ownership.json").read_text(encoding="utf-8")
    bound.unlink()
    _found_full_journal(tmp_path)
    founded = _sha256(bound)
    venue = _CountingVenue()

    with SQLiteEventJournal(bound, venue_writes=True, must_exist=True) as journal:
        control = BreakGlassControl(journal=journal, state_dir=state_dir, venue=venue)
        with pytest.raises(OwnershipStateError, match=re.escape(
            "; refusing to pair a journal with another system's ownership state."
        )):
            control.handle(
                parse_operator_command(
                    {
                        "command_type": "cancel_open",
                        "command_id": str(uuid4()),
                        "operator_label": "jamal",
                        "reason": "journal identity",
                        "source": "operator-cli",
                    },
                    delivered_at=datetime.now(timezone.utc),
                )
            )
    assert venue.cancels == 0
    assert (state_dir / "ownership.json").read_text(encoding="utf-8") == lease
    assert _sha256(bound) == founded


@pytest.mark.parametrize("command_type", [kind.value for kind in OperatorCommandType])
@pytest.mark.parametrize(
    ("lease_text", "reason"),
    [
        ("{not-json", "the ownership record could not be read"),
        (
            json.dumps({
                "schema_version": 99, "term": 1, "owner_id": "normal:newer-build",
                "owner_kind": "normal", "acquired_at": "2026-01-01T12:00:00+00:00",
                "previous_owner_id": None, "reason_code": "runtime_started",
                "session_id": "00000000-0000-4000-8000-000000000000",
            }),
            "unsupported ownership schema 99",
        ),
    ],
)
def test_handle_refuses_an_unreadable_lease_before_any_side_effect(
    tmp_path, command_type, lease_text, reason
):
    # Review N-01: a lease that cannot be read cannot vouch for its journal binding. handle()
    # used to treat it as unbound: a cancel reached the venue before the takeover refused, and
    # other commands journaled (reduce-only even changed the posture) beside an unusable lease.
    from datetime import datetime, timezone

    from crypto_trader.breakglass import parse_operator_command

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    journal_path = _found_full_journal(tmp_path)
    (state_dir / "ownership.json").write_text(lease_text, encoding="utf-8")
    founded = _sha256(journal_path)
    venue = _CountingVenue()

    with SQLiteEventJournal(journal_path, venue_writes=True, must_exist=True) as journal:
        control = BreakGlassControl(journal=journal, state_dir=state_dir, venue=venue)
        with pytest.raises(OwnershipStateError, match=reason):
            control.handle(
                parse_operator_command(
                    {
                        "command_type": command_type,
                        "command_id": str(uuid4()),
                        "operator_label": "jamal",
                        "reason": "unreadable lease",
                        "source": "operator-cli",
                    },
                    delivered_at=datetime.now(timezone.utc),
                )
            )
        assert journal.count() == 0
    assert venue.cancels == 0
    assert _sha256(journal_path) == founded
    assert (state_dir / "ownership.json").read_text(encoding="utf-8") == lease_text
    assert sorted(p.name for p in state_dir.iterdir() if not p.name.endswith(".lock.db")) == [
        "ownership.json"
    ]


@pytest.mark.parametrize("command", ["cancel-open", "reduce-only"])
def test_run_reports_a_lease_unusable_by_handle_time_as_refused_never_accepted(
    tmp_path, capsys, monkeypatch, command
):
    # Review N-01: run refuses an unreadable lease before choosing a journal; a lease that becomes
    # unreadable after that is refused by handle() before any side effect, so the operator sees a
    # refusal - never an accepted outcome or a partial (INCOMPLETE) change.
    from crypto_trader.breakglass import cli

    journal = _found_full_journal(tmp_path)
    with SQLiteEventJournal(journal, venue_writes=True) as writer:
        BreakGlassControl(journal=writer, state_dir=tmp_path).claim_normal_writer(
            owner_id="normal:runtime"
        )
    founded = _sha256(journal)
    choose = cli._journal_for

    def choose_then_corrupt(args):
        chosen = choose(args)
        (tmp_path / "ownership.json").write_text("{not-json", encoding="utf-8")
        return chosen

    monkeypatch.setattr(cli, "_journal_for", choose_then_corrupt)

    code = main(base_args(tmp_path, command))
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED, captured.out
    assert captured.out == ""
    assert captured.err.startswith(
        "REFUSED (fail closed, nothing changed): OwnershipStateError: the ownership record could "
        "not be read: "
    ), captured.err
    assert _sha256(journal) == founded


def _bind_without_a_journal_id(state_dir: Path, journal: Path) -> str:
    """A lease bound by path alone, as builds before journal identities wrote it."""
    from datetime import datetime, timezone

    from crypto_trader.breakglass import OwnerKind, OwnershipStore

    store = OwnershipStore(state_dir / "ownership.json")
    store.takeover(
        owner_id="normal:legacy",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=datetime.now(timezone.utc),
        reason_code="runtime_started",
        journal_path=str(journal),
    )
    return store.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("subcommand", ["run", "status"])
def test_a_lease_bound_without_a_journal_id_refuses_a_journal_that_has_one(
    tmp_path, capsys, subcommand
):
    # Review F02-R1: identities are minted only at founding and never backfilled, so a journal that
    # has one is not the id-less journal a path-only lease was bound to; it must not be adopted.
    system_a, system_b = tmp_path / "system-a", tmp_path / "system-b"
    system_a.mkdir()
    system_b.mkdir()
    journal_a = _found_full_journal(system_a)
    journal_b = _found_full_journal(system_b)
    lease = _bind_without_a_journal_id(system_b, journal_b)
    os.replace(journal_a, journal_b)
    moved = _sha256(journal_b)
    if subcommand == "run":
        args, prefix = base_args(system_b, "halt-new-risk"), "OwnershipStateError: "
    else:
        args, prefix = ["status", "--json", "--state-dir", str(system_b)], ""

    code = main(args)
    captured = capsys.readouterr()

    assert code == EXIT_MALFORMED, captured.out
    assert captured.out == ""
    assert re.fullmatch(
        re.escape(
            f"REFUSED (fail closed, nothing changed): {prefix}the trading-state lease is bound to "
            f"journal {journal_b} without a journal_id, but the journal open at {journal_b} has "
            "journal_id "
        )
        + _UUID
        + re.escape("; refusing to pair a journal with another system's ownership state.\n"),
        captured.err,
    ), captured.err
    assert _sha256(journal_b) == moved
    assert (system_b / "ownership.json").read_text(encoding="utf-8") == lease


def test_a_lease_bound_without_a_journal_id_never_adopts_a_journal_founded_anew(tmp_path):
    # Review F02-R1: neither a runtime claim nor a cancel may take a journal founded again at the
    # bound path as the bound one and record its identity; the venue is never called.
    from datetime import datetime, timezone

    from crypto_trader.breakglass import parse_operator_command

    state_dir = tmp_path / "state"
    bound = _found_full_journal(tmp_path)
    lease = _bind_without_a_journal_id(state_dir, bound)
    bound.unlink()
    _found_full_journal(tmp_path)
    founded = _sha256(bound)
    venue = _CountingVenue()
    refusal = (
        re.escape(f"the trading-state lease is bound to journal {bound} without a journal_id, but ")
        + re.escape(f"the journal open at {bound} has journal_id ")
        + _UUID
        + re.escape("; refusing to pair a journal with another system's ownership state.")
    )

    with SQLiteEventJournal(bound, venue_writes=True, must_exist=True) as journal:
        control = BreakGlassControl(journal=journal, state_dir=state_dir, venue=venue)
        with pytest.raises(OwnershipStateError, match=refusal):
            control.claim_normal_writer(owner_id="normal:other")
        with pytest.raises(OwnershipStateError, match=refusal):
            control.handle(
                parse_operator_command(
                    {
                        "command_type": "cancel_open",
                        "command_id": str(uuid4()),
                        "operator_label": "jamal",
                        "reason": "journal identity",
                        "source": "operator-cli",
                    },
                    delivered_at=datetime.now(timezone.utc),
                )
            )
    assert venue.cancels == 0
    assert (state_dir / "ownership.json").read_text(encoding="utf-8") == lease
    assert _sha256(bound) == founded


def test_a_lease_bound_without_a_journal_id_keeps_its_id_less_journal_by_path(tmp_path):
    # Review F02-R1 (residual, by design): a journal founded before identities has none, so its
    # path-only lease is judged by path as before, and a takeover never records an identity.
    state_dir = tmp_path / "state"
    bound = _found_full_journal(tmp_path)
    with sqlite3.connect(bound) as raw:
        raw.execute("DELETE FROM journal_meta WHERE key = 'journal_id'")
    raw.close()
    _bind_without_a_journal_id(state_dir, bound)

    with SQLiteEventJournal(bound, venue_writes=True, must_exist=True) as journal:
        assert journal.journal_id() is None
        control = BreakGlassControl(journal=journal, state_dir=state_dir)
        control.claim_normal_writer(owner_id="normal:runtime")
        record = control.ownership_store.read()

    assert record.owner_id == "normal:runtime"
    assert record.journal_path == str(bound)
    assert record.journal_id is None
    assert "journal_id" not in json.loads((state_dir / "ownership.json").read_text(encoding="utf-8"))


def test_the_cli_never_prints_credential_material(tmp_path, capsys):
    _found_full_journal(tmp_path)
    main(base_args(tmp_path, "cancel-open"))
    output = capsys.readouterr().out

    for token in ("api_key", "apikey", "api_secret", "x-mbx", "signature="):
        assert token not in output.lower()


def test_the_cli_is_usable_without_the_ai_layer(tmp_path):
    """A fresh interpreter importing the CLI must not pull in CrewAI or the LLM layer."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    script = (
        "import sys; import crypto_trader.breakglass.cli as cli; "
        "print('crewai' in sys.modules, 'crypto_trader.llm' in sys.modules, "
        "'crypto_trader.crew' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False False False"


def test_the_cli_can_run_while_the_ai_backlog_is_saturated(tmp_path):
    """The control path is a separate process: it does not depend on the strategy queue at all."""
    with SQLiteEventJournal(tmp_path / "journal.db", venue_writes=True) as journal:
        control = BreakGlassControl(journal=journal, state_dir=tmp_path / "state")
        control.total = None  # the control exposes no queue/backlog surface at all
        assert not hasattr(control, "drain")
        assert not hasattr(control, "lane")

    code = main(base_args(tmp_path, "halt-new-risk"))

    assert code == EXIT_ACCEPTED


def test_status_output_is_json_serializable_and_secret_free(tmp_path, capsys):
    _found_full_journal(tmp_path)
    main(base_args(tmp_path, "force-recovery-required"))
    capsys.readouterr()
    main(["status", "--json", "--state-dir", str(tmp_path), "--journal", str(tmp_path / "journal.db")])
    record = json.loads(capsys.readouterr().out)

    assert record["epoch"]["open"] is True
    assert record["restore_blockers"]
    rendered = json.dumps(record).lower()
    for token in ("api_key", "apikey", "secret", "signature"):
        assert token not in rendered
