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

from crypto_trader.breakglass import BreakGlassControl, BreakGlassStateError, OwnershipStateError
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
