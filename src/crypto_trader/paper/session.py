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

Documented limitation: the decision pass produces the session's approved requests and the simulated
pass then replays them, so simulated fills reach the position ledger after the decision pass.
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
from .simulation import ExecutionSimulator, SimulationOutcome

__all__ = ["TradingSession"]

_SESSION_NAMESPACE = UUID("7d4f2b91-3c6a-4e58-9b2f-1a9d5c8e7f30")
_ZERO = Decimal("0")


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

    def __post_init__(self) -> None:
        if self.config.places_simulated_orders and self.simulator is None:
            raise PaperStateError("simulated order submission requires an execution simulator.")

    # --- resume support ---

    def resume_index(self) -> int:
        """How many observations this session already processed (derived from the journal)."""
        processed = 0
        for event in self.journal.read_by_correlation(self.config.session_id):
            if (
                event.event_type == STRATEGY_DECISION_EVENT_TYPE
                and event.producer == self.config.producer
            ):
                processed += 1
        return processed

    # --- run ---

    def run(self, *, start_index: int = 0) -> RunReport:
        """Process the feed from ``start_index`` and return the session report."""
        entries = list(self.feed.observations())
        if not isinstance(start_index, int) or not 0 <= start_index <= len(entries):
            raise PaperStateError("start_index must be within the feed.")

        ledger = PositionLedger(Decimal(self.config.starting_equity_quote))
        self._reservations = {}
        self._pending = PendingIntentRegistry()
        self._risk_cache = None
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

        for index in range(start_index, len(entries)):
            observation = entries[index]
            decision = self.decisions.decide(observation)
            now = decision.decided_at or observation.observed_at
            if now.tzinfo is None or now.utcoffset() is None:
                raise PaperStateError("a decision timestamp must be timezone-aware.")

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
        if placements and self.simulator is not None:
            outcome = self.simulator.simulate(placements)
            self._journal_simulation(outcome)
            self._apply_fills(outcome, requests, ledger)

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
            "The decision pass precedes the simulated pass, so fills land after the decisions.",
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
        for event in outcome.phase3_events:
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
        equity = ledger.starting_equity_quote + ledger.realized_pnl_quote()
        return RiskStateSnapshot(
            equity_quote=equity,
            peak_equity_quote=max(equity, ledger.starting_equity_quote),
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
            ledger.apply_fill(
                symbol=observation.symbol,
                side="buy" if request.side is ExecutionSide.BUY else "sell",
                quantity=observation.last_fill_quantity,
                price=observation.last_fill_price or request.limit_price or _ZERO,
                fee=observation.fee or _ZERO,
            )

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
