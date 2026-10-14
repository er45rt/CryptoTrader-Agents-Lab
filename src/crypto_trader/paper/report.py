"""Structured shadow/paper run reports (Phase 10).

The report is the observable outcome of a session: decisions, validity outcomes, risk outcomes,
queue/backpressure events, safety events, recovery events, simulated orders and fills, exposure
and simulated accounting.

It is engine-independent, deterministic and secret-free. SIMULATED ACCOUNTING IS NOT A
PERFORMANCE CLAIM AND NOT A SAFETY CERTIFICATION.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from crypto_trader.events import (
    EXECUTION_FILL_EVENT_TYPE,
    ORDER_STATE_EVENT_TYPE,
    RECOVERY_EVENT_TYPE,
    RISK_DECISION_EVENT_TYPE,
    SAFETY_EVENT_TYPE,
    STRATEGY_DECISION_EVENT_TYPE,
    TRADE_INTENT_EVENT_TYPE,
    VALIDITY_EVENT_TYPE,
)
from crypto_trader.recovery.models import OrderStateName

from .errors import PaperStateError

__all__ = ["DecisionRecord", "FillSummary", "RunReport", "OrderSummary"]


@dataclass(frozen=True)
class DecisionRecord:
    """One processed market observation and everything that happened to it."""

    index: int
    market_reference: str
    observed_at: datetime
    action: str
    confidence: str
    rationale: str
    intent_id: UUID | None = None
    validity_disposition: str | None = None
    validity_reason: str | None = None
    risk_decision: str | None = None
    risk_reason_codes: tuple[str, ...] = ()
    submission: str = "not_applicable"


@dataclass(frozen=True)
class OrderSummary:
    """Final simulated state of one order."""

    order_reference: str
    intent_id: UUID | None
    correlation_id: UUID | None
    symbol: str
    state: str
    filled_quantity: Decimal
    remaining_quantity: Decimal


@dataclass(frozen=True)
class FillSummary:
    """One simulated fill."""

    order_reference: str
    intent_id: UUID | None
    correlation_id: UUID | None
    symbol: str
    quantity: Decimal
    price: Decimal
    remaining_quantity: Decimal


@dataclass(frozen=True)
class RunReport:
    """The complete, deterministic outcome of one shadow/paper session."""

    session_id: UUID
    mode: str
    symbol: str
    strategy_id: str
    observations_processed: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    decisions: tuple[DecisionRecord, ...] = ()
    validity_counts: Mapping[str, int] = field(default_factory=dict)
    risk_counts: Mapping[str, int] = field(default_factory=dict)
    backpressure_rejections: int = 0
    stale_decisions: tuple[str, ...] = ()
    superseded_decisions: tuple[str, ...] = ()
    safety_blocked: int = 0
    safety_events: tuple[str, ...] = ()
    recovery_events: tuple[str, ...] = ()
    orders: tuple[OrderSummary, ...] = ()
    fills: tuple[FillSummary, ...] = ()
    positions: Mapping[str, Decimal] = field(default_factory=dict)
    realized_pnl_quote: Decimal = Decimal("0")
    unrealized_pnl_quote: Decimal = Decimal("0")
    equity_quote: Decimal = Decimal("0")
    engine_version: str | None = None
    book_type: str | None = None
    queue_aware: bool = False
    notes: tuple[str, ...] = ()

    # --- serialization ---

    def to_dict(self) -> dict[str, Any]:
        """A JSON-compatible mapping (Decimals/UUIDs/datetimes become strings)."""

        def convert(value: Any) -> Any:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, (UUID, datetime)):
                return str(value)
            if isinstance(value, Mapping):
                return {str(key): convert(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [convert(item) for item in value]
            return value

        return convert(asdict(self))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def write(self, path: str | Path) -> Path:
        """Write the report as one JSON document (never a secret)."""
        target = Path(path)
        target.write_text(self.to_json() + "\n", encoding="utf-8")
        return target

    def semantic_hash(self) -> str:
        """Hash of the semantic content only (no wall-clock, no process identity)."""
        payload = self.to_dict()
        for volatile in ("started_at", "ended_at"):
            payload.pop(volatile, None)
        key = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


def report_from_journal(
    events: Iterable[Any],
    *,
    session_id: UUID,
    mode: str,
    symbol: str,
    strategy_id: str,
) -> RunReport:
    """Rebuild the semantic outcome of a recorded session from its journal events.

    This is the replay check: the same recorded journal must always produce the same counts,
    dispositions, order states and fills.
    """
    decisions: list[tuple[str, str]] = []
    order_state: dict[str, str] = {}
    filled: dict[str, Decimal] = {}
    remaining: dict[str, Decimal] = {}
    order_origins: dict[str, UUID] = {}
    intent_quantities: dict[UUID, Decimal] = {}
    fills: list[tuple[datetime, str, FillSummary]] = []
    validity_counts: dict[str, int] = {}
    risk_counts: dict[str, int] = {}
    stale: list[str] = []
    superseded: list[str] = []
    safety_events: list[str] = []
    recovery_events: list[str] = []
    backpressure = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    correlation_id: UUID | None = None

    def lifecycle_rank(state: str) -> int:
        try:
            return OrderStateName(state).rank
        except (ValueError, KeyError):
            return -1

    for event in events:
        # A journal may contain multiple sessions. Never attribute another
        # session's decisions, fills, risk or time range to this replay.
        if event.correlation_id != session_id:
            continue
        occurred_at = event.occurred_at
        started_at = occurred_at if started_at is None or occurred_at < started_at else started_at
        ended_at = occurred_at if ended_at is None or occurred_at > ended_at else ended_at
        if correlation_id is None and event.correlation_id == session_id:
            correlation_id = event.correlation_id
        event_type = event.event_type
        if event_type == STRATEGY_DECISION_EVENT_TYPE:
            decisions.append((event.payload.action, event.payload.confidence))
        elif event_type == TRADE_INTENT_EVENT_TYPE:
            intent_quantities[event.event_id] = event.payload.quantity
        elif event_type == VALIDITY_EVENT_TYPE:
            disposition = event.payload.disposition
            validity_counts[disposition] = validity_counts.get(disposition, 0) + 1
            if disposition == "stale":
                stale.append(event.payload.reason_code)
            elif disposition == "superseded":
                superseded.append(event.payload.reason_code)
            elif disposition == "backpressure_rejected":
                backpressure += 1
        elif event_type == RISK_DECISION_EVENT_TYPE:
            # The semantic decision is counted once; the additive rule audit is not double-counted.
            key = "allowed" if event.payload.approved else "rejected"
            risk_counts[key] = risk_counts.get(key, 0) + 1
        elif event_type == ORDER_STATE_EVENT_TYPE:
            key = str(event.order_id)
            origin_id = getattr(event, "causation_id", None)
            if origin_id is not None:
                prior = order_origins.setdefault(key, origin_id)
                if prior != origin_id:
                    raise PaperStateError("conflicting order-to-intent provenance during replay.")
            current = order_state.get(key)
            if current is None or lifecycle_rank(event.payload.state) >= lifecycle_rank(current):
                order_state[key] = event.payload.state
        elif event_type == EXECUTION_FILL_EVENT_TYPE:
            key = str(event.order_id)
            filled[key] = filled.get(key, Decimal("0")) + event.payload.quantity
            remaining[key] = event.payload.remaining_quantity or Decimal("0")
            fills.append(
                (
                    occurred_at,
                    key,
                    FillSummary(
                        order_reference=key,
                        intent_id=None,
                        correlation_id=event.correlation_id,
                        symbol=event.symbol or symbol,
                        quantity=event.payload.quantity,
                        price=event.payload.price,
                        remaining_quantity=remaining[key],
                    ),
                )
            )
        elif event_type == SAFETY_EVENT_TYPE:
            safety_events.append(event.payload.mode)
        elif event_type == RECOVERY_EVENT_TYPE:
            recovery_events.append(event.payload.state)

    if not decisions:
        raise PaperStateError(f"no recorded decisions for session {session_id}.")

    ordered_fills = [item[2] for item in sorted(fills, key=lambda item: (item[0], item[1]))]

    def replay_remaining(reference: str, state: str) -> Decimal:
        seen_filled = filled.get(reference, Decimal("0"))
        origin = order_origins.get(reference)
        original = intent_quantities.get(origin) if origin is not None else None
        if reference in remaining:
            leaves = remaining[reference]
            if leaves < 0 or (original is not None and seen_filled + leaves != original):
                raise PaperStateError("journal fill quantity does not reconcile to order intent.")
            return leaves
        if state in {"canceled", "rejected", "expired"}:
            # Terminal failures have no active leaves, regardless of unfilled intent.
            return Decimal("0")
        if state not in {"created", "submitted", "accepted"} or original is None:
            raise PaperStateError("cannot reconstruct unfilled order quantity without intent provenance.")
        if seen_filled:
            raise PaperStateError("missing persisted partial fill for unfilled order.")
        return original

    return RunReport(
        session_id=session_id,
        mode=mode,
        symbol=symbol,
        strategy_id=strategy_id,
        observations_processed=len(decisions),
        started_at=started_at,
        ended_at=ended_at,
        validity_counts=validity_counts,
        risk_counts=risk_counts,
        backpressure_rejections=backpressure,
        stale_decisions=tuple(stale),
        superseded_decisions=tuple(superseded),
        safety_events=tuple(safety_events),
        recovery_events=tuple(recovery_events),
        orders=tuple(
            OrderSummary(
                order_reference=reference,
                intent_id=None,
                correlation_id=correlation_id,
                symbol=symbol,
                state=state,
                filled_quantity=filled.get(reference, Decimal("0")),
                remaining_quantity=replay_remaining(reference, state),
            )
            for reference, state in sorted(order_state.items())
        ),
        fills=tuple(ordered_fills),
        notes=("rebuilt from the persisted journal (replay check)",),
    )


# R-02 contracts are registered only by historical_registry(), never globally.
from typing import Literal
from uuid import uuid4, uuid5
from pydantic import Field
from crypto_trader.events.base import DomainEvent, EventPayload
from crypto_trader.events.registry import build_default_registry
from crypto_trader.persistence import JournalRevision, SQLiteEventJournal
from crypto_trader.persistence.journal import _revision_fingerprint
from .decisions import FrozenRunInputs, input_json
from .accounting import SpotCashLedger
from decimal import localcontext, ROUND_HALF_EVEN
from threading import Lock


class RunStartedPayload(EventPayload):
    policy: Literal["R-02"] = "R-02"
    run_id: UUID
    inputs_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    code_tree: str = Field(pattern="^[0-9a-f]{40}$")
    reservation_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    replay_of_run_id: UUID | None = None


class PaperRunStarted(DomainEvent):
    event_type: Literal["paper.run.started"] = "paper.run.started"
    schema_version: Literal[1] = 1
    payload: RunStartedPayload


class RunCompletedPayload(EventPayload):
    policy: Literal["R-02"] = "R-02"
    run_id: UUID
    inputs_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    started_event_id: UUID
    pre_revision_entries: tuple[tuple[UUID, str], ...]
    pre_revision_fingerprint: str
    event_order: tuple[tuple[UUID, str], ...]
    economic_state: dict[str, str]
    economic_execution: tuple[dict[str, Any], ...]
    native_cash: dict[str, Any]
    report_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    report_json: str = Field(min_length=1, max_length=8 * 1024 * 1024)


class PaperRunCompleted(DomainEvent):
    event_type: Literal["paper.run.completed"] = "paper.run.completed"
    schema_version: Literal[1] = 1
    payload: RunCompletedPayload


def historical_registry():
    registry = build_default_registry()
    registry.register_model(PaperRunStarted)
    registry.register_model(PaperRunCompleted)
    return registry


def reserved_historical_path(journal):
    path = getattr(journal, "path", None)
    if path is None or str(path) == ":memory:":
        return False
    parent = Path(path).resolve().parent
    resolved = Path(path).resolve()
    return parent.name.startswith("r02-") or (parent / "reservation.json").exists() or resolved.stat().st_nlink != 1


_LIVE_ATTEMPTS = {}
_RESERVED_RUN_IDS = set()
_RESERVATION_LOCK = Lock()


@dataclass
class HistoricalAttempt:
    journal: Any
    run_id: UUID
    reservation_sha256: str
    replay_of_run_id: UUID | None
    used: bool = False

    def consume(self, journal, run_id):
        key = str(Path(self.journal.path).resolve())
        if self.used or _LIVE_ATTEMPTS.get(key) is not self or journal is not self.journal or run_id != self.run_id:
            raise PaperStateError("R-02 reserved attempt cannot be reused or adopted.")
        self.used = True
        if journal.revision().count:
            raise PaperStateError("R-02 requires a new empty journal.")
        marker = Path(key).parent / "reservation.json"
        if hashlib.sha256(marker.read_bytes()).hexdigest() != self.reservation_sha256:
            raise PaperStateError("R-02 reservation identity is invalid.")


def reserve_historical_run(root, run_id, *, replay_of_run_id=None):
    """Reserve the directory exclusively BEFORE creating SQLite or its first event.

    Even a crash after mkdir and before reservation.json leaves a fenced namespace.
    Existing directories are NEVER adopted. This is not a distributed engine fence.
    """
    root = Path(root)
    if not root.is_absolute() or not isinstance(run_id, UUID) or run_id.version != 4 or replay_of_run_id == run_id or replay_of_run_id is not None and not isinstance(replay_of_run_id, UUID):
        raise PaperStateError("R-02 requires explicit root and independent Run ID.")
    root = root.resolve()
    repository = Path(__file__).resolve().parents[3]
    if root.is_relative_to(repository) or any(p.lower() in {"darc", "derivarchive"} for p in root.parts):
        raise PaperStateError("R-02 evidence must remain outside protected data/repository.")
    directory = root / f"r02-{run_id}"
    with _RESERVATION_LOCK:
        if run_id in _RESERVED_RUN_IDS:
            raise PaperStateError("R-02 Run ID is already reserved in this process.")
        _RESERVED_RUN_IDS.add(run_id)
    root.mkdir(parents=True, exist_ok=True)
    try:
        directory.mkdir()  # durable namespace reservation; never exist_ok
    except FileExistsError as exc:
        raise PaperStateError("R-02 path is already reserved; use a new Run ID.") from exc
    marker = input_json({"policy": "R-02", "run_id": str(run_id), "nonce": str(uuid4()),
                         "replay_of_run_id": str(replay_of_run_id) if replay_of_run_id else None})
    with (directory / "reservation.json").open("xb") as handle:
        handle.write(marker)
    journal = SQLiteEventJournal(directory / "journal.sqlite", registry=historical_registry())
    attempt = HistoricalAttempt(journal, run_id, hashlib.sha256(marker).hexdigest(), replay_of_run_id)
    _LIVE_ATTEMPTS[str(Path(journal.path).resolve())] = attempt
    return attempt


class HistoricalRecordingJournal:
    """Application trace of acknowledged writes; no storage API/transaction changes."""
    def __init__(self, journal, started):
        self.journal = journal
        self.order = [started.event_id]

    def __getattr__(self, name):
        return getattr(self.journal, name)

    def append(self, event):
        inserted = self.journal.append(event)
        if not inserted:
            raise PaperStateError("R-02 encountered an existing event; no redelivery execution.")
        self.order.append(event.event_id)
        return inserted

    def append_batch_if_revision(self, events, **kwargs):
        result = self.journal.append_batch_if_revision(events, **kwargs)
        if result.inserted != len(events):
            raise PaperStateError("R-02 encountered duplicate or uncertain stage recording.")
        self.order.extend(event.event_id for event in events)
        return result


def reconstruct_historical_economics(events, bundle, run_id):
    with localcontext() as context:
        context.prec, context.rounding = 34, ROUND_HALF_EVEN
        return _reconstruct_historical_economics(events, bundle, run_id)


def _verify_cash_admission_trace(events, bundle, run_id):
    """Replay cash-gate context against prior fills/commitments in journal order.

    Native observations are persisted witnesses, not an independent signed
    checkpoint; their consistency and boundary are rechecked here.
    """
    from .session import _stable_id
    from .simulation import CashEndEvidence
    doc = bundle.document()
    instrument, cash = doc["instrument"], doc["cash"]
    ledger = SpotCashLedger(Decimal(cash["initial_quote"]), symbol=instrument["symbol"],
        base_asset=instrument["base_currency"], quote_asset=instrument["quote_currency"],
        fee_rate=max(Decimal(cash["maker_fee"]), Decimal(cash["taker_fee"])))
    decisions, origins, risk_ids, validity_ids = {}, {}, {}, {}
    for obs, row in zip(bundle.observations(), doc["decisions"]):
        decision = bundle._decision(row["decision"])
        if decision.action == "hold":
            continue
        identity = _stable_id(str(run_id), obs.market_reference, "intent")
        decisions[identity] = (obs, decision)
        origins[_stable_id(str(identity), "intent-event")] = identity
        risk_ids[_stable_id(str(identity), "risk")] = identity
        validity_ids[_stable_id(str(identity), "validity")] = identity
    active, sequence, runtime_id = set(), 0, None
    for event in events:
        if event.event_type == VALIDITY_EVENT_TYPE and event.payload.disposition == "accepted":
            identity = validity_ids[event.event_id]
            obs, decision = decisions[identity]
            proof = json.loads(event.payload.notes)
            native = dict(proof["native_cash"])
            for key in ("initial_quote", "base_total", "base_free", "base_locked", "quote_total",
                        "quote_free", "quote_locked", "fees_quote", "position_quantity"):
                native[key] = Decimal(native[key])
            native = CashEndEvidence(**native)
            reflected = frozenset(UUID(value) for value in proof["reflected_intents"])
            ledger.validate_native_state(native, reflected)
            if reflected != frozenset(active):
                raise PaperStateError("cash gate native acknowledgments conflict with journal stages.")
            stamp = decision.decided_at
            boundary = int(stamp.timestamp()) * 1_000_000_000 + stamp.microsecond * 1000
            current_id = UUID(proof["runtime_id"])
            if (proof["cash_gate"] != "r02.cash_admission.v1" or proof["market_reference"] != obs.market_reference
                    or proof["boundary_ns"] != boundary or type(proof["read_sequence"]) is not int
                    or proof["read_sequence"] <= sequence or current_id.version != 4
                    or runtime_id is not None and runtime_id != current_id):
                raise PaperStateError("cash gate current-read identity/boundary is inconsistent.")
            sequence, runtime_id = proof["read_sequence"], current_id
            expected = ledger.admission_evidence(decision.action, decision.quantity, decision.limit_price,
                native_quote_free=native.quote_free, native_base_free=native.base_free, reflected_intents=reflected)
            if any(proof[key] != json.loads(input_json(value)) for key, value in expected.items()) or not expected["allowed"]:
                raise PaperStateError("cash gate evidence does not reconcile to current commitments.")
        elif event.event_type == RISK_DECISION_EVENT_TYPE and event.payload.approved:
            identity = risk_ids[event.event_id]
            _, decision = decisions[identity]
            ledger.reserve(identity, decision.action, decision.quantity, decision.limit_price)
        elif event.event_type == ORDER_STATE_EVENT_TYPE:
            identity = origins[event.causation_id]
            if event.payload.state in {"accepted", "partially_filled"}:
                active.add(identity)
            elif event.payload.state in {"filled", "rejected", "canceled", "expired"}:
                active.discard(identity)
                if event.payload.state != "filled":
                    ledger.release(identity)
        elif event.event_type == EXECUTION_FILL_EVENT_TYPE:
            identity = origins[event.causation_id]
            _, decision = decisions[identity]
            ledger.apply_fill(symbol=event.symbol, side=decision.action, quantity=event.payload.quantity,
                price=event.payload.price, fee=event.payload.fee, fee_asset=event.payload.fee_asset, intent_id=identity)
    if active or ledger._cash_locks:
        raise PaperStateError("cash admission trace contains unresolved native/ledger commitments.")


def _reconstruct_historical_economics(events, bundle, run_id):
    """Validate stage coverage/provenance and independently replay persisted cash fills."""
    from .session import _stable_id
    doc = bundle.document()
    instrument = doc["instrument"]
    quote = Decimal(doc["cash"]["initial_quote"])
    ledger = SpotCashLedger(quote, symbol=instrument["symbol"],
                           base_asset=instrument["base_currency"], quote_asset=instrument["quote_currency"])
    by_id = {event.event_id: event for event in events}
    if len(by_id) != len(events) or any(event.correlation_id != run_id for event in events):
        raise PaperStateError("R-02 event identity/scope is inconsistent.")
    stages = set()
    approved = {}
    execution = []
    index_by_id = {event.event_id: i for i, event in enumerate(events)}
    previous_decision_position = -1
    for obs, row in zip(bundle.observations(), doc["decisions"]):
        decision = bundle._decision(row["decision"])
        def stage(identity, event_type):
            event = by_id.get(identity)
            if event is None or event.event_type != event_type or identity in stages:
                raise PaperStateError("R-02 has an incomplete or duplicate decision stage.")
            stages.add(identity)
            producer = "application.risk" if event_type == "risk.evaluation.recorded" else doc["session"]["producer"]
            if event.symbol != instrument["symbol"] or event.producer != producer:
                raise PaperStateError("R-02 decision stage provenance mismatch.")
            return event
        decision_id = _stable_id(str(run_id), obs.market_reference, "decision")
        recorded = stage(decision_id, STRATEGY_DECISION_EVENT_TYPE)
        if index_by_id[decision_id] <= previous_decision_position:
            raise PaperStateError("R-02 frozen decision order is inconsistent.")
        previous_decision_position = index_by_id[decision_id]
        if (recorded.payload.action, recorded.payload.confidence, recorded.payload.rationale, recorded.occurred_at) != (decision.action, decision.confidence, decision.rationale, obs.observed_at):
            raise PaperStateError("R-02 journal decision disagrees with frozen input.")
        if decision.action == "hold":
            continue
        intent_id = _stable_id(str(run_id), obs.market_reference, "intent")
        intent_event_id = _stable_id(str(intent_id), "intent-event")
        intent = stage(intent_event_id, TRADE_INTENT_EVENT_TYPE)
        if intent.causation_id != decision_id or (intent.payload.side, intent.payload.quantity, intent.payload.order_type, intent.payload.limit_price) != (decision.action, decision.quantity, decision.order_type, decision.limit_price):
            raise PaperStateError("R-02 intent disagrees with frozen decision.")
        validity = stage(_stable_id(str(intent_id), "validity"), VALIDITY_EVENT_TYPE)
        if not index_by_id[decision_id] < index_by_id[intent_event_id] < index_by_id[validity.event_id]:
            raise PaperStateError("R-02 intent/validity stage order is invalid.")
        if validity.payload.intent_id != intent_id:
            raise PaperStateError("R-02 validity intent mismatch.")
        if validity.payload.disposition != "accepted":
            continue
        risk = stage(_stable_id(str(intent_id), "risk"), RISK_DECISION_EVENT_TYPE)
        audit = stage(_stable_id(str(intent_id), "risk-evaluation"), "risk.evaluation.recorded")
        if not index_by_id[validity.event_id] < index_by_id[risk.event_id] or index_by_id[audit.event_id] != index_by_id[risk.event_id] + 1:
            raise PaperStateError("R-02 atomic risk stage order is invalid.")
        if audit.payload.intent_id != intent_id or audit.schema_version != 2 or risk.payload.approved != (audit.payload.decision == "allowed"):
            raise PaperStateError("R-02 risk pair is inconsistent.")
        if risk.payload.approved:
            if risk.payload.approved_quantity != decision.quantity or audit.payload.quantity != decision.quantity or audit.payload.side != decision.action:
                raise PaperStateError("R-02 approved economics disagree with intent.")
            approved[intent_event_id] = (row["index"], decision, audit.event_id)
    order_origins = {}
    latest = {}
    filled = {}
    last_time = {}
    order_by_origin = {}
    for event in events:
        if event.event_type in {"paper.run.started", "paper.run.completed"}:
            continue
        if event.event_id in stages:
            continue
        if event.event_type not in {ORDER_STATE_EVENT_TYPE, EXECUTION_FILL_EVENT_TYPE} or event.causation_id not in approved or event.order_id is None or event.symbol != instrument["symbol"]:
            raise PaperStateError("R-02 contains an orphan or unexpected event.")
        origin = event.causation_id
        if event.producer != "infrastructure.nautilus":
            raise PaperStateError("R-02 execution source is not the native adapter.")
        prior = order_origins.setdefault(event.order_id, origin)
        originating_order = order_by_origin.setdefault(origin, event.order_id)
        if prior != origin or originating_order != event.order_id or index_by_id[approved[origin][2]] >= index_by_id[event.event_id]:
            raise PaperStateError("R-02 execution provenance/order is inconsistent.")
        index, decision, _ = approved[origin]
        if event.occurred_at <= decision.decided_at or event.occurred_at < last_time.get(event.order_id, event.occurred_at):
            raise PaperStateError("R-02 execution time order is invalid.")
        last_time[event.order_id] = event.occurred_at
        if event.event_type == ORDER_STATE_EVENT_TYPE:
            if latest.get(event.order_id) in {"filled", "canceled", "expired", "rejected"}:
                raise PaperStateError("R-02 execution advanced after terminal state.")
            latest[event.order_id] = event.payload.state
        else:
            payload = event.payload
            if latest.get(event.order_id) not in {"filled", "partially_filled"}:
                raise PaperStateError("R-02 fill has no matching native lifecycle stage.")
            if decision.action == "buy" and payload.price > decision.limit_price or decision.action == "sell" and payload.price < decision.limit_price:
                raise PaperStateError("R-02 fill violates its limit price.")
            total = filled.get(event.order_id, Decimal("0")) + payload.quantity
            if payload.remaining_quantity is None or total + payload.remaining_quantity != decision.quantity:
                raise PaperStateError("R-02 fill quantities do not reconcile.")
            ledger.apply_fill(symbol=event.symbol, side=decision.action, quantity=payload.quantity,
                              price=payload.price, fee=payload.fee, fee_asset=payload.fee_asset)
            filled[event.order_id] = total
            execution.append({"decision_index": index, "side": decision.action,
                "quantity": str(payload.quantity), "price": str(payload.price), "fee": str(payload.fee),
                "fee_asset": payload.fee_asset, "remaining_quantity": str(payload.remaining_quantity),
                "occurred_at": event.occurred_at.isoformat()})
    if set(order_origins.values()) != set(approved) or any(state not in {"filled", "canceled", "expired", "rejected"} for state in latest.values()):
        raise PaperStateError("R-02 has unresolved or unsubmitted orders.")
    for order, state in latest.items():
        if state == "filled" and filled.get(order, Decimal("0")) != approved[order_origins[order]][1].quantity:
            raise PaperStateError("R-02 terminal order is missing fills.")
    _verify_cash_admission_trace(events, bundle, run_id)
    return ledger.economic_state(bundle.observations()[-1].price), tuple(execution)


def validate_native_cash(native, state, initial_quote):
    if set(native) != {"initial_quote", "base_total", "base_free", "base_locked", "quote_total", "quote_free", "quote_locked", "fees_quote", "position_quantity", "unsettled", "account_type", "allow_borrowing"}:
        raise PaperStateError("R-02 native evidence fields are incomplete.")
    if native["account_type"] != "CASH" or native["allow_borrowing"] is not False or native["unsettled"] is not False:
        raise PaperStateError("R-02 native orders/account are unresolved.")
    if Decimal(str(native["initial_quote"])) != initial_quote or Decimal(str(native["position_quantity"])) != state["base_total"]:
        raise PaperStateError("R-02 native initial balance/inventory mismatch.")
    for key in ("base_total", "base_free", "base_locked", "quote_total", "quote_free", "quote_locked", "fees_quote"):
        number = Decimal(str(native[key]))
        if not number.is_finite() or number != state[key]:
            raise PaperStateError("R-02 native cash accounting mismatch.")


def _verify_historical_report(report, events, bundle, run_id, state):
    """Cross-check embedded report references, not just its self-contained hash."""
    from .session import _stable_id
    doc = bundle.document()
    symbol = doc["instrument"]["symbol"]
    by_id = {event.event_id: event for event in events}
    records, validity_counts, risk_counts, safety_modes = [], {}, {}, []
    for index, (obs, row) in enumerate(zip(bundle.observations(), doc["decisions"])):
        decision = bundle._decision(row["decision"])
        fields = dict(index=index, market_reference=obs.market_reference, observed_at=obs.observed_at,
                      action=decision.action, confidence=decision.confidence, rationale=decision.rationale)
        if decision.action == "hold":
            records.append(DecisionRecord(**fields, submission="hold"))
            continue
        intent_id = _stable_id(str(run_id), obs.market_reference, "intent")
        validity = by_id[_stable_id(str(intent_id), "validity")].payload
        disposition = validity.disposition
        validity_counts[disposition] = validity_counts.get(disposition, 0) + 1
        fields.update(intent_id=intent_id, validity_disposition=disposition, validity_reason=validity.reason_code)
        if disposition != "accepted":
            records.append(DecisionRecord(**fields, submission="stopped_by_validity"))
            continue
        audit = by_id[_stable_id(str(intent_id), "risk-evaluation")].payload
        risk_counts[audit.decision] = risk_counts.get(audit.decision, 0) + 1
        safety_modes.append(doc["safety_mode"])
        blocked = bool(set(audit.reason_codes) & {"trading_halted", "reduce_only_violation"})
        records.append(DecisionRecord(**fields, risk_decision=audit.decision,
            risk_reason_codes=tuple(audit.reason_codes), submission="submitted_to_simulator"
            if audit.decision == "allowed" else "blocked_by_safety" if blocked else "stopped_by_risk"))
    expected = RunReport(run_id, "paper", symbol, doc["session"]["strategy_id"],
        decisions=tuple(records)).to_dict()["decisions"]
    observations = bundle.observations()
    if (report["decisions"] != expected or report["strategy_id"] != doc["session"]["strategy_id"]
            or report["validity_counts"] != validity_counts or report["risk_counts"] != risk_counts
            or report["started_at"] != str(observations[0].observed_at)
            or report["ended_at"] != str(observations[-1].observed_at)
            or report["engine_version"] != doc["provenance"]["engine_version"]
            or report["book_type"] != "L2_MBP" or report["queue_aware"] is not True
            or report["safety_events"] != safety_modes):
        raise PaperStateError("R-02 report decision/settings references mismatch.")
    for key, disposition in (("stale_decisions", "stale"), ("superseded_decisions", "superseded")):
        if report[key] != [record.market_reference for record in records if record.validity_disposition == disposition]:
            raise PaperStateError("R-02 report validity references mismatch.")
    if report["backpressure_rejections"] != validity_counts.get("backpressure_rejected", 0) or report["safety_blocked"] != sum(r.submission == "blocked_by_safety" for r in records):
        raise PaperStateError("R-02 report pipeline counts mismatch.")
    fills = [event for event in events if event.event_type == EXECUTION_FILL_EVENT_TYPE]
    positions = {symbol: state["base_total"]} if fills else {}
    if {key: Decimal(value) for key, value in report["positions"].items()} != positions:
        raise PaperStateError("R-02 report positions mismatch.")
    # This namespace is the existing persisted native reference -> domain UUID
    # mapping. No native engine import or new identity contract is introduced.
    namespace = UUID("6f2c1e2a-9a1e-4c7f-9a6c-2f1b6d4e5a10")
    states = {event.order_id: event for event in events if event.event_type == ORDER_STATE_EVENT_TYPE}
    reported_orders = {}
    for order in report["orders"]:
        identity = uuid5(namespace, order["order_reference"])
        if identity in reported_orders or identity not in states:
            raise PaperStateError("R-02 report order reference is missing or duplicated.")
        event = states[identity]
        origin = by_id[event.causation_id]
        intent_id = next(r.intent_id for r in records
            if r.intent_id is not None and _stable_id(str(r.intent_id), "intent-event") == origin.event_id)
        quantity = sum((fill.payload.quantity for fill in fills if fill.order_id == identity), Decimal("0"))
        leaves = Decimal(order["remaining_quantity"])
        if (order["intent_id"] != str(intent_id) or order["correlation_id"] != str(run_id)
                or order["symbol"] != symbol or order["state"] != event.payload.state
                or Decimal(order["filled_quantity"]) != quantity or not leaves.is_finite()
                or leaves < 0 or leaves > origin.payload.quantity - quantity
                or event.payload.state == "filled" and leaves != 0):
            raise PaperStateError("R-02 report order economics/state mismatch.")
        reported_orders[identity] = order
    if set(reported_orders) != set(states) or len(report["fills"]) != len(fills):
        raise PaperStateError("R-02 report execution coverage mismatch.")
    for recorded, event in zip(report["fills"], fills):
        order = reported_orders[event.order_id]
        if any(recorded[key] != order[key] for key in ("order_reference", "intent_id", "correlation_id", "symbol")) or any(
            Decimal(recorded[key]) != getattr(event.payload, key) for key in ("quantity", "price", "remaining_quantity")):
            raise PaperStateError("R-02 report fill references/economics mismatch.")


def verify_historical_completion(journal, bundle):
    before = journal.revision()
    events = tuple(journal.read_range())
    completions = [e for e in events if e.event_type == "paper.run.completed"]
    starts = [e for e in events if e.event_type == "paper.run.started"]
    if len(completions) != 1 or len(starts) != 1 or not isinstance(completions[0], PaperRunCompleted) or not isinstance(starts[0], PaperRunStarted):
        raise PaperStateError("R-02 completion evidence is missing or invalid.")
    seal, start = completions[0], starts[0]
    payload = seal.payload
    run_id = payload.run_id
    if start.symbol != bundle.document()["instrument"]["symbol"] or seal.symbol != start.symbol:
        raise PaperStateError("R-02 run boundary symbol mismatch.")
    if start.event_id != uuid5(run_id, "paper.run.started") or seal.event_id != uuid5(run_id, "paper.run.completed") or start.producer != bundle.document()["session"]["producer"] or seal.producer != start.producer:
        raise PaperStateError("R-02 run contract provenance mismatch.")
    if start.payload.run_id != run_id or start.correlation_id != run_id or seal.correlation_id != run_id or start.payload.inputs_sha256 != bundle.sha256 or payload.inputs_sha256 != bundle.sha256 or payload.started_event_id != start.event_id or seal.causation_id != start.event_id or start.payload.code_tree != bundle.document()["provenance"]["code_tree"]:
        raise PaperStateError("R-02 completion identity/input mismatch.")
    pre = JournalRevision(payload.pre_revision_entries, payload.pre_revision_fingerprint)
    actual = tuple(entry for entry in before.entries if entry[0] != seal.event_id)
    if actual != pre.entries or tuple(sorted(payload.event_order, key=lambda row: str(row[0]))) != pre.entries or not payload.event_order or payload.event_order[0][0] != start.event_id:
        raise PaperStateError("R-02 completion revision/ordered event coverage mismatch.")
    by_id = {event.event_id: event for event in events}
    if set(by_id) != set(before.event_ids):
        raise PaperStateError("R-02 decoded journal coverage mismatch.")
    ordered = tuple(by_id[identity] for identity, _ in payload.event_order)
    if any(event.occurred_at <= start.occurred_at for event in ordered[1:]) or seal.occurred_at <= max(event.occurred_at for event in ordered):
        raise PaperStateError("R-02 run boundary time evidence is invalid.")
    state, execution = reconstruct_historical_economics(ordered, bundle, run_id)
    expected = {key: str(value) for key, value in state.items()}
    if payload.economic_state != expected or payload.economic_execution != execution:
        raise PaperStateError("R-02 completion economic evidence mismatch.")
    validate_native_cash(payload.native_cash, state, Decimal(bundle.document()["cash"]["initial_quote"]))
    if hashlib.sha256(payload.report_json.encode()).hexdigest() != payload.report_sha256:
        raise PaperStateError("R-02 report fingerprint mismatch.")
    report = json.loads(payload.report_json)
    if report["session_id"] != str(run_id) or report["symbol"] != bundle.document()["instrument"]["symbol"] or report["mode"] != "paper" or report["observations_processed"] != len(bundle.observations()):
        raise PaperStateError("R-02 report identity/count mismatch.")
    for key in ("realized_pnl_quote", "unrealized_pnl_quote", "equity_quote"):
        if Decimal(report[key]) != state[key]:
            raise PaperStateError("R-02 report economic mismatch.")
    _verify_historical_report(report, ordered, bundle, run_id, state)
    if journal.revision() != before:
        raise PaperStateError("R-02 journal changed during completion verification.")
    return seal


def read_historical_completion(path, spec):
    """Read-only diagnostics of evidence consistency, not an external attestation.

    Approval pins are supplied independently by the caller. Native cash is a
    persisted observation, not a separately signed engine checkpoint. A whole
    artifact set can be moved if its reservation identity remains verifiable;
    neither this schema nor its hash proves the original absolute root path.
    Only closed, stable artifacts without SQLite sidecars are accepted. Immutable
    read mode prevents SQLite from creating/updating WAL shared-memory evidence;
    an extant WAL is refused, never ignored, replayed or checkpointed here.
    """
    import sqlite3
    from contextlib import closing
    bundle = FrozenRunInputs.approved(spec)
    registry = historical_registry()
    path = Path(path).resolve()
    try:
        def stable_digest():
            if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise PaperStateError("R-02 diagnostics require closed evidence without SQLite sidecars.")
            with path.open("rb") as handle:
                return hashlib.file_digest(handle, "sha256").hexdigest()
        evidence_digest = stable_digest()
        marker = path.parent / "reservation.json"
        reservation_bytes = marker.read_bytes()
        if len(reservation_bytes) > 4096 or marker.is_symlink() or not marker.is_file():
            raise PaperStateError("R-02 reservation file is invalid.")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")  # one read snapshot; no schema/settings changes
            version = connection.execute("SELECT value FROM journal_meta WHERE key='schema_version'").fetchall()
            if len(version) != 1 or version[0][0] != "1":
                raise PaperStateError("R-02 journal schema evidence is invalid.")
            rows = connection.execute("SELECT * FROM events ORDER BY rowid").fetchall()
            events = []
            entries = []
            for row in rows:
                identity, raw, stored = row["event_id"], row["payload_json"], row["payload_sha256"]
                digest = hashlib.sha256(raw.encode()).hexdigest()
                event = registry.decode(raw)
                if digest != stored or str(event.event_id) != identity:
                    raise PaperStateError("R-02 journal checksum/identity corruption.")
                fields = {name: getattr(event, name) for name in ("event_type", "schema_version", "producer", "strategy_id", "symbol", "sequence_number")}
                fields.update({name: None if getattr(event, name) is None else str(getattr(event, name))
                               for name in ("correlation_id", "causation_id", "order_id")})
                fields.update(occurred_at_ns=int(event.occurred_at.timestamp() * 1_000_000_000),
                              occurred_at_iso=event.occurred_at.astimezone(timezone.utc).isoformat())
                if any(row[name] != value for name, value in fields.items()):
                    raise PaperStateError("R-02 journal indexed metadata conflicts with payload.")
                events.append(event)
                entries.append((event.event_id, digest))
        entries = tuple(sorted(entries, key=lambda row: str(row[0])))
        revision = JournalRevision(entries, _revision_fingerprint(entries))
        class ReadView:
            def revision(self): return revision
            def read_range(self): return events
        seal = verify_historical_completion(ReadView(), bundle)
        if tuple(event.event_id for event in events) != tuple(identity for identity, _ in seal.payload.event_order) + (seal.event_id,):
            raise PaperStateError("R-02 physical journal order conflicts with completion evidence.")
        start = events[0]
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise PaperStateError("R-02 reservation has duplicate identity fields.")
                result[key] = value
            return result
        reservation = json.loads(reservation_bytes, object_pairs_hook=unique)
        if not isinstance(reservation, dict) or set(reservation) != {"policy", "run_id", "nonce", "replay_of_run_id"} or input_json(reservation) != reservation_bytes:
            raise PaperStateError("R-02 reservation structure is invalid.")
        run_id, nonce = UUID(reservation["run_id"]), UUID(reservation["nonce"])
        replay = reservation["replay_of_run_id"]
        if (reservation["policy"] != "R-02" or run_id.version != 4 or nonce.version != 4
                or str(run_id) != reservation["run_id"] or str(nonce) != reservation["nonce"]
                or run_id != start.payload.run_id or path.parent.name != f"r02-{run_id}"
                or path.name != "journal.sqlite"
                or replay != (str(start.payload.replay_of_run_id) if start.payload.replay_of_run_id else None)
                or replay == str(run_id)
                or hashlib.sha256(reservation_bytes).hexdigest() != start.payload.reservation_sha256
                or marker.read_bytes() != reservation_bytes or stable_digest() != evidence_digest):
            raise PaperStateError("R-02 reservation identity/path/fingerprint mismatch.")
        return seal
    except PaperStateError:
        raise
    except Exception as exc:
        raise PaperStateError("R-02 journal is corrupt, incomplete or unverifiable.") from exc
