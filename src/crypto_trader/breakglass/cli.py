"""The narrow local operator CLI for break-glass control (Phase 13).

        python -m crypto_trader.breakglass.cli status --state-dir <dir>
        python -m crypto_trader.breakglass.cli run --command-type halt-new-risk \
            --command-id <uuid> --operator <name> --reason "<why>" --state-dir <dir>

The journal is always ``<state-dir>/journal.db``: one system's ownership state and journal live
together, so a mistyped path can never pair one system's journal with another system's lease.

Properties required of this control channel:

* usable without an LLM and while the AI queue/backlog is stalled - this process imports no agent,
  crew or model code and never touches the strategy path;
* explicit and operator initiated: every command needs an idempotency key, an operator label and a
  reason; nothing is inferred from conversation state or from a default;
* malformed/unknown commands are refused with a non-zero exit code and change nothing (fail closed);
* no credential is ever printed, echoed or persisted: the CLI prints only the secret-free outcome
  record, and the venue client loads its key pair from the environment through the Phase 11
  adapter, which never exposes a value.

Exit codes: ``0`` accepted (including an idempotent re-delivery), ``3`` refused by the deterministic
safety policy, ``2`` malformed/unknown command or unusable configuration, ``4`` incomplete: the
journal failed after the command started, so its lease or venue side effects may already be in effect.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from crypto_trader.events.errors import EventContractError
from crypto_trader.execution import InstrumentSpec
from crypto_trader.execution.binance_testnet import (
    BinanceSpotTestnetClient,
    RequestsTransport,
    TestnetClock,
    TestnetOrderRuntime,
    WithdrawalPolicy,
    credentials_from_environment,
)
from crypto_trader.persistence import JournalError, JournalSchemaError, SQLiteEventJournal
from crypto_trader.recovery import load_policy as load_recovery_policy

from .commands import parse_operator_command
from .control import BreakGlassControl
from .errors import BreakGlassError, MalformedOperatorCommandError
from .venue import BinanceSpotTestnetEmergencyControl, PolicyOnlyVenueControl

__all__ = [
    "EXIT_ACCEPTED",
    "EXIT_INCOMPLETE",
    "EXIT_MALFORMED",
    "EXIT_REFUSED",
    "build_parser",
    "main",
]

EXIT_ACCEPTED = 0
EXIT_MALFORMED = 2
EXIT_REFUSED = 3
EXIT_INCOMPLETE = 4

DEFAULT_RECOVERY_POLICY_ID = "recovery-breakglass-v1"
EXCHANGE_FALLBACK = "If orders are open, cancel them directly at the exchange."


def build_parser() -> argparse.ArgumentParser:
    """Build the operator CLI parser (deterministic, no defaults that could downgrade safety)."""
    parser = argparse.ArgumentParser(
        prog="break-glass",
        description="Deterministic operator break-glass control (Spot Testnet only; no mainnet).",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    status = subparsers.add_parser("status", help="report the current break-glass posture (read-only)")
    _add_common_paths(status, journal_required=False)
    status.add_argument("--json", action="store_true", help="emit the record as JSON")

    run = subparsers.add_parser("run", help="deliver one operator break-glass command")
    _add_common_paths(run, journal_required=False)
    run.add_argument(
        "--command-type",
        "--command",
        dest="command_type",
        required=True,
        help="halt-new-risk | cancel-open | reduce-only | enter-degraded | "
        "force-recovery-required | reconcile | acknowledge | restore-normal",
    )
    run.add_argument("--command-id", required=True, help="operator idempotency key (UUID)")
    run.add_argument("--operator", required=True, help="operator name or label (never a secret)")
    run.add_argument("--operator-identity", default=None, help="optional stronger operator identity")
    run.add_argument("--reason", required=True, help="why this command is being issued")
    run.add_argument("--source", default="operator-cli", help="control-channel identifier")
    run.add_argument(
        "--requested-at",
        default=None,
        help="ISO-8601 request time (defaults to the current UTC time)",
    )
    run.add_argument("--correlation-id", default=None, help="optional workflow correlation UUID")
    run.add_argument("--causation-id", default=None, help="optional causing event UUID")
    run.add_argument(
        "--venue",
        choices=("none", "testnet"),
        default="none",
        help="attach venue-side emergency control (testnet is Binance SPOT TESTNET only)",
    )
    run.add_argument("--symbol", default="BTCUSDT", help="instrument for the testnet venue control")
    run.add_argument(
        "--testnet-withdrawal-policy",
        choices=("require-disabled", "record-only"),
        default="require-disabled",
        help="require-disabled fails closed on a key with withdrawal capability",
    )
    run.add_argument(
        "--recovery-policy-id",
        default=DEFAULT_RECOVERY_POLICY_ID,
        help="Phase 8 reconciliation policy identity used by the reconcile command",
    )
    run.add_argument("--json", action="store_true", help="emit the outcome record as JSON")
    return parser


def _add_common_paths(parser: argparse.ArgumentParser, *, journal_required: bool) -> None:
    parser.add_argument(
        "--journal", required=journal_required, default=None, help="the event journal; must be <state-dir>/journal.db (the default)"
    )
    parser.add_argument(
        "--state-dir",
        required=True,
        help="directory holding the persisted break-glass state (safety/ownership/epoch)",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the process exit code (never raises for a refused command)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.subcommand == "status":
            return _run_status(args)
        if args.subcommand == "run":
            return _run_command(args)
    except MalformedOperatorCommandError as exc:
        return _refuse(str(exc))
    except BreakGlassError as exc:
        return _refuse(f"{type(exc).__name__}: {exc}")
    parser.error("unknown subcommand")  # pragma: no cover - argparse guarantees a subcommand
    return EXIT_MALFORMED  # pragma: no cover


def _run_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    journal_path, mismatch = _journal_for(args)
    if mismatch is not None:
        return _refuse(mismatch)
    try:
        with SQLiteEventJournal(journal_path, read_only=True) as journal:
            control = BreakGlassControl(journal=journal, state_dir=state_dir)
            record = control.status().to_record()
    except (JournalError, EventContractError, sqlite3.Error) as exc:  # never creates or repairs
        return _refuse(f"{type(exc).__name__}: {exc}")
    _emit(record, as_json=args.json)
    return EXIT_ACCEPTED


def _run_command(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    delivered_at = datetime.now(timezone.utc)
    command = parse_operator_command(
        {
            "command_type": args.command_type,
            "command_id": args.command_id,
            "requested_at": args.requested_at,
            "operator_label": args.operator,
            "operator_identity": args.operator_identity,
            "reason": args.reason,
            "source": args.source,
            "correlation_id": args.correlation_id,
            "causation_id": args.causation_id,
        },
        delivered_at=delivered_at,
    )
    journal_path, mismatch = _journal_for(args)
    if mismatch is not None:
        return _refuse(f"{mismatch} {EXCHANGE_FALLBACK}")
    try:
        journal = _open_emergency_journal(journal_path)
    except (JournalError, sqlite3.Error) as exc:  # before any lease, state or venue side effect
        return _refuse(f"{type(exc).__name__}: {exc} [journal: {journal_path}] {EXCHANGE_FALLBACK}")
    with journal:
        venue = _build_venue(args)
        control = BreakGlassControl(
            journal=journal,
            state_dir=state_dir,
            venue=venue,
            reconciliation_policy=load_recovery_policy({"policy_id": args.recovery_policy_id}),
        )
        try:
            outcome = control.handle(command)
        except (JournalError, EventContractError, sqlite3.Error, OSError) as exc:
            # Never "nothing changed": the lease or the venue cancel may precede the failed append.
            return _incomplete(f"{type(exc).__name__}: {exc} [journal: {journal_path}]")
        record = outcome.to_record()
    _emit(record, as_json=args.json)
    return EXIT_ACCEPTED if outcome.accepted else EXIT_REFUSED


def _open_emergency_journal(journal_path: Path) -> SQLiteEventJournal:
    """Open the existing FULL-founded journal for durable emergency writes; never create one."""
    try:
        exists = journal_path.is_file()
    except (OSError, ValueError) as exc:
        raise JournalSchemaError(f"the journal could not be checked: {exc}") from exc
    if not exists:
        raise JournalSchemaError("the journal is not an existing file; run never creates a journal.")
    # must_exist closes the window between the check above and the connect: never found anew.
    return SQLiteEventJournal(journal_path, venue_writes=True, must_exist=True)


def _build_venue(args: argparse.Namespace):
    """Attach the venue-side emergency control when the operator explicitly asked for it."""
    if args.venue == "none":
        return PolicyOnlyVenueControl()
    instrument = InstrumentSpec(symbol=args.symbol, venue="BINANCE_SPOT_TESTNET")
    client = BinanceSpotTestnetClient(
        credentials=credentials_from_environment(),
        transport=RequestsTransport(timeout_seconds=15.0),
        clock=TestnetClock(time_source=_monotonic_utc, recv_window_ms=5_000),
        symbol=args.symbol,
        withdrawal_policy=(
            WithdrawalPolicy.REQUIRE_DISABLED
            if args.testnet_withdrawal_policy == "require-disabled"
            else WithdrawalPolicy.RECORD_ONLY
        ),
    )
    runtime = TestnetOrderRuntime(client=client, instrument=instrument)
    runtime.start()
    return BinanceSpotTestnetEmergencyControl(client=client, runtime=runtime)


def _monotonic_utc() -> float:
    return datetime.now(timezone.utc).timestamp()


def _journal_for(args: argparse.Namespace) -> tuple[Path, str | None]:
    """The journal bound to ``--state-dir`` is always ``<state-dir>/journal.db``; refuse any other."""
    bound = _default_journal(Path(args.state_dir))
    if args.journal is None:
        return bound, None
    given = Path(args.journal)
    if os.path.normcase(os.path.abspath(given)) == os.path.normcase(os.path.abspath(bound)):
        return bound, None
    return bound, (
        f"--journal {given} is not the journal bound to --state-dir {args.state_dir} "
        f"(expected {bound}); refusing to pair a journal with another system's ownership state."
    )


def _default_journal(state_dir: Path) -> Path:
    return state_dir / "journal.db"


def _emit(record: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        return
    sys.stdout.write(_render(record) + "\n")


def _render(record: dict[str, Any]) -> str:
    lines = [f"{key}: {value}" for key, value in sorted(record.items())]
    return "\n".join(lines)


def _incomplete(message: str) -> int:
    sys.stderr.write(
        f"INCOMPLETE (side effects may already be in effect): {message}. The emergency lease and "
        "any venue cancel may already have happened: check status and re-deliver the same "
        f"--command-id. {EXCHANGE_FALLBACK}\n"
    )
    return EXIT_INCOMPLETE


def _refuse(message: str) -> int:
    sys.stderr.write(f"REFUSED (fail closed, nothing changed): {message}\n")
    return EXIT_MALFORMED


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
