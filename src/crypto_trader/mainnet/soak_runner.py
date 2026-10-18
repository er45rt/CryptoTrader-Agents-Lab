"""Phase 15 operator entrypoint: real Binance Mainnet READ-ONLY dry-run soak.

Operational entrypoint (run it from the repository root)::

    python -m crypto_trader.mainnet.soak_runner --state-dir output/phase15-soak \
        --interval-seconds 60 --max-cycles 0

``--state-dir`` holds ONLY generated runtime artifacts (soak state, log, pid, journal): it is never
committed. This module is the committed, reproducible entrypoint, so an operator never depends on
an untracked local script.

Termination is deliberate and explicit, never a kill:

* ``--max-cycles N`` bounds a SHAKEDOWN rehearsal (>= 50 cycles is the documented minimum);
* ``--max-cycles 0`` runs until a ``STOP`` file appears in ``--state-dir`` (the official soak);

and either way the runner finishes the in-flight cycle, flushes the durable state and exits 0. A
shakedown is complete once it has met the Phase 15 evidence checklist (>= 50 completed cycles on
the documented minimum, at least one confirmed journal-determinism check, zero crash/exception,
zero mainnet mutation attempts, zero secret leaks, a re-validated read-only permission profile on
every cycle); the twelve soak exit criteria belong to the OFFICIAL soak that is started only after
the frozen candidate is pushed and tagged.

This is a thin, safety-preserving loop over the repository's documented Phase 15 implementation:

    market snapshot -> decision -> trade intent -> decision validity -> deterministic hard risk
    -> safety/writer fence -> OMS dry-run representation -> HARD NO-SUBMIT gate

with the durable ``SoakRecorder`` state as the exit-criteria evidence. It mirrors the wiring of the
opt-in live validation (``tests/test_mainnet_live_readonly.py``), including its explicit small
test-scale risk limits (NOT production limits) and its read-only decision source.

Safety invariants, none of which this runner relaxes:

* dedicated credentials from ``BINANCE_MAINNET_READONLY_API_KEY`` /
  ``BINANCE_MAINNET_READONLY_API_SECRET`` only; never printed, journaled, logged or persisted;
* the client can only issue allowlisted GET requests (its request helper accepts no HTTP method);
* the strict ``apiRestrictions`` profile is verified before start and re-verified at every
  would-be execution boundary; a non-read-only profile fails closed;
* the hard no-submit gate refuses every mutation before any transport call. This runner never
  calls ``try_to_submit`` and never constructs a venue order/cancel/replace/transfer/withdrawal
  request. If the gate ever reports a mutation attempt the loop exits immediately;
* the loop stops immediately when a ``STOP`` file appears in the state directory.

The runner does not change any Phase 15 exit criterion.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from crypto_trader.execution import (  # noqa: E402
    ExecutionOrderType,
    ExecutionSide,
    ExecutionTimeInForce,
    InstrumentSpec,
)
from crypto_trader.execution.binance_testnet import (  # noqa: E402
    RequestsTransport,
    TestnetClock as HttpClock,
)
from crypto_trader.mainnet import (  # noqa: E402
    HardNoSubmitGate,
    MainnetDryRunPipeline,
    MainnetReadOnlyClient,
    SoakRecorder,
    SoakStateStore,
    credentials_from_environment,
)
from crypto_trader.persistence import (  # noqa: E402
    IdempotentConsumer,
    SQLiteEventJournal,
    rebuild_state,
)
from crypto_trader.production import ProductionIntent  # noqa: E402
from crypto_trader.recovery import load_policy as load_recovery_policy  # noqa: E402
from crypto_trader.risk import RiskStateSnapshot, load_limits  # noqa: E402
from crypto_trader.validity import (  # noqa: E402
    DecisionValidityGate,
    LiveMarketStateProvider,
    MarketObservation,
)
from crypto_trader.validity import load_policy as load_validity_policy  # noqa: E402

SYMBOL = "BTCUSDT"
_SYNTHETIC_POLICY_ID = "mainnet-dryrun-test-v1"
_SYNTHETIC_INSTRUMENT = InstrumentSpec(
    symbol=SYMBOL,
    venue="BINANCE_SPOT_MAINNET_READONLY",
    base_currency="BTC",
    quote_currency="USDT",
    price_precision=2,
    size_precision=5,
    starting_balance="0 USDT",
)
_SYNTHETIC_LIMIT_CONFIG = {
    "limits_version": "risk-mainnet-dryrun-test-v1",
    "max_order_notional_quote": "5000",
    "max_symbol_exposure_quote": "20000",
    "max_gross_exposure_quote": "50000",
    "max_net_exposure_quote": "30000",
    "max_strategy_exposure_quote": "40000",
    "loss_limit_quote": "1000",
    "max_drawdown_pct": "10",
    "concentration_limit_pct": "50",
    "max_open_orders": 5,
    "max_open_positions": 3,
}
DETERMINISM_CHECK_EVERY = 30
MAX_CONSECUTIVE_CYCLE_FAILURES = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JsonLineLogger:
    """Append-only secret-free JSON-lines log."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a", encoding="utf-8")

    def write(self, event: str, message: str, **fields: Any) -> None:
        record = {
            "ts": _now().isoformat(),
            "event": event,
            "message": message,
            **fields,
        }
        self._stream.write(json.dumps(record, sort_keys=True) + "\n")
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


class _ReplayConsumer(IdempotentConsumer):
    """Replay-only consumer: the comparison target for the determinism check."""

    def apply(self, event) -> None:  # noqa: D102 - no side effects by design
        del event


def _journal_is_deterministic(journal: SQLiteEventJournal) -> bool:
    first = rebuild_state(journal, _ReplayConsumer())
    second = rebuild_state(journal, _ReplayConsumer())
    if first.events_seen == 0:
        return False
    return (
        first.events_seen == second.events_seen
        and first.events_applied == second.events_applied
        and first.duplicates_skipped == second.duplicates_skipped
        and first.last_event_id == second.last_event_id
        and first.last_sequence_number == second.last_sequence_number
    )


def _live_market_state(client: MainnetReadOnlyClient) -> LiveMarketStateProvider:
    def latest(symbol: str) -> MarketObservation:
        stamp = _now()
        bid, ask = client.book_ticker(symbol)
        return MarketObservation(
            symbol=symbol,
            price=(bid + ask) / Decimal(2),
            market_event_ts=stamp,
            local_ingest_ts=stamp,
            market_reference=f"live-{stamp.timestamp()}",
        )

    return LiveMarketStateProvider(latest)


def _decision_source(client: MainnetReadOnlyClient, rules):
    def decide(snapshot):
        bid, ask = client.book_ticker(SYMBOL)
        mid = (bid + ask) / Decimal(2)
        tick = rules.tick_size.normalize()
        step = rules.step_size.normalize()
        limit_price = ((mid / tick).to_integral_value(rounding=ROUND_FLOOR) * tick).quantize(tick)
        minimum_quantity = max(rules.min_notional / limit_price, rules.min_quantity)
        quantity = (
            (minimum_quantity / step).to_integral_value(rounding=ROUND_CEILING) * step
        ).quantize(step)
        stamp = _now()
        return ProductionIntent(
            correlation_id=uuid4(),
            symbol=SYMBOL,
            side=ExecutionSide.BUY,
            order_type=ExecutionOrderType.LIMIT,
            quantity=quantity,
            strategy_id="strategy.mainnet-readonly-soak",
            decision_started_at=stamp,
            decision_completed_at=stamp,
            market_event_ts=snapshot.market_event_ts,
            reference_price=mid,
            market_reference=snapshot.market_reference,
            limit_price=limit_price,
            time_in_force=ExecutionTimeInForce.FOK,
        )

    return decide


def _build_client() -> MainnetReadOnlyClient:
    client = MainnetReadOnlyClient(
        credentials=credentials_from_environment(),
        transport=RequestsTransport(timeout_seconds=15.0),
        clock=HttpClock(time_source=time.time, recv_window_ms=5_000),
        symbol=SYMBOL,
    )
    client.synchronize_clock()
    return client


def _synthetic_risk_state(now: datetime) -> RiskStateSnapshot:
    return RiskStateSnapshot(
        equity_quote=Decimal("1000"),
        peak_equity_quote=Decimal("1000"),
        realized_pnl_quote=Decimal("0"),
        gross_exposure_quote=Decimal("0"),
        net_exposure_quote=Decimal("0"),
        open_orders=0,
        open_positions=0,
        snapshot_ts=now,
    )


def _synthetic_validity_gate() -> DecisionValidityGate:
    return DecisionValidityGate(
        load_validity_policy(
            {
                "policy_id": _SYNTHETIC_POLICY_ID,
                "max_age_seconds": 120,
                "max_market_move_bps": "200",
            }
        )
    )


def _build_pipeline(client: MainnetReadOnlyClient, journal, state_dir: Path, recorder: SoakRecorder):
    return MainnetDryRunPipeline(
        journal=journal,
        state_dir=state_dir / "state",
        instrument=_SYNTHETIC_INSTRUMENT,
        market_state=_live_market_state(client),
        decision_source=_decision_source(client, client.exchange_info(SYMBOL)),
        limits=load_limits(_SYNTHETIC_LIMIT_CONFIG),
        risk_state_provider=_synthetic_risk_state,
        validity_gate=_synthetic_validity_gate(),
        reconciliation_policy=load_recovery_policy({"policy_id": _SYNTHETIC_POLICY_ID}),
        permission_check=client.verify_read_only_permissions,
        gate=HardNoSubmitGate(instrument=_SYNTHETIC_INSTRUMENT),
        client=client,
        soak=recorder,
        clock=_now,
        owner_id="normal:phase15-mainnet-readonly-soak",
        producer="application.mainnet.soak",
    )


def _sleep_until_stop(state_dir: Path, seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if (state_dir / "STOP").exists():
            return
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 15 mainnet read-only dry-run soak")
    parser.add_argument("--state-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument(
        "--max-cycles", type=int, default=0, help="0 runs until STOP/failure (the real soak)"
    )
    parser.add_argument(
        "--planned-restart",
        action="store_true",
        help="record this launch as a deliberate planned restart/recovery exercise",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    state_dir = Path(args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    logger = JsonLineLogger(state_dir / "soak.log")
    try:
        return _run(args, state_dir, logger)
    finally:
        logger.close()


def _run(args: argparse.Namespace, state_dir: Path, logger: JsonLineLogger) -> int:
    pid_path = state_dir / "soak.pid"
    store = SoakStateStore(state_dir / "soak.json")

    try:
        client = _build_client()
        permissions = client.verify_read_only_permissions()
    except Exception as exc:
        logger.write("fatal", "read-only startup failed", error=type(exc).__name__, detail=str(exc))
        return 1
    logger.write(
        "permission_profile_verified",
        "strict read-only apiRestrictions profile verified",
        **permissions.to_record(),
    )

    recorder = SoakRecorder(store, clock=_now)
    resumed = store.exists()
    if resumed and args.planned_restart:
        recorder.resume(planned=True)
        logger.write("planned_restart_recorded", "planned restart/recovery exercise recorded")
    recorder.set_journal_deterministic(False)  # persist soak_start_time + heartbeat immediately

    pid_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started_at": _now().isoformat(),
                "mode": "mainnet_readonly_dry_run",
                "state_file": str(store.path),
                "log_path": str(state_dir / "soak.log"),
                "state_dir": str(state_dir),
                "resumed": resumed,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    logger.write(
        "soak_started",
        "real mainnet read-only dry-run soak process started",
        pid=os.getpid(),
        resumed=resumed,
        interval_seconds=args.interval_seconds,
        snapshot=recorder.snapshot(),
    )

    with ExitStack() as stack:
        journal = stack.enter_context(SQLiteEventJournal(state_dir / "journal.db"))
        pipeline = _build_pipeline(client, journal, state_dir, recorder)
        try:
            started = pipeline.start()
        except Exception as exc:
            logger.write(
                "fatal",
                "dry-run pipeline failed to start",
                error=type(exc).__name__,
                detail=str(exc),
            )
            return 1
        logger.write("pipeline_started", "dry-run pipeline started", **started)

        cycles_done = 0
        failures = 0
        while True:
            if (state_dir / "STOP").exists():
                logger.write("stopped", "STOP file observed; exiting cleanly")
                break
            try:
                result = pipeline.run_cycle()
            except Exception as exc:
                failures += 1
                logger.write(
                    "cycle_error",
                    "cycle raised",
                    consecutive_failures=failures,
                    error=type(exc).__name__,
                    detail=str(exc),
                    traceback=traceback.format_exc(limit=6),
                )
                if failures >= MAX_CONSECUTIVE_CYCLE_FAILURES:
                    logger.write(
                        "fatal",
                        "too many consecutive cycle failures; leaving durable state untouched",
                    )
                    return 1
                _sleep_until_stop(state_dir, args.interval_seconds)
                continue
            failures = 0
            cycles_done += 1

            gate_attempts = int(pipeline.status()["mainnet_mutation_attempts"])
            logger.write(
                "cycle",
                "dry-run cycle completed",
                cycles_done=cycles_done,
                result=result.to_record(),
                gate_mutation_attempts=gate_attempts,
            )
            if gate_attempts != 0:
                logger.write(
                    "fatal",
                    "the hard no-submit gate reported a mainnet mutation attempt; stopping",
                    gate_mutation_attempts=gate_attempts,
                )
                return 2

            if cycles_done % DETERMINISM_CHECK_EVERY == 0:
                deterministic = _journal_is_deterministic(journal)
                recorder.set_journal_deterministic(deterministic)
                logger.write(
                    "journal_determinism_check",
                    "journal replay determinism checked",
                    deterministic=deterministic,
                )

            logger.write(
                "soak_state",
                "durable soak snapshot",
                snapshot=recorder.snapshot(),
            )
            if args.max_cycles and cycles_done >= args.max_cycles:
                logger.write("stopped", "max cycles reached; exiting cleanly", cycles_done=cycles_done)
                break
            _sleep_until_stop(state_dir, args.interval_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
