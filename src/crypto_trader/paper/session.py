"""Shadow / paper trading session (Phase 10).

Both modes run the real decision pipeline - public market observation -> injected AI/strategy
decision -> decision validity -> deterministic hard risk -> safety gate - and journal every stage
through the existing Phase 3 contracts. They differ only in what happens after approval:

* SHADOW records what *would* have been submitted and places nothing. With
  ``config.shadow_submits`` the would-be orders go through the simulator too (still no live venue).
* PAPER submits the approved requests to the injected simulated execution substrate and journals
  the resulting lifecycle/fill events.

There is NO authenticated exchange trading anywhere on this path: no credential, no signature,
no order endpoint, no live venue client.

Determinism: every event id and intent id is derived from the session id and the market reference,
so re-running the same session over the same frozen feed produces identical semantics and the
journal stays idempotent. ``resume_index`` lets a restarted process continue exactly where the
recorded journal stops without duplicating effects.

Nautilus Paper follows a chronological, persistent-engine simulation; the scripted offline
test double retains its legacy batch behavior and is not causal backtest evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from crypto_trader.events import (
    STRATEGY_DECISION_EVENT_TYPE,
    TRADE_INTENT_EVENT_TYPE,
    VALIDITY_EVENT_TYPE,
    RISK_DECISION_EVENT_TYPE,
    EXECUTION_FILL_EVENT_TYPE,
    ORDER_STATE_EVENT_TYPE,
    StrategyDecisionPayload,
    StrategyDecisionRecorded,
    TradeIntentCreated,
    TradeIntentPayload,
)
from crypto_trader.execution import (
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionSide,
)
from crypto_trader.persistence import EventJournal
from crypto_trader.risk import (
    HardRiskEngine,
    PendingIntent,
    PendingIntentRegistry,
    RiskDecision,
    RiskLimits,
    RiskRequest,
    RiskStateSnapshot,
    Side,
    record_risk_decision,
)
from crypto_trader.safety import SafetyController, risk_limits_for_safety
from crypto_trader.validity import (
    DecisionValidityGate,
    MarketObservation as ValidityMarketObservation,
    ReplayMarketStateProvider,
    ValidityRequest,
    record_validity_decision,
)

from .accounting import PositionLedger
from .config import SessionConfig
from .decisions import DecisionSource, StrategyDecision
from .errors import PaperStateError
from .feed import MarketDataFeed, MarketObservation
from .modes import TradingMode
from .report import DecisionRecord, FillSummary, OrderSummary, RunReport
from .simulation import (
    ExecutionSimulator, SimulationOutcome, TemporalExecutionSimulator, scripted_order_uuid,
)

__all__ = ["TradingSession"]

_SESSION_NAMESPACE = UUID("7d4f2b91-3c6a-4e58-9b2f-1a9d5c8e7f30")
_ZERO = Decimal("0")
# Existing Phase-3 native order-reference mapping; keep independent of the engine.
_ORDER_ID_NAMESPACE = UUID("6f2c1e2a-9a1e-4c7f-9a6c-2f1b6d4e5a10")


def _stable_id(*parts: str) -> UUID:
    return uuid5(_SESSION_NAMESPACE, "|".join(parts))


@dataclass
class TradingSession:
    """A deterministic shadow/paper session over a public market-data feed."""

    config: SessionConfig
    journal: EventJournal
    feed: MarketDataFeed
    decisions: DecisionSource
    validity_gate: DecisionValidityGate
    limits: RiskLimits
    safety: SafetyController
    simulator: ExecutionSimulator | None = None
    recovery_run: Any | None = None
    # Reserve approved exposure until the deferred simulation pass resolves it.
    _reservations: dict[UUID, tuple[Side, Decimal, Decimal]] = field(default_factory=dict)
    _pending: PendingIntentRegistry = field(default_factory=PendingIntentRegistry)
    _risk_cache: tuple[tuple[str, bool], HardRiskEngine] | None = None
    _peak_equity_quote: Decimal | None = None
    _temporal_decided_ns: dict[UUID, int] = field(default_factory=dict)
    _temporal_terminal_intents: set[UUID] = field(default_factory=set)
    _temporal_seen_event_ids: set[UUID] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.config.places_simulated_orders and self.simulator is None:
            raise PaperStateError("simulated order submission requires an execution simulator.")

    # --- resume support ---

    def resume_index(self) -> int:
        """Return only a contiguous prefix with complete persisted decision stages.

        A crash may happen *after* strategy.decision.recorded but *before*
        validity/risk journaling. Counting decisions alone would skip work
        after restart. Resolve stable IDs by market reference, not by journal
        iteration order (journals may sort equal-time events by UUID).
        """
        entries = tuple(self.feed.observations())
        events = tuple(self.journal.read_by_correlation(self.config.session_id))
        by_id = {event.event_id: event for event in events}
        known_decisions: set[UUID] = set()
        processed = 0
        saw_gap = False

        def require_stage(event_id: UUID, event_type: str) -> Any:
            candidate = by_id.get(event_id)
            if candidate is None or candidate.event_type != event_type:
                raise PaperStateError("incomplete journal stage: refusing to advance replay cursor.")
            return candidate

        for entry in entries:
            decision_id = _stable_id(str(self.config.session_id), entry.market_reference, "decision")
            known_decisions.add(decision_id)
            event = by_id.get(decision_id)
            if event is None:
                saw_gap = True
                continue
            if saw_gap:
                raise PaperStateError("incomplete journal: non-contiguous decision prefix.")
            if event.event_type != STRATEGY_DECISION_EVENT_TYPE or event.producer != self.config.producer:
                raise PaperStateError("incomplete or mismatched journal decision identity.")
            if event.payload.action != "hold":
                intent_id = _stable_id(str(self.config.session_id), entry.market_reference, "intent")
                require_stage(_stable_id(str(intent_id), "intent-event"), TRADE_INTENT_EVENT_TYPE)
                validity = require_stage(_stable_id(str(intent_id), "validity"), VALIDITY_EVENT_TYPE)
                if validity.payload.disposition == "accepted":
                    require_stage(_stable_id(str(intent_id), "risk"), RISK_DECISION_EVENT_TYPE)
                    require_stage(_stable_id(str(intent_id), "risk-evaluation"), "risk.evaluation.recorded")
            processed += 1

        for event in events:
            if (event.event_type == STRATEGY_DECISION_EVENT_TYPE
                    and event.producer == self.config.producer
                    and event.event_id not in known_decisions):
                raise PaperStateError("incomplete journal: decision does not match the frozen feed.")
        return processed

    # --- run ---

    def run(self, *, start_index: int = 0) -> RunReport:
        """Process the feed from ``start_index`` and return the session report."""
        entries = list(self.feed.observations())
        if not isinstance(start_index, int) or not 0 <= start_index <= len(entries):
            raise PaperStateError("start_index must be within the feed.")
        if any(entry.symbol != self.config.symbol for entry in entries):
            raise PaperStateError("market observation symbol must match the session symbol.")

        ledger = PositionLedger(Decimal(self.config.starting_equity_quote))
        self._reservations = {}
        self._pending = PendingIntentRegistry()
        self._risk_cache = None
        self._peak_equity_quote = ledger.starting_equity_quote
        self._temporal_decided_ns = {}
        self._temporal_terminal_intents = set()
        self._temporal_seen_event_ids = set()
        prior_intents = {
            _stable_id(str(self.config.session_id), entry.market_reference, "intent")
            for entry in entries[:start_index]
        }
        # Retain prior approvals on resume. The batch decision pass has no synchronous fill
        # feedback, so reservations are conservatively retained until a separate reconciliation.
        for event in self.journal.read_by_correlation(self.config.session_id):
            if event.event_type != "risk.evaluation.recorded":
                continue
            payload = event.payload
            if payload.intent_id in prior_intents and payload.decision == "allowed":
                self._reservations[payload.intent_id] = (
                    Side(payload.side), payload.quantity, payload.price
                )
                self._pending.register(PendingIntent(
                    intent_id=payload.intent_id, symbol=payload.symbol,
                    strategy_id=self.config.strategy_id, side=Side(payload.side),
                    quantity=payload.quantity,
                    notional=payload.quantity * payload.price,
                ))
        # The market-state provider walks the same frozen sequence, so validity never sees a
        # future observation when it evaluates an intent.
        market_state = ReplayMarketStateProvider(
            [self._validity_observation(entry) for entry in entries]
        )
        for _ in range(start_index):
            market_state.advance(self.config.symbol)

        records: list[DecisionRecord] = []
        requests: list[ExecutionRequest] = []
        validity_counts: dict[str, int] = {}
        risk_counts: dict[str, int] = {}
        stale: list[str] = []
        superseded: list[str] = []
        backpressure = 0
        safety_blocked = 0
        safety_events: list[str] = []

        # The approved paper bridge uses one persistent temporal engine. The legacy scripted
        # double intentionally remains a synthetic test fixture, not backtest evidence.
        temporal = bool(
            self.config.places_simulated_orders
            and isinstance(self.simulator, TemporalExecutionSimulator)
        )
        if temporal:
            # Validate the complete frozen timeline BEFORE starting the native engine
            # or writing journal events; equal event times are not causally ordered.
            for previous, current in zip(entries, entries[1:]):
                if current.observed_at <= previous.observed_at:
                    raise PaperStateError(
                        "temporal market observations must have strictly increasing timestamps."
                    )
        if temporal and start_index:
            raise PaperStateError(
                "temporal execution resume requires verified journal, risk peak and engine state."
            )
        simulator = self.simulator
        if temporal:
            simulator.start_temporal()

        seen_observations = 0
        seen_events = 0

        def capture(current: SimulationOutcome, *, boundary_ns: int) -> None:
            nonlocal seen_observations, seen_events
            if (
                len(current.observations) < seen_observations
                or len(current.phase3_events) < seen_events
            ):
                raise PaperStateError("temporal simulation output moved backwards.")
            delta = SimulationOutcome(
                observations=current.observations[seen_observations:],
                phase3_events=current.phase3_events[seen_events:],
            )
            closed, event_ids = self._validate_temporal_delta(
                delta, requests, boundary_ns=boundary_ns
            )
            self._journal_simulation(delta)
            self._apply_fills(delta, requests, ledger)
            self._reconcile_temporal_reservations(delta)
            self._temporal_terminal_intents = closed
            self._temporal_seen_event_ids.update(event_ids)
            seen_observations = len(current.observations)
            seen_events = len(current.phase3_events)

        temporal_finished = False
        try:
            for index in range(start_index, len(entries)):
                observation = entries[index]
                if temporal:
                    observed_ns = self._timestamp_ns(observation.observed_at)
                    capture(simulator.advance_until(observed_ns), boundary_ns=observed_ns)
                decision = self.decisions.decide(observation)
                now = decision.decided_at or observation.observed_at
                if now.tzinfo is None or now.utcoffset() is None:
                    raise PaperStateError("a decision timestamp must be timezone-aware.")
                if temporal:
                    if now < observation.observed_at:
                        raise PaperStateError("decision completion predates the market observation.")
                    if index + 1 < len(entries) and now > entries[index + 1].observed_at:
                        raise PaperStateError(
                            "temporal decision overlaps the next market observation."
                        )
                    capture(simulator.advance_until(self._timestamp_ns(now)), boundary_ns=self._timestamp_ns(now))
                self._update_peak(ledger, observation.price)
                self._record_decision(observation, decision)
                record = self._process(
                    index=index,
                    observation=observation,
                    decision=decision,
                    now=now,
                    market_state=market_state,
                    ledger=ledger,
                    requests=requests,
                    safety_events=safety_events,
                )
                if temporal and record.submission == "submitted_to_simulator":
                    request = requests[-1]
                    stamp_ns = self._timestamp_ns(now)
                    self._temporal_decided_ns[request.intent_id] = stamp_ns
                    simulator.submit_temporal(request, decided_ns=stamp_ns)
                market_state.advance(self.config.symbol)
                records.append(record)
                if record.validity_disposition is not None:
                    key = record.validity_disposition
                    validity_counts[key] = validity_counts.get(key, 0) + 1
                    if key == "stale":
                        stale.append(observation.market_reference)
                    elif key == "superseded":
                        superseded.append(observation.market_reference)
                    elif key == "backpressure_rejected":
                        backpressure += 1
                if record.risk_decision is not None:
                    key = record.risk_decision
                    risk_counts[key] = risk_counts.get(key, 0) + 1
                if record.submission == "blocked_by_safety":
                    safety_blocked += 1

            outcome = SimulationOutcome()
            placements = requests if self.config.places_simulated_orders else []
            if temporal:
                outcome = simulator.finish_temporal()
                temporal_finished = True
                horizon_method = getattr(simulator, "market_horizon_ns", None)
                horizon_ns = (
                    horizon_method() if callable(horizon_method) else
                    (self._timestamp_ns(entries[-1].observed_at) if entries else 0)
                )
                capture(outcome, boundary_ns=horizon_ns)
            elif placements and simulator is not None:
                outcome = simulator.simulate(placements)
                self._journal_simulation(outcome)
                self._apply_fills(outcome, requests, ledger)
        finally:
            if temporal and not temporal_finished:
                simulator.abort_temporal()

        marks = {self.config.symbol: entries[-1].price} if entries else {}
        recovery_events: tuple[str, ...] = ()
        if self.recovery_run is not None:
            recovery_events = tuple(
                sorted({item.value for item in self.recovery_run.report.outcomes.values()})
            )

        notes = (
            "SIMULATED ACCOUNTING: not a performance claim and not a safety certification.",
            "L2 MBP cannot reveal hidden/iceberg liquidity; true venue queue rank is unknown.",
            "Dynamic latency customization is PARTIAL in 1.231.0 (static per-command only).",
            (
                "Temporal paper simulation applies fills before subsequent risk decisions."
                if temporal else
                "The legacy scripted test double applies fills after the decision pass."
            ),
        )
        return RunReport(
            session_id=self.config.session_id,
            mode=self.config.mode.value,
            symbol=self.config.symbol,
            strategy_id=self.config.strategy_id,
            observations_processed=len(records),
            started_at=entries[start_index].observed_at if records else None,
            ended_at=entries[-1].observed_at if records else None,
            decisions=tuple(records),
            validity_counts=validity_counts,
            risk_counts=risk_counts,
            backpressure_rejections=backpressure,
            stale_decisions=tuple(stale),
            superseded_decisions=tuple(superseded),
            safety_blocked=safety_blocked,
            safety_events=tuple(safety_events),
            recovery_events=recovery_events,
            orders=self._order_summaries(outcome),
            fills=self._fill_summaries(outcome),
            positions={symbol: state.quantity for symbol, state in ledger.positions().items()},
            realized_pnl_quote=ledger.realized_pnl_quote(),
            unrealized_pnl_quote=ledger.unrealized_pnl_quote(marks),
            equity_quote=ledger.equity_quote(marks),
            engine_version=outcome.engine_version,
            book_type=outcome.book_type,
            queue_aware=outcome.queue_aware,
            notes=notes,
        )

    # --- pipeline steps ---

    def _process(
        self,
        *,
        index: int,
        observation: MarketObservation,
        decision: StrategyDecision,
        now: datetime,
        market_state: ReplayMarketStateProvider,
        ledger: PositionLedger,
        requests: list[ExecutionRequest],
        safety_events: list[str],
    ) -> DecisionRecord:
        base: dict[str, Any] = {
            "index": index,
            "market_reference": observation.market_reference,
            "observed_at": observation.observed_at,
            "action": decision.action,
            "confidence": decision.confidence,
            "rationale": decision.rationale,
        }
        if decision.action == "hold":
            return DecisionRecord(**base, submission="hold")

        intent_id = _stable_id(str(self.config.session_id), observation.market_reference, "intent")
        self._record_intent(observation, decision, intent_id)

        validity = self.validity_gate.evaluate(
            ValidityRequest(
                correlation_id=self.config.session_id,
                symbol=self.config.symbol,
                decision_started_at=observation.observed_at,
                decision_completed_at=now,
                market_event_ts=observation.observed_at,
                reference_price=decision.limit_price or observation.price,
                market_reference=observation.market_reference,
                intent_id=intent_id,
                decision_scope=self.config.strategy_id,
            ),
            market_state=market_state,
            now=now,
            capacity_available=True,
        )
        record_validity_decision(
            self.journal,
            validity,
            producer=self.config.producer,
            notes=observation.market_reference,
            event_id=_stable_id(str(intent_id), "validity"),
        )
        if validity.disposition.value != "accepted":
            return DecisionRecord(
                **base,
                intent_id=intent_id,
                validity_disposition=validity.disposition.value,
                validity_reason=validity.reason_code,
                submission="stopped_by_validity",
            )

        safety_events.append(self.safety.mode.value)
        risk = self._risk_engine().evaluate(
            self._risk_request(observation, decision, intent_id),
            self._risk_state(ledger, observation, now),
            now=now,
        )
        self._record_risk(risk, now)
        if not risk.allowed:
            reasons = set(risk.reason_codes)
            blocked_by_safety = bool(reasons & {"trading_halted", "reduce_only_violation"})
            return DecisionRecord(
                **base,
                intent_id=intent_id,
                validity_disposition="accepted",
                validity_reason=validity.reason_code,
                risk_decision="rejected",
                risk_reason_codes=tuple(risk.reason_codes),
                submission="blocked_by_safety" if blocked_by_safety else "stopped_by_risk",
            )

        self._reservations[intent_id] = (
            risk.side, risk.quantity, max(risk.price, observation.price)
        )
        request = self._execution_request(observation, decision, intent_id)
        if self.config.mode is TradingMode.SHADOW and not self.config.shadow_submits:
            return DecisionRecord(
                **base,
                intent_id=intent_id,
                validity_disposition="accepted",
                validity_reason=validity.reason_code,
                risk_decision="allowed",
                risk_reason_codes=tuple(risk.reason_codes),
                submission="would_submit",
            )
        requests.append(request)
        return DecisionRecord(
            **base,
            intent_id=intent_id,
            validity_disposition="accepted",
            validity_reason=validity.reason_code,
            risk_decision="allowed",
            risk_reason_codes=tuple(risk.reason_codes),
            submission="submitted_to_simulator",
        )

    # --- journaling helpers ---

    def _record_decision(self, observation: MarketObservation, decision: StrategyDecision) -> None:
        self.journal.append(
            StrategyDecisionRecorded(
                event_id=_stable_id(str(self.config.session_id), observation.market_reference, "decision"),
                occurred_at=observation.observed_at,
                correlation_id=self.config.session_id,
                producer=self.config.producer,
                strategy_id=self.config.strategy_id,
                symbol=self.config.symbol,
                payload=StrategyDecisionPayload(
                    action=decision.action,
                    confidence=decision.confidence,
                    rationale=decision.rationale,
                ),
            )
        )

    def _record_intent(
        self, observation: MarketObservation, decision: StrategyDecision, intent_id: UUID
    ) -> None:
        self.journal.append(
            TradeIntentCreated(
                event_id=_stable_id(str(intent_id), "intent-event"),
                occurred_at=observation.observed_at,
                correlation_id=self.config.session_id,
                causation_id=_stable_id(
                    str(self.config.session_id), observation.market_reference, "decision"
                ),
                producer=self.config.producer,
                strategy_id=self.config.strategy_id,
                symbol=self.config.symbol,
                payload=TradeIntentPayload(
                    side=decision.action,
                    quantity=decision.quantity,
                    order_type=decision.order_type,
                    limit_price=decision.limit_price,
                    rationale=decision.rationale,
                ),
            )
        )

    def _record_risk(self, risk: RiskDecision, now: datetime) -> None:
        record_risk_decision(
            self.journal,
            risk,
            occurred_at=now,
            producer=self.config.producer,
            event_id=_stable_id(str(risk.intent_id), "risk"),
            evaluation_event_id=_stable_id(str(risk.intent_id), "risk-evaluation"),
        )

    def _risk_engine(self) -> HardRiskEngine:
        """The deterministic risk engine under the CURRENT safety mode (Phase 9 bridge).

        The engine is rebuilt only when the safety mode (or the kill switch) changes, and it always
        shares one pending-intent registry so duplicate/conflict protection survives the rebuild.
        """
        key = (self.safety.mode.value, self.safety.kill_switch_engaged)
        if self._risk_cache is None or self._risk_cache[0] != key:
            self._risk_cache = (
                key,
                HardRiskEngine(
                    risk_limits_for_safety(self.safety, self.limits),
                    pending=self._pending,
                ),
            )
        return self._risk_cache[1]

    def _journal_simulation(self, outcome: SimulationOutcome) -> None:
        # Existing Phase-3 causation_id records the original persisted intent event.
        # The order-state payload contains no order quantity, so replay needs this
        # proven link to reconstruct the leaves of an accepted, unfilled order.
        # Both mappings are deterministic and engine-independent here.
        by_order: dict[UUID, UUID] = {}
        for observation in outcome.observations:
            origin = _stable_id(str(observation.intent_id), "intent-event")
            for key in (
                uuid5(_ORDER_ID_NAMESPACE, observation.order_reference),
                scripted_order_uuid(observation.order_reference),
            ):
                prior = by_order.setdefault(key, origin)
                if prior != origin:
                    raise PaperStateError("ambiguous simulated order-to-intent identity.")
        for event in outcome.phase3_events:
            origin = by_order.get(event.order_id)
            if origin is not None and event.event_type in {
                ORDER_STATE_EVENT_TYPE, EXECUTION_FILL_EVENT_TYPE,
            }:
                if event.causation_id not in (None, origin):
                    raise PaperStateError("simulated event has conflicting intent provenance.")
                event = event.model_copy(update={"causation_id": origin})
            self.journal.append(event)

    # --- translation helpers ---

    def _validity_observation(self, entry: MarketObservation) -> ValidityMarketObservation:
        return ValidityMarketObservation(
            symbol=self.config.symbol,
            price=entry.price,
            market_event_ts=entry.observed_at,
            local_ingest_ts=entry.observed_at,
            market_reference=entry.market_reference,
        )

    def _risk_request(
        self, observation: MarketObservation, decision: StrategyDecision, intent_id: UUID
    ) -> RiskRequest:
        return RiskRequest(
            correlation_id=self.config.session_id,
            symbol=self.config.symbol,
            side=Side.BUY if decision.action == "buy" else Side.SELL,
            quantity=decision.quantity,
            price=decision.limit_price or observation.price,
            strategy_id=self.config.strategy_id,
            intent_id=intent_id,
            order_type=decision.order_type,
        )

    def _update_peak(self, ledger: PositionLedger, mark: Decimal) -> Decimal:
        """Maintain the in-session equity high-water mark from point-in-time marks."""
        equity = ledger.equity_quote({self.config.symbol: mark})
        if equity < 0:
            raise PaperStateError("negative marked equity; risk snapshot is unavailable.")
        self._peak_equity_quote = max(self._peak_equity_quote or equity, equity)
        return equity

    @staticmethod
    def _timestamp_ns(stamp: datetime) -> int:
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise PaperStateError("temporal timestamps must be timezone-aware.")
        seconds = int(stamp.timestamp())
        return seconds * 1_000_000_000 + stamp.microsecond * 1_000

    def _risk_state(
        self, ledger: PositionLedger, observation: MarketObservation, now: datetime
    ) -> RiskStateSnapshot:
        position = ledger.position(self.config.symbol)
        reserved_quantity = sum(
            quantity if side is Side.BUY else -quantity
            for side, quantity, _ in self._reservations.values()
        )
        value = position.quantity * observation.price + sum(
            quantity * max(price, observation.price) * (1 if side is Side.BUY else -1)
            for side, quantity, price in self._reservations.values()
        )
        equity = self._update_peak(ledger, observation.price)
        return RiskStateSnapshot(
            equity_quote=equity,
            peak_equity_quote=self._peak_equity_quote or equity,
            realized_pnl_quote=ledger.realized_pnl_quote(),
            gross_exposure_quote=abs(position.quantity * observation.price) + sum(
                quantity * max(price, observation.price)
                for _, quantity, price in self._reservations.values()
            ),
            net_exposure_quote=value,
            open_orders=len(self._reservations),
            open_positions=0 if position.is_flat and not self._reservations else 1,
            symbol_exposure_quote={self.config.symbol: value},
            strategy_exposure_quote={self.config.strategy_id: value},
            position_quantity={self.config.symbol: position.quantity + reserved_quantity},
            snapshot_ts=now,
        )

    def _execution_request(
        self, observation: MarketObservation, decision: StrategyDecision, intent_id: UUID
    ) -> ExecutionRequest:
        return ExecutionRequest.create(
            correlation_id=self.config.session_id,
            symbol=self.config.symbol,
            side=ExecutionSide.BUY if decision.action == "buy" else ExecutionSide.SELL,
            order_type=(
                ExecutionOrderType.LIMIT
                if decision.order_type == "limit"
                else ExecutionOrderType.MARKET
            ),
            quantity=decision.quantity,
            limit_price=decision.limit_price,
            intent_id=intent_id,
        )

    def _validate_temporal_delta(
        self,
        delta: SimulationOutcome,
        requests: Sequence[ExecutionRequest],
        *,
        boundary_ns: int,
    ) -> tuple[set[UUID], set[UUID]]:
        """Validate all native observations AND durable events before any journal write.

        This checks quantities, terminal transitions, deterministic event pairing,
        duplicate identities, fees and decision-time bounds without mutating state.
        """
        known = {request.intent_id: request for request in requests}
        remaining = {
            intent_id: quantity
            for intent_id, (_, quantity, _) in self._reservations.items()
        }
        terminal = set(self._temporal_terminal_intents)
        expected: list[tuple[Any, ...]] = []
        for item in delta.observations:
            if item.intent_id not in known:
                raise PaperStateError("simulated observation for unknown intent.")
            decision_ns = self._temporal_decided_ns.get(item.intent_id)
            if decision_ns is None or type(item.ts_ns) is not int:
                raise PaperStateError("execution observation missing a verified causal timestamp.")
            if item.ts_ns <= decision_ns:
                raise PaperStateError("simulated fill occurred before its decision completed.")
            if item.ts_ns > boundary_ns:
                raise PaperStateError("execution observation is beyond the market time boundary.")
            if item.symbol != self.config.symbol or item.correlation_id != self.config.session_id:
                raise PaperStateError("execution observation has mismatched scope.")
            if not item.order_reference or item.state is None:
                raise PaperStateError("execution observation lacks order identity or state.")
            if item.intent_id in terminal:
                raise PaperStateError("execution observed after terminal order state.")
            if (
                item.remaining_quantity is not None
                and (
                    not item.remaining_quantity.is_finite()
                    or item.remaining_quantity < _ZERO
                )
            ):
                raise PaperStateError("invalid remaining execution quantity.")
            fee = item.fee if item.fee is not None else _ZERO
            if not isinstance(fee, Decimal) or not fee.is_finite() or fee < _ZERO:
                raise PaperStateError("simulated fee must be finite and non-negative.")
            current = remaining.get(item.intent_id, _ZERO)
            if item.last_fill_quantity is not None:
                qty = item.last_fill_quantity
                price = item.last_fill_price
                if (
                    not qty.is_finite() or qty <= _ZERO or qty > current
                    or price is None or not price.is_finite() or price <= _ZERO
                ):
                    raise PaperStateError("invalid simulated fill or reserved quantity.")
                current -= qty
                remaining[item.intent_id] = current
            # Each native observation must agree exactly with the paper reservation
            # and the order's cumulative fill progress, not only the event payload.
            original_qty = known[item.intent_id].quantity
            if (
                not isinstance(item.remaining_quantity, Decimal)
                or not item.remaining_quantity.is_finite()
                or item.remaining_quantity != current
            ):
                raise PaperStateError(
                    "native remaining quantity does not match the reserved quantity."
                )
            if (
                not isinstance(item.filled_quantity, Decimal)
                or not item.filled_quantity.is_finite()
                or item.filled_quantity != original_qty - current
            ):
                raise PaperStateError(
                    "native cumulative filled quantity does not match order progress."
                )
            if item.state.value == "filled" and current != _ZERO:
                raise PaperStateError("terminal filled state conflicts with outstanding quantity.")
            if item.state.value in {"filled", "canceled", "rejected", "expired"}:
                terminal.add(item.intent_id)
                remaining[item.intent_id] = _ZERO
            from datetime import timezone
            occurred = datetime.fromtimestamp(item.ts_ns / 1_000_000_000, tz=timezone.utc)
            common = (
                uuid5(_ORDER_ID_NAMESPACE, item.order_reference),
                item.correlation_id,
                item.symbol,
                item.producer,
                occurred,
            )
            expected.append((ORDER_STATE_EVENT_TYPE, *common, item.state.value))
            if item.last_fill_quantity is not None:
                expected.append((
                    EXECUTION_FILL_EVENT_TYPE, *common,
                    item.last_fill_quantity,
                    item.last_fill_price,
                    fee,
                    item.fee_asset,
                    item.remaining_quantity,
                ))

        new_ids: set[UUID] = set()
        actual: list[tuple[Any, ...]] = []
        for event in delta.phase3_events:
            if event.event_id in new_ids or event.event_id in self._temporal_seen_event_ids:
                raise PaperStateError("duplicate native execution event id.")
            new_ids.add(event.event_id)
            if event.correlation_id != self.config.session_id or event.symbol != self.config.symbol:
                raise PaperStateError("journal event has mismatched correlation/symbol.")
            at_ns = self._timestamp_ns(event.occurred_at)
            if at_ns > boundary_ns:
                raise PaperStateError("journal execution event is beyond current market horizon.")
            common = (
                event.order_id,
                event.correlation_id,
                event.symbol,
                event.producer,
                event.occurred_at,
            )
            if event.event_type == ORDER_STATE_EVENT_TYPE:
                signature = (ORDER_STATE_EVENT_TYPE, *common, event.payload.state)
            elif event.event_type == EXECUTION_FILL_EVENT_TYPE:
                signature = (
                    EXECUTION_FILL_EVENT_TYPE, *common,
                    event.payload.quantity,
                    event.payload.price,
                    event.payload.fee,
                    event.payload.fee_asset,
                    event.payload.remaining_quantity,
                )
            else:
                raise PaperStateError("unexpected execution event type in temporal delta.")
            actual.append(signature)

        if actual != expected:
            raise PaperStateError(
                "execution journal event order/sequence does not match native observations."
            )
        return terminal, new_ids

    def _apply_fills(
        self,
        outcome: SimulationOutcome,
        requests: Sequence[ExecutionRequest],
        ledger: PositionLedger,
    ) -> None:
        by_intent = {request.intent_id: request for request in requests}
        for observation in outcome.observations:
            if observation.last_fill_quantity is None:
                continue
            request = by_intent.get(observation.intent_id)
            if request is None:
                raise PaperStateError(
                    f"simulated fill for unknown intent {observation.intent_id}; refusing to guess."
                )
            deadline = self._temporal_decided_ns.get(observation.intent_id)
            if deadline is not None and observation.ts_ns <= deadline:
                raise PaperStateError("simulated fill occurred before its decision completed.")
            ledger.apply_fill(
                symbol=observation.symbol,
                side="buy" if request.side is ExecutionSide.BUY else "sell",
                quantity=observation.last_fill_quantity,
                price=observation.last_fill_price or request.limit_price or _ZERO,
                fee=observation.fee or _ZERO,
            )

    def _reconcile_temporal_reservations(self, delta: SimulationOutcome) -> None:
        """Release only filled/canceled exposure; protect remaining partial quantity."""
        for item in delta.observations:
            existing = self._reservations.get(item.intent_id)
            if existing is None:
                continue
            side, quantity, price = existing
            if item.last_fill_quantity is not None:
                if item.last_fill_quantity <= 0 or item.last_fill_quantity > quantity:
                    raise PaperStateError("simulated partial fill exceeds reserved quantity.")
                quantity -= item.last_fill_quantity
            if item.state is not None and item.state.value in {
                "canceled", "rejected", "expired"
            }:
                quantity = _ZERO
            if quantity == _ZERO:
                self._reservations.pop(item.intent_id, None)
                self._pending.remove(item.intent_id)
            elif quantity != existing[1]:
                self._reservations[item.intent_id] = (side, quantity, price)
                self._pending.register(PendingIntent(
                    intent_id=item.intent_id,
                    symbol=self.config.symbol,
                    strategy_id=self.config.strategy_id,
                    side=side,
                    quantity=quantity,
                    notional=quantity * price,
                ))

    @staticmethod
    def _order_summaries(outcome: SimulationOutcome) -> tuple[OrderSummary, ...]:
        latest: dict[str, Any] = {}
        for observation in outcome.observations:
            latest[observation.order_reference] = observation
        return tuple(
            OrderSummary(
                order_reference=reference,
                intent_id=observation.intent_id,
                correlation_id=observation.correlation_id,
                symbol=observation.symbol,
                state=(observation.state.value if observation.state else "unknown"),
                filled_quantity=observation.filled_quantity,
                remaining_quantity=observation.remaining_quantity,
            )
            for reference, observation in sorted(latest.items())
        )

    @staticmethod
    def _fill_summaries(outcome: SimulationOutcome) -> tuple[FillSummary, ...]:
        return tuple(
            FillSummary(
                order_reference=observation.order_reference,
                intent_id=observation.intent_id,
                correlation_id=observation.correlation_id,
                symbol=observation.symbol,
                quantity=observation.last_fill_quantity or _ZERO,
                price=observation.last_fill_price or _ZERO,
                remaining_quantity=observation.remaining_quantity,
            )
            for observation in outcome.observations
            if observation.last_fill_quantity is not None
        )
