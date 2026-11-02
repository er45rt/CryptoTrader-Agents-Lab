"""Crash-only Paper host: AI -> Decision -> Risk -> Paper -> Feedback.

There is no in-memory trading state to lose. Each cycle rebuilds a fresh simulator and replays the
whole durable tape with the durable decisions through the unchanged TradingSession. The journal is
idempotent, so a replay adds only what is new, and any divergence raises on an event identity
instead of writing a second effect. A restart is therefore the same code path as a normal cycle.

Write order is what makes that safe: tape line -> observation -> sealed decision -> journal ->
state file. Each step only depends on steps already fsynced.
"""
from __future__ import annotations

import hashlib
import json
import msvcrt
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from crypto_trader.crew_paper_bridge import (
    CrewPaperBridgeConfig, CrewPaperDecisionSource, portfolio_snapshot,
)
from crypto_trader.execution import InstrumentSpec
from crypto_trader.execution.nautilus import NautilusSimulationRunner
from crypto_trader.paper import (
    MarketObservation, PaperStateError, TradingMode, TradingSession, load_session_config,
    report_from_journal,
)
from crypto_trader.paper.config import SpotCashSettings
from crypto_trader.paper.decisions import input_json
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.risk import load_limits
from crypto_trader.safety import SafetyController
from crypto_trader.validity import DecisionValidityGate, load_policy

from .public_market import FeedUnavailable, MarketBatch
from .tape import MarketTape

__all__ = ["HostConfig", "PaperHost", "SessionLock", "create_session", "read_status"]

MANIFEST = "manifest.json"
STATE = "state.json"
STOP_REQUEST = "stop.request"
LOCK = "host.lock"
TERMINAL = {"filled", "canceled", "rejected", "expired"}
_END_OF_TIME = datetime(9999, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class HostConfig:
    """Everything that shapes the replay; sealed at creation so resume cannot drift."""

    session_id: str
    symbol: str
    base_currency: str
    quote_currency: str
    starting_quote: str
    max_notional_quote: str
    decision_interval_seconds: int
    max_plan_ttl_seconds: int
    tick_size: str
    quantity_step: str
    price_precision: int
    size_precision: int
    maker_fee: str
    taker_fee: str
    provider_kind: str
    limits: dict[str, Any]
    validity: dict[str, Any]
    strategy_id: str = "crew-paper-host"
    max_feed_age_seconds: int = 120

    def __post_init__(self) -> None:
        if self.provider_kind not in {"crew", "stub"}:
            raise PaperStateError("provider_kind must be crew or stub.")
        if self.decision_interval_seconds <= 0:
            raise PaperStateError("decision interval must be positive.")
        limits = load_limits(self.limits)
        notional = Decimal(self.max_notional_quote)
        if limits.max_gross_exposure_quote < 2 * notional:
            # A protective exit is projected on top of the position it closes.
            raise PaperStateError("max_gross_exposure_quote must allow exiting a full position.")

    @classmethod
    def create(cls, **values: Any) -> "HostConfig":
        return cls(session_id=str(uuid4()), **values)


class SessionLock:
    """OS byte-range lock: released by the kernel however the process dies."""

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / LOCK
        self._stream = None

    def __enter__(self) -> "SessionLock":
        stream = self.path.open("a+b")
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            stream.close()
            raise PaperStateError("another host process owns this session.") from exc
        self._stream = stream
        return self

    def __exit__(self, *exc: Any) -> None:
        stream, self._stream = self._stream, None
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        stream.close()


def _atomic_write(path: Path, data: bytes) -> None:
    partial = path.with_name(f"{path.name}.{uuid4().hex[:8]}.partial")
    with partial.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, path)


def create_session(directory: Path, config: HostConfig) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "decisions").mkdir()
    body = asdict(config)
    record = {"config": body, "sha256": hashlib.sha256(input_json(body)).hexdigest()}
    _atomic_write(directory / MANIFEST, json.dumps(record, sort_keys=True, indent=1).encode("utf-8"))
    return directory


def load_manifest(directory: Path) -> HostConfig:
    try:
        record = json.loads((Path(directory) / MANIFEST).read_bytes())
    except (OSError, ValueError) as exc:
        raise PaperStateError("session manifest is missing or unreadable.") from exc
    if hashlib.sha256(input_json(record["config"])).hexdigest() != record["sha256"]:
        raise PaperStateError("session manifest does not match its seal.")
    return HostConfig(**record["config"])


def read_status(directory: Path) -> dict[str, Any]:
    """State rebuilt from the journal, never from the state file, plus that file's last write."""
    directory = Path(directory)
    config = load_manifest(directory)
    state = json.loads((directory / STATE).read_bytes()) if (directory / STATE).exists() else None
    with SQLiteEventJournal(directory / "journal.db") as journal:
        recovered = _journal_state(journal, config, as_of=_END_OF_TIME)
    return {"config_session": config.session_id, "provider_kind": config.provider_kind,
            "recovered": recovered, "last_state": state}


def _bridge_config(config: HostConfig) -> CrewPaperBridgeConfig:
    return CrewPaperBridgeConfig(
        session_id=UUID(config.session_id), symbol=config.symbol,
        starting_equity_quote=Decimal(config.starting_quote),
        max_notional_quote=Decimal(config.max_notional_quote),
        tick_size=Decimal(config.tick_size), quantity_step=Decimal(config.quantity_step),
        max_plan_ttl=timedelta(seconds=config.max_plan_ttl_seconds), immediate_orders=True,
    )


def _journal_state(journal: Any, config: HostConfig, *, as_of: datetime) -> dict[str, Any]:
    session_id = UUID(config.session_id)
    events = journal.read_by_correlation(session_id)
    fills = [event for event in events if event.event_type == "execution.fill.recorded"]
    orders = {str(event.order_id) for event in events if event.event_type == "order.state.changed"}
    closed = {str(event.order_id) for event in events if event.event_type == "order.state.changed"
              and event.payload.state in TERMINAL}
    mark = fills[-1].payload.price if fills else Decimal(config.starting_quote)
    snapshot = portfolio_snapshot(journal, config=_bridge_config(config), mark=mark, as_of=as_of)
    return {
        "position_quantity": str(snapshot.position_quantity),
        "average_cost": str(snapshot.average_cost),
        "realized_pnl_quote": str(snapshot.realized_pnl_quote),
        "quote_free": str(snapshot.quote_free),
        "stop_exits": snapshot.stop_exits, "target_exits": snapshot.target_exits,
        "fills": len(fills), "fill_ids": sorted(str(event.event_id) for event in fills),
        "open_orders": sorted(orders - closed),
        "journal_events": len(events),
    }


@dataclass
class PaperHost:
    directory: Path
    source: Callable[[int | None], MarketBatch]
    plan_provider: Callable[[dict[str, Any]], str]
    wall_clock_ns: Callable[[], int] = time.time_ns
    sleep: Callable[[float], None] = time.sleep
    crash: Callable[[str], None] = lambda point: None
    journal_wrapper: Callable[[Any], Any] = lambda journal: journal
    provenance: dict[str, str] = field(default_factory=dict)
    backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    stamp_attempts: int = 5

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)
        self.config = load_manifest(self.directory)
        self.tape = MarketTape(self.directory / "tape.jsonl", self.config.symbol,
                               tick_size=Decimal(self.config.tick_size))
        self.feed_failures = 0
        self.last_report: Any = None
        self.last_view: dict[str, Any] = {}
        self._unsealed_plans: dict[bytes, str] = {}

    # --- market input ---

    def _ingest(self, batch: MarketBatch) -> int | None:
        self.tape.ingest_trades(list(batch.trades))
        stamp = self.tape.ingest_book(batch.book) if batch.book is not None else None
        self.crash("before_tape_commit")
        self.tape.commit()
        self.crash("after_tape_commit")
        return stamp

    def _poll(self) -> int | None:
        batch = self.source(self.tape.last_trade_id)
        self.feed_failures = 0
        return self._ingest(batch)

    def _feed_age_seconds(self) -> float:
        if not self.tape.last_book_ns:
            return float("inf")
        return (self.wall_clock_ns() - self.tape.last_book_ns) / 1e9

    def _decision_due(self, observations: tuple[MarketObservation, ...]) -> bool:
        if not observations:
            return True
        last = observations[-1].observed_at.timestamp()
        return self.tape.last_book_ns / 1e9 - last >= self.config.decision_interval_seconds

    def _mid(self) -> Decimal:
        row = next(item for item in reversed(self.tape.rows) if item["kind"] == "book")
        return Decimal(row["ask"])  # A long entry pays the spread; the conservative reference.

    # --- durable decisions ---

    def _plan_once(self, inputs: dict[str, Any]) -> str:
        """A feed outage can fail the stamp after the model answered: the retry reuses that answer."""
        key = input_json(inputs)
        if key not in self._unsealed_plans:
            self._unsealed_plans[key] = self.plan_provider(inputs)
        return self._unsealed_plans[key]

    def _source(self, journal: Any, *, prepared_only: bool) -> CrewPaperDecisionSource:
        def stamp(observation: MarketObservation) -> datetime:
            """Completion time is a real tape event after the model returned."""
            del observation
            for attempt in range(self.stamp_attempts):
                try:
                    batch_ts = self._poll()
                except FeedUnavailable:
                    self.sleep(self.backoff_seconds * 2 ** attempt)
                    continue
                if batch_ts is not None:
                    return self.tape._when(batch_ts)
            raise FeedUnavailable("no market event can stamp this decision.")

        return CrewPaperDecisionSource(
            config=_bridge_config(self.config), journal=journal,
            store_dir=self.directory / "decisions", clock=stamp,
            plan_provider=self._plan_once, prepared_only=prepared_only,
            provenance={"provider_kind": self.config.provider_kind, **self.provenance},
        )

    def _prepare_pending(self, journal: Any, observations: tuple[MarketObservation, ...]) -> bool:
        source = self._source(journal, prepared_only=False)
        for observation in observations:
            if source.has_decision(observation):
                continue
            self.crash("before_decision")
            source.prepare(observation)
            self._unsealed_plans.clear()
            self.crash("after_decision")
            return True
        return False

    # --- replay ---

    def _runner(self) -> NautilusSimulationRunner:
        config = self.config
        books, trades = self.tape.market_events()
        runner = NautilusSimulationRunner(
            instrument=InstrumentSpec(
                symbol=config.symbol, base_currency=config.base_currency,
                quote_currency=config.quote_currency, price_precision=config.price_precision,
                size_precision=config.size_precision,
                starting_balance=f"{config.starting_quote} {config.quote_currency}",
            ),
            book_updates=books, trades=trades,
        )
        runner.configure_paper_cash(SpotCashSettings(
            initial_quote=Decimal(config.starting_quote),
            maker_fee=Decimal(config.maker_fee), taker_fee=Decimal(config.taker_fee),
        ))
        return runner

    def _replayable(self, journal: Any) -> tuple[MarketObservation, ...]:
        """Observations whose decision exists and whose submission a later event can certify."""
        source = self._source(journal, prepared_only=True)
        ready: list[MarketObservation] = []
        for observation in self.tape.observations():
            if not source.has_decision(observation):
                break
            decision = source.decide(observation)
            stamp_ns = TradingSession._timestamp_ns(decision.decided_at or observation.observed_at)
            if not self.tape.has_event_after(stamp_ns):
                break
            ready.append(observation)
        return tuple(ready)

    def _replay(self, journal: Any) -> dict[str, Any] | None:
        observations = self._replayable(journal)
        if not observations:
            return None
        config = self.config
        session_config = load_session_config({
            "mode": TradingMode.PAPER.value, "symbol": config.symbol,
            "strategy_id": config.strategy_id, "session_id": config.session_id,
            "starting_equity_quote": config.starting_quote, "producer": "application.paper_host",
        })
        runner = self._runner()
        session = TradingSession(
            config=session_config, journal=self.journal_wrapper(journal),
            feed=self.tape.feed(observations),
            decisions=self._source(journal, prepared_only=True),
            validity_gate=DecisionValidityGate(load_policy(config.validity)),
            limits=load_limits(config.limits),
            safety=SafetyController(correlation_id=UUID(config.session_id)),
            simulator=runner,
        )
        report = session.run()
        self.crash("after_replay")
        self.last_report = report
        return self._verify(journal, report, session, runner)

    def _verify(self, journal: Any, report: Any, session: Any, runner: Any) -> dict[str, Any]:
        """Three witnesses must agree, or the session stops rather than trades on a guess."""
        config = self.config
        rebuilt = report_from_journal(
            journal.read_by_correlation(UUID(config.session_id)), session_id=UUID(config.session_id),
            mode="paper", symbol=config.symbol, strategy_id=config.strategy_id,
            strict_native_provenance=True,
        )
        ledger = session._r02_ledger
        native = runner.cash_end_evidence()
        journal_state = _journal_state(journal, config, as_of=_END_OF_TIME)
        if (len(rebuilt.fills) != len(report.fills)
                or sum((fill.quantity for fill in rebuilt.fills), Decimal("0"))
                != sum((fill.quantity for fill in report.fills), Decimal("0"))):
            raise PaperStateError("replay divergence: journal and engine fills differ.")
        if (native.quote_total != ledger.quote_total or native.base_total != ledger.base_total
                or Decimal(journal_state["position_quantity"]) != ledger.base_total
                or Decimal(journal_state["quote_free"])
                != ledger.quote_total - sum((quantity * price for side, quantity, price
                                             in ledger._cash_locks.values() if side == "buy"), Decimal("0"))):
            raise PaperStateError("replay divergence: journal, ledger and native cash disagree.")
        return {
            "quote_total": str(ledger.quote_total), "base_total": str(ledger.base_total),
            "fees_quote": str(ledger.fees_quote()), "equity_quote": str(report.equity_quote),
            "realized_pnl_quote": str(report.realized_pnl_quote),
            "unrealized_pnl_quote": str(report.unrealized_pnl_quote),
            "orders": [asdict(order) | {"intent_id": str(order.intent_id)} for order in report.orders
                       if order.state not in TERMINAL],
            "protections": session.protections.snapshot(),
            "protection_log": session.protections.log[-40:],
            "journal": journal_state,
        }

    # --- lifecycle ---

    def cycle(self) -> dict[str, Any]:
        """One crash-safe step. Every intermediate position is a valid restart point."""
        try:
            stamp = self._poll()
        except FeedUnavailable as exc:
            self.feed_failures += 1
            delay = exc.retry_after or min(self.backoff_seconds * 2 ** (self.feed_failures - 1),
                                           self.max_backoff_seconds)
            self._write_state({**self.last_view, "feed": "down", "feed_error": str(exc),
                               "feed_failures": self.feed_failures})
            self.sleep(delay)
            return {"feed": "down"}
        with SQLiteEventJournal(self.directory / "journal.db") as journal:
            view = self._replay(journal)
            if stamp is not None and self._feed_age_seconds() <= self.config.max_feed_age_seconds:
                observations = self.tape.observations()
                if self._decision_due(observations) and self._all_decided(journal, observations):
                    self.tape.add_observation(stamp, self._mid())
                    self.tape.commit()
                    self.crash("after_observation")
            try:
                if self._prepare_pending(journal, self.tape.observations()):
                    view = self._replay(journal)
            except FeedUnavailable as exc:
                self.last_view = view or self.last_view
                self._write_state({**self.last_view, "feed": "down", "feed_error": str(exc)})
                return {"feed": "down"}
        self.last_view = view or self.last_view
        status = {"feed": "up", **self.last_view}
        self._write_state(status)
        return status

    def _all_decided(self, journal: Any, observations: tuple[MarketObservation, ...]) -> bool:
        source = self._source(journal, prepared_only=True)
        return all(source.has_decision(observation) for observation in observations)

    def _write_state(self, status: dict[str, Any]) -> None:
        self.crash("before_state")
        body = {
            "provider_kind": self.config.provider_kind, "tape_rows": len(self.tape.rows),
            "tape_tip": self.tape.tip, "tape_counters": self.tape.counters,
            "observations": len(self.tape.observations()),
            "written_at": datetime.now(timezone.utc).isoformat(), **status,
        }
        _atomic_write(self.directory / STATE, json.dumps(body, sort_keys=True, indent=1, default=str).encode("utf-8"))

    def run(self, *, poll_seconds: float, max_cycles: int | None = None) -> str:
        with SessionLock(self.directory):
            cycles = 0
            stop = self.directory / STOP_REQUEST
            while True:
                if stop.exists():
                    stop.unlink()
                    self._write_state({**self.last_view, "lifecycle": "stopped"})
                    return "stopped"
                if max_cycles is not None and cycles >= max_cycles:
                    self._write_state({**self.last_view, "lifecycle": "cycle_limit"})
                    return "cycle_limit"
                self.cycle()
                cycles += 1
                self.sleep(poll_seconds)
