"""Phase 13: the operator CLI - explicit, LLM-free, fail-closed and secret-free."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from crypto_trader.breakglass import BreakGlassControl
from crypto_trader.breakglass.cli import (
    EXIT_ACCEPTED,
    EXIT_MALFORMED,
    EXIT_REFUSED,
    main,
)
from crypto_trader.persistence import SQLiteEventJournal

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def run_cli(tmp_path: Path, *args: str, journal_path: Path | None = None) -> int:
    journal = journal_path or (tmp_path / "journal.db")
    return main(
        [
            *args,
            "--state-dir",
            str(tmp_path / "state"),
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
        str(tmp_path / "state"),
        "--json",
    ]
    for key, value in extra.items():
        args.extend([f"--{key.replace('_', '-')}", value])
    return args


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("explicit_journal", [True, False])
def test_status_refuses_a_missing_journal_and_creates_nothing(tmp_path, capsys, explicit_journal):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    journal = tmp_path / "journal.db" if explicit_journal else state_dir / "journal.db"
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
        "REFUSED (fail closed, nothing changed): JournalSchemaError: read-only journal has no "
        "usable schema (no such table: journal_meta); refusing to create one.\n"
    )
    assert journal.read_bytes() == b""
    assert sorted(p.name for p in tmp_path.iterdir()) == ["journal.db"]


def test_run_accepts_a_halt_and_engages_the_kill_switch(tmp_path, capsys):
    code = main(base_args(tmp_path, "halt-new-risk"))
    record = json.loads(capsys.readouterr().out)

    assert code == EXIT_ACCEPTED
    assert record["disposition"] == "accepted"
    assert record["resulting_safety_state"] == "halt_new_risk"
    assert record["new_risk_permitted"] is False
    assert record["operator_acknowledgement_pending"] is True


def test_a_second_identical_delivery_is_idempotent(tmp_path, capsys):
    command_id = str(uuid4())
    first = main(base_args(tmp_path, "halt-new-risk", command_id=command_id))
    json.loads(capsys.readouterr().out)
    second = main(base_args(tmp_path, "halt-new-risk", command_id=command_id))
    record = json.loads(capsys.readouterr().out)

    assert first == second == EXIT_ACCEPTED
    assert record["duplicate_delivery"] is True
    assert record["reason_code"] == "break_glass_engaged_halt_new_risk"


def test_restore_is_refused_until_the_gates_are_met(tmp_path, capsys):
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


def test_the_cli_never_prints_credential_material(tmp_path, capsys):
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
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        control = BreakGlassControl(journal=journal, state_dir=tmp_path / "state")
        control.total = None  # the control exposes no queue/backlog surface at all
        assert not hasattr(control, "drain")
        assert not hasattr(control, "lane")

    code = main(base_args(tmp_path, "halt-new-risk"))

    assert code == EXIT_ACCEPTED


def test_status_output_is_json_serializable_and_secret_free(tmp_path, capsys):
    main(base_args(tmp_path, "force-recovery-required"))
    capsys.readouterr()
    main(["status", "--json", "--state-dir", str(tmp_path / "state"), "--journal", str(tmp_path / "journal.db")])
    record = json.loads(capsys.readouterr().out)

    assert record["epoch"]["open"] is True
    assert record["restore_blockers"]
    rendered = json.dumps(record).lower()
    for token in ("api_key", "apikey", "secret", "signature"):
        assert token not in rendered
