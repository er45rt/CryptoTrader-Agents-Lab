"""The production runtime boundary with writer fencing (Phase 14).

This is the smallest production path that reuses the real components instead of reimplementing
them: the Phase 11 execution port, the Phase 8 recovery/reconciliation runtime, the Phase 9 safety
controller, the Phase 13 break-glass ownership/fencing control, the Phase 5 journal, the Phase 6
validity gate and the Phase 7 deterministic Hard Risk engine.

Enforced decision order for every new-risk order::

    Trade Intent -> Decision Validity -> Deterministic Hard Risk -> safety/writer-fence gate -> execution

The path is split into two explicit steps precisely so the final gate cannot be skipped or cached:

* :meth:`ProductionRuntime.prepare_intent` runs validity and Hard Risk and returns a preparation.
  It contacts no venue and confers no authority to trade.
* :meth:`ProductionRuntime.submit_prepared` re-reads the persisted safety state and re-validates the
  held writer lease IMMEDIATELY before the venue call, then submits.

Between those two calls an operator break-glass takeover can happen - that is the real TOCTOU
window - and the fence refuses the submission. A stale writer, a halted posture, an open break-glass
epoch or a competing writer can never reach the venue.

The runtime is venue-agnostic at the boundary: it depends on the engine-independent Phase 4
``ExecutionPort``. Phase 14 wires it to Binance **Spot Testnet** only; the production environment is
selected explicitly and refused while disabled.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from crypto_trader.breakglass import BreakGlassControl, BreakGlassError, WriterGuard
from crypto_trader.events import (
    ORDER_STATE_EVENT_TYPE, ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE,
    EXECUTION_FILL_EVENT_TYPE, VENUE_TRADE_EVENT_TYPE, ACCOUNT_EQUITY_EVENT_TYPE,
    OrderSubmissionAttempted,
)
from crypto_trader.execution import (
    DurableVenueWritePort,
    ExecutionCapability,
    ExecutionOrderType,
    ExecutionPort,
    ExecutionRequest,
    ExecutionRuntimeState,
    ExecutionSide,
    ExecutionTimeInForce,
    InstrumentSpec,
    NonDurableExecutionError,
)
from crypto_trader.execution.binance_testnet import AmbiguousOrderError
from crypto_trader.execution.binance_testnet.startup import (
    BinanceStartupCollector, StartupEvidenceError,
)
from crypto_trader.persistence import EventJournal
from crypto_trader.recovery.journaling import reconciliation_events
from crypto_trader.recovery import (
    OrderStateReconstructor,
    ReconciliationEngine,
    ReconciliationPolicy,
    RestartRecovery,
    StaticVenueStateSource,
    VenueStateSource,
    reconstruct_order_states,
)
from crypto_trader.risk import (
    HardRiskEngine,
    PendingIntentRegistry,
    RiskLimits,
    RiskRequest,
    RiskStateSnapshot,
    Side as RiskSide,
)
from crypto_trader.safety import SafetyController, SafetyMode, risk_limits_for_safety
from crypto_trader.validity import (
    DecisionValidityGate,
    Disposition,
    MarketStateProvider,
    ValidityPolicy,
    ValidityRequest,
)

from .environment import EnvironmentDecision, TradingEnvironment, resolve_environment
from .errors import (
    ProductionRecoveryRequiredError,
    ProductionSafetyBlockedError,
    ProductionStartupError,
    ProductionStateError,
)
from .fence import FenceCheck, ProductionFence
from .risk_state import ProductionRiskStateSource

_STRATEGY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

__all__ = [
    "PRODUCTION_PRODUCER",
    "IntentPreparation",
    "ProductionExecutionOutcome",
    "ProductionIntent",
    "ProductionRuntime",
    "ProductionStartupReport",
    "ProductionSubmitResult",
    "intent_deadline",
    "production_control",
    "validity_request_for",
]

PRODUCTION_PRODUCER = "application.production"

_DEFAULT_OWNER_ID = "normal:production-runtime"

_SIDE_TO_RISK = {
    ExecutionSide.BUY: RiskSide.BUY,
    ExecutionSide.SELL: RiskSide.SELL,
}


def canonical_event(event) -> str:
    # Concrete models preserve payload fields, envelope identity and explicit nulls.
    return json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class JournalView:
    contents: tuple[tuple[UUID, str], ...]
    required_references: tuple[str, ...]
    state_references: tuple[str, ...]
    fingerprint: str

    @classmethod
    def capture(cls, journal, symbol: str) -> JournalView:
        events = journal.read_range()
        rebuilt = OrderStateReconstructor()
        for event in events:
            if event.event_type in {
                ORDER_STATE_EVENT_TYPE, ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE,
                EXECUTION_FILL_EVENT_TYPE, VENUE_TRADE_EVENT_TYPE, ACCOUNT_EQUITY_EVENT_TYPE,
            } and event.symbol != symbol:
                raise StartupEvidenceError("writing startup requires a dedicated single-symbol journal.")
            rebuilt.handle(event)
        states = rebuilt.states()
        if any(state.symbol != symbol for state in states) or any(
            anchor.symbol != symbol for anchor in rebuilt.attempt_anchor_map().values()
        ):
            raise StartupEvidenceError("reconstructed journal scope does not match the instrument.")
        contents = tuple((event.event_id, canonical_event(event)) for event in events)
        if len({identity for identity, _ in contents}) != len(contents):
            raise StartupEvidenceError("journal identities are not unique.")
        return cls(
            contents=contents,
            required_references=tuple(sorted(
                state.order_reference for state in states
                if state.submission_attempted and not state.is_terminal
            )),
            state_references=tuple(sorted(state.order_reference for state in states)),
            fingerprint=hashlib.sha256(json.dumps(
                [value for _, value in contents], separators=(",", ":"),
            ).encode()).hexdigest(),
        )

    def require_stable(self, journal, *, symbol, run=None, now=None, producer=None, correlation=None):
        current = JournalView.capture(journal, symbol)
        allowed = ()
        if run is not None:
            ids = tuple(run.journaled_event_ids)
            baseline_ids = {identity for identity, _ in self.contents}
            if len(ids) != 3 or len(set(ids)) != 3 or baseline_ids.intersection(ids):
                raise StartupEvidenceError("recovery event identities are invalid.")
            expected = reconciliation_events(
                run.report, occurred_at=now, producer=producer,
                correlation_id=correlation, event_ids=ids,
            )
            allowed = tuple((event.event_id, canonical_event(event)) for event in expected)
            actual = dict(current.contents)
            if any(actual.get(identity) != value for identity, value in allowed):
                raise StartupEvidenceError("recovery pass additions do not match this run.")
        allowed_ids = {identity for identity, _ in allowed}
        if tuple(item for item in current.contents if item[0] not in allowed_ids) != self.contents:
            raise StartupEvidenceError("journal changed during startup evidence collection/recovery.")
        if (
            current.required_references != self.required_references
            or current.state_references != self.state_references
        ):
            raise StartupEvidenceError("journal-derived query coverage changed during startup.")
        if run is not None and tuple(sorted(
            state.order_reference for state in run.rebuilt_states
        )) != self.state_references:
            raise StartupEvidenceError("recovery reconstructed a different journal view.")
        if run is not None and any(state.symbol != symbol for state in run.rebuilt_states):
            raise StartupEvidenceError("recovery rebuilt another symbol.")


@dataclass(frozen=True)
class ProductionIntent:
    """One strategy/AI-produced intent presented to the production decision path.

    The intent carries NO approval authority: it is an unvalidated request. Validity, Hard Risk and
    the final safety/fence gate are applied by the runtime, never by the caller.
    """

    correlation_id: UUID
    symbol: str
    side: ExecutionSide
    order_type: ExecutionOrderType
    quantity: Decimal
    strategy_id: str
    decision_started_at: datetime
    decision_completed_at: datetime
    market_event_ts: datetime
    reference_price: Decimal
    market_reference: str
    intent_id: UUID = field(default_factory=uuid4)
    limit_price: Decimal | None = None
    time_in_force: ExecutionTimeInForce = ExecutionTimeInForce.GTC
    valid_until: datetime | None = None
    decision_scope: str = "default"

    def __post_init__(self) -> None:
        if not isinstance(self.correlation_id, UUID) or not isinstance(self.intent_id, UUID):
            raise ProductionStateError("an intent requires correlation_id and intent_id UUIDs.")
        if not isinstance(self.side, ExecutionSide):
            raise ProductionStateError("an intent requires an ExecutionSide.")
        if not isinstance(self.order_type, ExecutionOrderType):
            raise ProductionStateError("an intent requires an ExecutionOrderType.")
        if not isinstance(self.quantity, Decimal) or self.quantity <= 0:
            raise ProductionStateError("an intent requires a positive Decimal quantity.")
        if not isinstance(self.reference_price, Decimal) or self.reference_price <= 0:
            raise ProductionStateError(
                "an intent requires a positive Decimal reference price: Hard Risk needs an "
                "explicit price and never guesses one."
            )
        if self.order_type is ExecutionOrderType.LIMIT and (
            not isinstance(self.limit_price, Decimal) or self.limit_price <= 0
        ):
            raise ProductionStateError("a limit intent requires a positive Decimal limit price.")
        if self.order_type is ExecutionOrderType.MARKET and self.limit_price is not None:
            raise ProductionStateError("a market intent must not carry a limit price.")
        for name in (
            "decision_started_at",
            "decision_completed_at",
            "market_event_ts",
            "valid_until",
        ):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ProductionStateError(f"{name} must be timezone-aware.")
        if not self.symbol or not self.strategy_id or not self.market_reference:
            raise ProductionStateError("symbol, strategy_id and market_reference are required.")
        if not isinstance(self.strategy_id, str) or not _STRATEGY_ID_PATTERN.fullmatch(
            self.strategy_id
        ):
            raise ProductionStateError(
                "strategy_id must be 1-128 characters of letters, digits, '_', '-' or '.'."
            )

    @property
    def risk_price(self) -> Decimal:
        """The deterministic price Hard Risk evaluates against (never a guessed market price)."""
        return self.limit_price if self.limit_price is not None else self.reference_price


def validity_request_for(intent: ProductionIntent) -> ValidityRequest:
    """Build the one canonical validity request for a production intent."""
    if not isinstance(intent, ProductionIntent):
        raise ProductionStateError("validity_request_for expects a ProductionIntent.")
    return ValidityRequest(
        correlation_id=intent.correlation_id,
        symbol=intent.symbol,
        decision_started_at=intent.decision_started_at,
        decision_completed_at=intent.decision_completed_at,
        market_event_ts=intent.market_event_ts,
        reference_price=intent.reference_price,
        market_reference=intent.market_reference,
        intent_id=intent.intent_id,
        decision_scope=intent.decision_scope,
        valid_until=intent.valid_until,
    )


def intent_deadline(intent: ProductionIntent, policy: ValidityPolicy) -> datetime:
    """Return the earliest deterministic deadline after which an intent is invalid."""
    if not isinstance(intent, ProductionIntent):
        raise ProductionStateError("intent_deadline expects a ProductionIntent.")
    if not isinstance(policy, ValidityPolicy):
        raise ProductionStateError("intent_deadline requires a resolved ValidityPolicy.")
    deadlines = [
        intent.market_event_ts.astimezone(timezone.utc)
        + timedelta(seconds=policy.max_age_seconds)
    ]
    if intent.valid_until is not None:
        deadlines.append(intent.valid_until.astimezone(timezone.utc))
    if policy.valid_until_seconds is not None:
        deadlines.append(
            intent.decision_completed_at.astimezone(timezone.utc)
            + timedelta(seconds=policy.valid_until_seconds)
        )
    return min(deadlines)


@dataclass(frozen=True)
class IntentPreparation:
    """The result of validity + Hard Risk. It is NOT an authorization to trade."""

    intent_id: UUID
    correlation_id: UUID
    symbol: str
    side: ExecutionSide
    order_type: ExecutionOrderType
    quantity: Decimal
    limit_price: Decimal | None
    time_in_force: ExecutionTimeInForce
    strategy_id: str
    approved: bool
    stages: tuple[str, ...]
    blocked_reason_code: str | None
    safety_mode: str
    prepared_at: datetime

    def to_record(self) -> dict[str, object]:
        """A JSON-able, secret-free record of the decision path so far."""
        return {
            "intent_id": str(self.intent_id),
            "correlation_id": str(self.correlation_id),
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": str(self.quantity),
            "limit_price": None if self.limit_price is None else str(self.limit_price),
            "strategy_id": self.strategy_id,
            "approved": self.approved,
            "stages": list(self.stages),
            "blocked_reason_code": self.blocked_reason_code,
            "safety_mode_at_prepare": self.safety_mode,
            "prepared_at": self.prepared_at.astimezone(timezone.utc).isoformat(),
        }


@dataclass(frozen=True)
class ProductionSubmitResult:
    """One accepted submission that passed the final fence, with its evidence."""

    order_reference: str
    intent_id: UUID
    correlation_id: UUID
    environment: str
    safety_mode: str
    fence_term: int
    submitted_at: datetime
    fence: FenceCheck

    def to_record(self) -> dict[str, object]:
        return {
            "order_reference": self.order_reference,
            "intent_id": str(self.intent_id),
            "correlation_id": str(self.correlation_id),
            "environment": self.environment,
            "safety_mode": self.safety_mode,
            "fence_term": self.fence_term,
            "submitted_at": self.submitted_at.astimezone(timezone.utc).isoformat(),
            "fence": self.fence.to_record(),
        }


@dataclass(frozen=True)
class ProductionExecutionOutcome:
    """The complete outcome of one convenience-path execution (never a silent submit)."""

    submitted: bool
    preparation: IntentPreparation
    submission: ProductionSubmitResult | None
    blocked_reason_code: str | None

    def to_record(self) -> dict[str, object]:
        return {
            "submitted": self.submitted,
            "preparation": self.preparation.to_record(),
            "submission": None if self.submission is None else self.submission.to_record(),
            "blocked_reason_code": self.blocked_reason_code,
        }


@dataclass(frozen=True)
class ProductionStartupReport:
    """The evidence of a fail-closed startup that claimed the normal writer lease."""

    environment: str
    base_url: str
    owner_id: str
    fence_term: int
    safety_mode: str
    safety_state_source: str
    recovered_orders: int
    reconciliation_state: str
    started_at: datetime
    journaled_event_ids: tuple[UUID, ...] = ()
    notes: tuple[str, ...] = ()
    venue_evidence: dict[str, object] | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "base_url": self.base_url,
            "owner_id": self.owner_id,
            "fence_term": self.fence_term,
            "safety_mode": self.safety_mode,
            "safety_state_source": self.safety_state_source,
            "recovered_orders": self.recovered_orders,
            "reconciliation_state": self.reconciliation_state,
            "started_at": self.started_at.astimezone(timezone.utc).isoformat(),
            "journaled_event_ids": [str(event_id) for event_id in self.journaled_event_ids],
            "notes": list(self.notes),
            "venue_evidence": self.venue_evidence,
        }


def production_control(
    *,
    journal: EventJournal,
    state_dir: str | Path,
    reconciliation_policy: ReconciliationPolicy | None = None,
    clock: Callable[[], datetime] | None = None,
    venue=None,
) -> BreakGlassControl:
    """Build the Phase 13 control the runtime uses (an injection point for tests/venues)."""
    return BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=venue,
        reconciliation_policy=reconciliation_policy,
        clock=clock,
    )


class ProductionRuntime:
    """The single production ingress from a strategy/AI intent to a venue submission."""

    def __init__(
        self,
        *,
        journal: EventJournal,
        state_dir: str | Path,
        instrument: InstrumentSpec,
        execution: ExecutionPort,
        validity_gate: DecisionValidityGate,
        market_state: MarketStateProvider,
        limits: RiskLimits,
        risk_state_provider: Callable[[datetime], RiskStateSnapshot] | ProductionRiskStateSource,
        reconciliation_policy: ReconciliationPolicy,
        environment: str | TradingEnvironment | None = None,
        mainnet_authorized: bool = False,
        mainnet_base_url: str | None = None,
        owner_id: str = _DEFAULT_OWNER_ID,
        venue_sources: Sequence[VenueStateSource] = (),
        control: BreakGlassControl | None = None,
        clock: Callable[[], datetime] | None = None,
        pending: PendingIntentRegistry | None = None,
        producer: str = PRODUCTION_PRODUCER,
    ) -> None:
        if journal is None:
            raise ProductionStateError("the production runtime requires the event journal.")
        if not str(state_dir):
            raise ProductionStateError("the production runtime requires a state directory.")
        if not isinstance(instrument, InstrumentSpec):
            raise ProductionStateError("the production runtime requires an InstrumentSpec.")
        if not isinstance(limits, RiskLimits):
            raise ProductionStateError("the production runtime requires explicit RiskLimits.")
        if not isinstance(reconciliation_policy, ReconciliationPolicy):
            raise ProductionStateError(
                "the production runtime requires an explicit ReconciliationPolicy; reconciliation "
                "is never invented."
            )
        if not callable(risk_state_provider):
            raise ProductionStateError("the production runtime requires a risk-state provider.")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ProductionStateError("the production runtime requires a non-empty owner id.")

        # SQLiteEventJournal is thread-affine. The production runtime must be constructed
        # and subsequently used on that same owner thread; producer threads use admission only.
        self._owner_thread_id = threading.get_ident()
        self._journal = journal
        self._state_dir = Path(state_dir)
        self._instrument = instrument
        self._execution = execution
        self._validity_gate = validity_gate
        self._market_state = market_state
        self._limits = limits
        self._risk_state_provider = risk_state_provider
        self._reconciliation_policy = reconciliation_policy
        self._requested_environment = environment
        self._mainnet_authorized = mainnet_authorized
        self._mainnet_base_url = mainnet_base_url
        self._owner_id = owner_id.strip()
        self._venue_sources = tuple(venue_sources)
        # Stage 3 capability gate; mutable wiring is checked again at submission.
        capability = self._resolve_execution_capability(execution)
        self._capability = capability
        self._durable_venue_writes = False
        if capability.writes_to_venue:
            if not isinstance(execution, DurableVenueWritePort):
                raise NonDurableExecutionError(
                    "the injected execution port declares venue writing but does not inherit the "
                    "approved durable submission contract; refusing to construct a production "
                    "runtime that could create venue risk without a durable attempt boundary."
                )
            attempt_journal = execution.durable_attempt_journal()
            if attempt_journal is None or attempt_journal is not journal:
                raise NonDurableExecutionError(
                    "the injected venue-writing execution port is not wired to this runtime's own "
                    "durable journal, so a submission-attempt marker could not be committed before "
                    "the first send; refusing to construct the production runtime."
                )
            self._durable_venue_writes = True
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._pending = pending if pending is not None else PendingIntentRegistry()
        self._issued_preparations: dict[UUID, tuple[IntentPreparation, datetime]] = {}
        self._prepared_revisions = {}
        self._prior_session_intents: frozenset[UUID] = frozenset()
        self._session_evidence: tuple[tuple[UUID, str], ...] = ()
        self._admission_token: object | None = None
        self._producer = producer
        self._control = (
            control
            if control is not None
            else production_control(
                journal=journal,
                state_dir=state_dir,
                reconciliation_policy=reconciliation_policy,
                clock=self._clock,
            )
        )
        if self._durable_venue_writes:
            self._require_canonical_risk_binding()
        self._state = ExecutionRuntimeState.STOPPED
        self._environment: EnvironmentDecision | None = None
        self._fence: ProductionFence | None = None
        self._guard: WriterGuard | None = None
        self._last_fence: FenceCheck | None = None
        self._last_outcome: str | None = None
        self._last_refusal: str | None = None
        self._startup: ProductionStartupReport | None = None

    # --- observability -------------------------------------------------------

    @property
    def control(self) -> BreakGlassControl:
        return self._control

    @property
    def instrument(self) -> InstrumentSpec:
        return self._instrument

    def environment(self) -> EnvironmentDecision | None:
        return self._environment

    def state(self) -> ExecutionRuntimeState:
        return self._state

    def owner_id(self) -> str:
        return self._owner_id

    def fence_term(self) -> int | None:
        return None if self._fence is None else self._fence.fence_term

    def startup_report(self) -> ProductionStartupReport | None:
        return self._startup

    @property
    def validity_policy(self) -> ValidityPolicy:
        """The immutable validity policy shared with production admission."""
        return self._validity_gate.policy

    def status(self) -> dict[str, object]:
        """Read-only runtime posture (never writes, never repairs, never caches an approval)."""
        self._require_owner_thread()
        posture = self._control.status()
        return {
            "runtime_state": self._state.value,
            "environment": (
                None if self._environment is None else self._environment.environment.value
            ),
            "base_url": None if self._environment is None else self._environment.base_url,
            "owner_id": self._owner_id,
            "fence_term": self.fence_term(),
            "safety_mode": posture.safety_mode,
            "kill_switch_engaged": posture.kill_switch_engaged,
            "epoch_open": posture.epoch_open,
            "new_risk_permitted": posture.new_risk_permitted,
            "last_outcome": self._last_outcome,
            "last_refusal": self._last_refusal,
        }

    # --- lifecycle -----------------------------------------------------------

    def start(self, *, correlation_id: UUID | None = None) -> ProductionStartupReport:
        """Writing startup claims first, then proves current signed venue/recovery evidence."""
        self._require_owner_thread()
        if self._state is ExecutionRuntimeState.RUNNING:
            raise ProductionStateError("the production runtime is already running.")
        self._startup = None
        self._environment = self._guard = self._fence = self._last_fence = None
        self._last_outcome = self._last_refusal = None
        execution_started = False
        phase = "admission_binding"
        try:
            if self._durable_venue_writes and self._admission_token is None:
                self._last_refusal = "admission_unbound"
                raise ProductionStateError(
                    "venue-writing production runtime requires a bound "
                    "ProductionAdmissionController before startup."
                )
            phase = "startup_safety_anchor"
            startup_safety_anchor = self._safety_transition_anchor()
            phase = "environment"
            now = self._now()
            run_id = correlation_id or uuid4()
            nonce = uuid4()  # Caller correlation identity never supplies freshness.
            decision = resolve_environment(
                self._requested_environment, mainnet_authorized=self._mainnet_authorized,
                mainnet_base_url=self._mainnet_base_url,
            )
            phase = "startup_posture"
            load = self._control.load_safety_state()
            self._require_startup_posture(load.controller)
            self._require_startup_safety_anchor(startup_safety_anchor)
            startup_safety_mode = load.controller.mode.value
            phase = "execution_context"
            capability = self._resolve_execution_capability(self._execution)
            if capability is not self._capability:
                raise NonDurableExecutionError("execution capability changed before startup.")
            writing = capability.writes_to_venue
            collector = None
            if writing:
                if self._execution.durable_attempt_journal() is not self._journal:
                    raise NonDurableExecutionError("execution startup journal wiring changed.")
                if self._venue_sources:
                    raise StartupEvidenceError("writing startup refuses caller-supplied venue sources.")
                collector = BinanceStartupCollector(
                    self._execution, journal=self._journal,
                    instrument=self._instrument, endpoint=decision.base_url,
                )

            def claim():
                try:
                    return self._control.claim_normal_writer(
                        owner_id=self._owner_id, reason_code="production_runtime_started",
                    )
                except BreakGlassError as exc:
                    self._last_refusal = "writer_lease_unavailable"
                    raise ProductionStateError("startup refused: normal writer lease unavailable.") from exc

            guard = fence = view = batch = None

            def authorize_startup_fence_locked(active_fence: ProductionFence) -> FenceCheck:
                self._require_startup_safety_anchor(startup_safety_anchor)
                return active_fence.authorize_new_risk(
                    now=self._now(),
                    correlation_id=run_id,
                    prepared_safety_mode=startup_safety_mode,
                )

            def authorize_startup_fence(active_fence: ProductionFence) -> FenceCheck:
                with self._control.ownership_store.exclusive(purpose="startup_fence"):
                    return authorize_startup_fence_locked(active_fence)

            if writing:
                phase = "writer_claim"
                guard = claim()
                fence = ProductionFence(
                    control=self._control, guard=guard, owner_id=self._owner_id,
                    environment=decision.environment,
                )
                authorize_startup_fence(fence)
                phase = "venue_evidence"
                view = JournalView.capture(self._journal, self._instrument.symbol)
                prior_intents = frozenset(
                    identity for identity, contents in view.contents
                    if json.loads(contents)["event_type"] == "trade.intent.created"
                )
                batch = collector.collect(
                    nonce=nonce, fence_term=fence.fence_term, view=view, clock=self._now,
                )
                # The ownership lock is deliberately not held during network I/O.
                authorize_startup_fence(fence)
                source = collector.validate(batch, nonce=nonce, fence_term=fence.fence_term, view=view)
                view.require_stable(self._journal, symbol=self._instrument.symbol)
                sources = [source]
                local_states = None  # Preserve conflicts and immutable durable attempt anchors.
            else:
                # Explicit NON_WRITING ports retain their offline behavior.
                reconstructed, _ = reconstruct_order_states(self._journal)
                local_states = reconstructed.states()
                sources = list(self._venue_sources) or [
                    StaticVenueStateSource(entries=(), name="production.no-venue-evidence")
                ]
            phase = "recovery"
            recovery = RestartRecovery(
                engine=ReconciliationEngine(self._reconciliation_policy), journal=self._journal,
                venue_sources=sources, producer=self._producer,
            )
            run = recovery.run(now=now, local_states=local_states, correlation_id=run_id)
            if not run.report.is_fully_resolved or run.requires_halt_new_risk or not run.may_resume_trading:
                self._last_refusal = "recovery_required"
                raise ProductionRecoveryRequiredError(
                    "startup refused: unresolved order state requires recovery: "
                    f"{sorted(run.report.unresolved_orders)}"
                )
            if not writing:
                guard = claim()
                fence = ProductionFence(
                    control=self._control, guard=guard, owner_id=self._owner_id,
                    environment=decision.environment,
                )

            def verify_evidence():
                if writing:
                    if (self._execution is not collector.execution or self._venue_sources
                        or self._instrument != collector.instrument):
                        raise StartupEvidenceError("production execution/source wiring changed during startup.")
                    if self._resolve_execution_capability(self._execution) is not capability:
                        raise StartupEvidenceError("execution capability changed during startup.")
                    collector.validate(batch, nonce=nonce, fence_term=fence.fence_term, view=view)
                    view.require_stable(
                        self._journal, symbol=self._instrument.symbol, run=run, now=now,
                        producer=self._producer, correlation=run_id,
                    )

            phase = "post_recovery_evidence"
            verify_evidence()
            check = authorize_startup_fence(fence)
            phase = "execution_boundary_start_failed"
            execution_started = True
            self._execution.start()
            phase = "final_startup_evidence"
            # Serialize final publication with takeover, without locking the venue reads.
            with self._control.ownership_store.exclusive(purpose="submission"):
                verify_evidence()
                check = authorize_startup_fence_locked(fence)
                if self._execution.state() is not ExecutionRuntimeState.RUNNING:
                    raise ProductionStartupError("execution boundary did not become running.")
                self._environment, self._guard, self._fence = decision, guard, fence
                self._last_fence = check
                self._startup = ProductionStartupReport(
                    environment=decision.environment.value, base_url=decision.base_url,
                    owner_id=self._owner_id, fence_term=check.fence_term,
                    safety_mode=check.safety_mode, safety_state_source=check.safety_state_source,
                    recovered_orders=len(run.rebuilt_states), reconciliation_state=run.recovery_state,
                    started_at=check.checked_at, journaled_event_ids=tuple(run.journaled_event_ids),
                    notes=decision.notes, venue_evidence=None if batch is None else batch.summary(),
                )
                # A successful fully-resolved startup supersedes any in-memory reservation from
                # the previous run; durable journal/venue state is authoritative after recovery.
                if writing:
                    # A resolved order-status report alone is not complete economic evidence.
                    # Keep attempted reservations until canonical risk refresh proves coverage.
                    for item in self._pending.items():
                        if not self._attempt_may_exist(item.intent_id):
                            self._pending.remove(item.intent_id)
                    self._prior_session_intents = prior_intents
                    self._session_evidence = tuple(
                        (identity, canonical_event(self._journal.read_by_id(identity)))
                        for identity in run.journaled_event_ids
                    )
                else:
                    self._pending.clear()
                self._issued_preparations.clear()
                self._prepared_revisions.clear()
                self._state = ExecutionRuntimeState.RUNNING
            return self._startup
        except Exception as exc:
            self._state = ExecutionRuntimeState.STOPPED
            self._startup = None
            if self._last_refusal is None:
                self._last_refusal = f"{phase}:{type(exc).__name__}"
            if execution_started:
                try:
                    self._execution.stop()
                except Exception:
                    pass  # Best effort only; never undo another writer's lease.
            raise

    def stop(self) -> None:
        """Stop serving. The held lease record stays on disk; a restart takes a fresh term."""
        self._require_owner_thread()
        self._execution.stop()
        self._state = ExecutionRuntimeState.STOPPED

    # --- decision path -------------------------------------------------------

    def prepare_intent(
        self,
        intent: ProductionIntent,
        *,
        capacity_available: bool = True,
        latest_scope_intent_id: UUID | None = None,
        _admission_token: object | None = None,
    ) -> IntentPreparation:
        """Run validity then canonical Hard Risk; writing mode refreshes read-only venue evidence."""
        self._require_owner_thread()
        self._require_admission_token(_admission_token)
        self._require_running()
        if not isinstance(intent, ProductionIntent):
            raise ProductionStateError("prepare_intent expects a ProductionIntent.")
        now = self._now()
        self._expire_preparations(now)
        controller = self._control.load_safety_state().controller
        stages: list[str] = []
        if self._durable_venue_writes:
            self._require_session_evidence()
            if self._intent_predates_session(intent):
                return self._blocked(intent, ["session:abandoned"], "prior_session_intent", controller, now)
        if (
            controller.kill_switch_engaged
            or controller.mode not in {SafetyMode.NORMAL, SafetyMode.REDUCE_ONLY}
        ):
            stages.append("safety:blocked")
            return self._blocked(
                intent,
                stages,
                "trading_halted",
                controller,
                now,
            )

        # Stage 1: deterministic decision validity / staleness.
        validity = self._validity_gate.evaluate(
            validity_request_for(intent),
            market_state=self._market_state,
            now=now,
            latest_scope_intent_id=latest_scope_intent_id,
            capacity_available=capacity_available,
        )
        stages.append(f"validity:{validity.disposition.value}")
        if validity.disposition is not Disposition.ACCEPTED:
            return self._blocked(intent, stages, validity.reason_code, controller, now)

        # Stage 2: deterministic Hard Risk, under the CURRENT persisted safety mode.
        engine = HardRiskEngine(
            risk_limits_for_safety(controller, self._limits),
            pending=self._pending,
            include_pending_reservations=self._durable_venue_writes,
            enforce_attributed_inventory=self._durable_venue_writes,
        )
        if isinstance(self._risk_state_provider, ProductionRiskStateSource):
            if self._guard is None:
                raise ProductionStateError(
                    "canonical risk state requires the active runtime writer guard."
                )
            risk_state, risk_now = self._risk_state_provider(
                now,
                guard=self._guard,
                correlation_id=intent.correlation_id,
            )
            if self._durable_venue_writes:
                # The canonical source validated journal fills, balances and venue open orders.
                # Transfer only prior attempted reservations into that proven exposure snapshot.
                for item in self._pending.items():
                    if item.intent_id not in self._issued_preparations and self._attempt_may_exist(item.intent_id):
                        self._pending.remove(item.intent_id)
        else:
            risk_state = self._risk_state_provider(now)
            risk_now = now
        decision = engine.evaluate(
            RiskRequest(
                correlation_id=intent.correlation_id,
                symbol=intent.symbol,
                side=_SIDE_TO_RISK[intent.side],
                quantity=intent.quantity,
                price=intent.risk_price,
                strategy_id=intent.strategy_id,
                intent_id=intent.intent_id,
                order_type=intent.order_type.value,
            ),
            risk_state,
            now=risk_now,
        )
        stages.append("risk:allowed" if decision.allowed else "risk:blocked")
        if not decision.allowed:
            return self._blocked(intent, stages, ",".join(decision.reason_codes), controller, now)

        # Stage 3 is the fence, and it happens in submit_prepared immediately before the venue call.
        stages.append("safety:fence_deferred_to_submit")
        preparation = IntentPreparation(
            intent_id=intent.intent_id,
            correlation_id=intent.correlation_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            time_in_force=intent.time_in_force,
            strategy_id=intent.strategy_id,
            approved=True,
            stages=tuple(stages),
            blocked_reason_code=None,
            safety_mode=controller.mode.value,
            prepared_at=now,
        )
        self._issued_preparations[intent.intent_id] = (
            preparation,
            intent_deadline(intent, self._validity_gate.policy),
        )
        if self._durable_venue_writes:
            self._prepared_revisions[intent.intent_id] = self._journal.revision()
        return preparation

    def submit_prepared(
        self,
        preparation: IntentPreparation,
        *,
        _admission_token: object | None = None,
    ) -> ProductionSubmitResult:
        """Wait on venue admission outside the lock, then atomically authorize and submit."""
        self._require_owner_thread()
        self._require_admission_token(_admission_token)
        if self._durable_venue_writes:
            try:
                self._validate_submission_preparation(preparation, release_on_failure=False)
                self._execution.await_write_admission()
            except Exception as admission_exc:
                # Admission failed before this call can create a marker or POST. Cleanup must
                # serialize with a concurrent submit of the SAME preparation: if another thread
                # crossed the durable boundary while we waited, its ambiguity reservation wins.
                try:
                    with self._control.ownership_store.exclusive(purpose="submission"):
                        issued = self._issued_preparations.get(preparation.intent_id)
                        if issued is not None and issued[0] is preparation:
                            self._release_preparation(preparation.intent_id)
                except Exception as cleanup_exc:
                    # Failure to prove safe cleanup is fail-closed: keep the reservation and
                    # preserve the original admission failure as the public cause.
                    raise admission_exc from cleanup_exc
                raise
        with self._control.ownership_store.exclusive(purpose="submission"):
            return self._submit_prepared_locked(preparation)

    def _validate_submission_preparation(
        self,
        preparation: IntentPreparation,
        *,
        release_on_failure: bool = True,
    ) -> datetime:
        """Validate local submission prerequisites; callers re-run this after taking the lock."""
        self._require_running()
        if not isinstance(preparation, IntentPreparation):
            raise ProductionStateError("submit_prepared expects an IntentPreparation.")
        if not preparation.approved:
            raise ProductionSafetyBlockedError(
                "the intent was not approved "
                f"({preparation.blocked_reason_code}); nothing is submitted."
            )
        issued = self._issued_preparations.get(preparation.intent_id)
        if issued is None or issued[0] is not preparation:
            raise ProductionStateError(
                "submit_prepared requires the exact unused preparation issued by this runtime."
            )
        now = self._now()
        if now > issued[1]:
            if release_on_failure:
                self._release_preparation(preparation.intent_id)
            raise ProductionStateError("the issued preparation expired before submission.")
        if self._fence is None:
            if release_on_failure:
                self._release_preparation(preparation.intent_id)
            raise ProductionStateError("the runtime holds no writer fence; refusing to submit.")

        try:
            capability = self._resolve_execution_capability(self._execution)
            if capability is not self._capability:
                raise NonDurableExecutionError("execution capability changed after construction")
            if (
                self._durable_venue_writes
                and self._execution.durable_attempt_journal() is not self._journal
            ):
                raise NonDurableExecutionError("execution journal changed after construction")
            if self._durable_venue_writes:
                self._require_canonical_risk_binding()
                self._require_session_evidence()
        except Exception:
            if release_on_failure:
                self._release_preparation(preparation.intent_id)
            raise
        return now

    def _submit_prepared_locked(self, preparation: IntentPreparation) -> ProductionSubmitResult:
        now = self._validate_submission_preparation(preparation)
        try:
            # The last thing that happens before the venue call. Never cached, never skipped.
            check = self._fence.authorize_new_risk(
                now=now,
                correlation_id=preparation.correlation_id,
                prepared_safety_mode=preparation.safety_mode,
            )
        except Exception as exc:
            if isinstance(exc, ProductionSafetyBlockedError):
                self._last_refusal = str(exc)
            self._release_preparation(preparation.intent_id)
            raise

        self._last_fence = check
        if self._durable_venue_writes and self._journal.revision() != self._prepared_revisions.get(preparation.intent_id):
            self._release_preparation(preparation.intent_id)
            raise ProductionStateError("journal changed after Hard Risk preparation; new decision required.")
        request = ExecutionRequest(
            intent_id=preparation.intent_id,
            correlation_id=preparation.correlation_id,
            symbol=preparation.symbol,
            side=preparation.side,
            order_type=preparation.order_type,
            quantity=preparation.quantity,
            limit_price=preparation.limit_price,
            time_in_force=preparation.time_in_force,
            strategy_id=preparation.strategy_id,
        )
        try:
            # Venue-writing execution goes through the approved durable submission sequence
            # (marker -> authorization -> one send). Non-writing execution keeps its own port.
            if self._durable_venue_writes:
                reference = DurableVenueWritePort.submit_durably(self._execution, request)
            else:
                reference = self._execution.submit(request)
        except AmbiguousOrderError as exc:
            # UNKNOWN_PENDING_RECONCILIATION: never retry this preparation. Retire the one-shot
            # issued object but KEEP its economic pending reservation until recovery/startup
            # establishes authoritative outcome.
            self._issued_preparations.pop(preparation.intent_id, None)
            self._prepared_revisions.pop(preparation.intent_id, None)
            reason_code = getattr(exc, "reason_code", None)
            self._last_outcome = (
                "unknown_pending_reconciliation"
                if not reason_code
                else f"unknown_pending_reconciliation:{reason_code}"
            )
            raise
        except Exception:
            if self._durable_venue_writes and self._attempt_may_exist(preparation.intent_id):
                self._issued_preparations.pop(preparation.intent_id, None)
                self._prepared_revisions.pop(preparation.intent_id, None)
                self._last_outcome = "unknown_pending_reconciliation"
            else:
                self._release_preparation(preparation.intent_id)
            raise
        self._release_preparation(preparation.intent_id, submitted=True)
        self._last_outcome = "submitted"
        self._last_refusal = None
        return ProductionSubmitResult(
            order_reference=reference,
            intent_id=preparation.intent_id,
            correlation_id=preparation.correlation_id,
            environment=check.environment,
            safety_mode=check.safety_mode,
            fence_term=check.fence_term,
            submitted_at=now,
            fence=check,
        )

    def execute_intent(
        self,
        intent: ProductionIntent,
        *,
        capacity_available: bool = True,
        latest_scope_intent_id: UUID | None = None,
        _admission_token: object | None = None,
    ) -> ProductionExecutionOutcome:
        """Prepare then submit; an admission-bound runtime requires its controller capability.

        Durable venue-writing paths deduplicate an exact intent_id across restart via the
        deterministic client order id and journal. A new intent_id for the same decision is not
        equivalent durable identity and must be handled before Mainnet promotion.
        """
        preparation = self.prepare_intent(
            intent,
            capacity_available=capacity_available,
            latest_scope_intent_id=latest_scope_intent_id,
            _admission_token=_admission_token,
        )
        if not preparation.approved:
            return ProductionExecutionOutcome(
                submitted=False,
                preparation=preparation,
                submission=None,
                blocked_reason_code=preparation.blocked_reason_code,
            )
        submission = self.submit_prepared(
            preparation,
            _admission_token=_admission_token,
        )
        return ProductionExecutionOutcome(
            submitted=True,
            preparation=preparation,
            submission=submission,
            blocked_reason_code=None,
        )

    # --- internals -----------------------------------------------------------

    def _bind_admission(self) -> object:
        """Bind exactly one serialized admission controller to this runtime."""
        self._require_owner_thread()
        if self._admission_token is not None:
            raise ProductionStateError(
                "production runtime already has an admission controller bound."
            )
        if self._issued_preparations or len(self._pending) != 0:
            raise ProductionStateError(
                "production runtime cannot bind admission with outstanding preparations "
                "or pending economic reservations."
            )
        token = object()
        self._admission_token = token
        return token

    def _require_admission_token(self, token: object | None) -> None:
        if self._durable_venue_writes and self._admission_token is None:
            raise ProductionStateError(
                "venue-writing production runtime requires a bound "
                "ProductionAdmissionController; direct decision-path calls are refused."
            )
        if self._admission_token is not None and token is not self._admission_token:
            raise ProductionStateError(
                "production runtime is admission-bound; direct decision-path calls are refused."
            )

    def _require_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise ProductionStateError(
                "production runtime is thread-affine: construct and use it on the journal owner "
                "thread; producer threads must use the admission controller."
            )

    def _release_preparation(self, intent_id: UUID, *, submitted: bool = False) -> None:
        self._issued_preparations.pop(intent_id, None)
        self._prepared_revisions.pop(intent_id, None)
        if submitted or not self._durable_venue_writes or not self._attempt_may_exist(intent_id):
            self._pending.remove(intent_id)

    def _expire_preparations(self, now: datetime) -> None:
        expired = [
            intent_id
            for intent_id, (_preparation, expires_at) in self._issued_preparations.items()
            if now > expires_at
        ]
        for intent_id in expired:
            if self._durable_venue_writes and self._attempt_may_exist(intent_id):
                self._issued_preparations.pop(intent_id, None)
                self._prepared_revisions.pop(intent_id, None)
            else:
                self._release_preparation(intent_id)

    def _attempt_may_exist(self, intent_id: UUID) -> bool:
        try:
            return any(isinstance(event, OrderSubmissionAttempted) and event.payload.intent_id == intent_id
                       for event in self._journal.read_range())
        except Exception:
            return True  # Failure to prove absence never releases economic risk.

    def _intent_predates_session(self, intent: ProductionIntent) -> bool:
        """Cheap producer-thread check, bound to persisted startup evidence at consumption."""
        if not self._durable_venue_writes or self._startup is None:
            return False
        boundary = self._startup.started_at
        return (intent.intent_id in self._prior_session_intents
                or intent.decision_completed_at < boundary or intent.market_event_ts < boundary)

    def _require_session_evidence(self) -> None:
        if not self._session_evidence or any(
            (event := self._journal.read_by_id(identity)) is None or canonical_event(event) != contents
            for identity, contents in self._session_evidence
        ):
            raise ProductionStateError("persisted startup session boundary is missing or changed.")

    def _require_canonical_risk_binding(self) -> ProductionRiskStateSource:
        """Fail closed unless venue writes and canonical risk evidence share one authority path."""
        source = self._risk_state_provider
        if type(source) is not ProductionRiskStateSource:
            raise ProductionStateError(
                "venue-writing production requires the canonical ProductionRiskStateSource."
            )
        execution_client = getattr(self._execution, "client", None)
        if (
            execution_client is None
            or source._client is not execution_client
            or source._journal is not self._journal
            or source._control is not self._control
            or source._instrument != self._instrument
            or source._environment != source._client.venue_label
        ):
            raise ProductionStateError(
                "canonical risk source is not bound to this runtime's execution client, journal, "
                "control, instrument and environment."
            )
        return source

    @staticmethod
    def _resolve_execution_capability(execution: object) -> ExecutionCapability:
        """Resolve the explicit execution capability, failing closed on anything unknown.

        UNKNOWN CAPABILITY IS NEVER SAFE CAPABILITY: a missing, malformed, contradictory or
        unsupported declaration is refused before the runtime can create venue risk. There is
        deliberately no ``getattr(..., False)``-style permissive fallback.
        """
        declaration = getattr(execution, "capability", None)
        if declaration is None:
            legacy = getattr(execution, "writes_to_venue", None)
            detail = "" if legacy is None else f" (legacy writes_to_venue={legacy!r})"
            raise NonDurableExecutionError(
                "the injected execution port has no explicit ExecutionCapability declaration"
                + detail
                + "; unknown capability is never treated as safe."
            )
        if not isinstance(declaration, ExecutionCapability):
            raise NonDurableExecutionError(
                f"the injected execution port declares a malformed capability {declaration!r}; "
                "refusing to guess its venue-write semantics."
            )
        if isinstance(execution, DurableVenueWritePort):
            if declaration is not ExecutionCapability.DURABLE_VENUE_WRITING:
                raise NonDurableExecutionError(
                    "the injected execution port inherits the approved durable submission "
                    f"contract but declares capability {declaration.value!r}; refusing the "
                    "contradictory declaration."
                )
            return declaration
        if declaration is ExecutionCapability.VENUE_WRITING_UNPROVEN:
            raise NonDurableExecutionError(
                "the injected execution port declares venue writing without the approved durable "
                "submission boundary; it can never be used for production execution."
            )
        return declaration

    def _blocked(
        self,
        intent: ProductionIntent,
        stages: list[str],
        reason_code: str,
        controller: SafetyController,
        now: datetime,
    ) -> IntentPreparation:
        return IntentPreparation(
            intent_id=intent.intent_id,
            correlation_id=intent.correlation_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            time_in_force=intent.time_in_force,
            strategy_id=intent.strategy_id,
            approved=False,
            stages=tuple(stages),
            blocked_reason_code=reason_code,
            safety_mode=controller.mode.value,
            prepared_at=now,
        )

    def _require_startup_posture(self, controller: SafetyController) -> None:
        if controller.kill_switch_engaged:
            self._last_refusal = "kill_switch_engaged"
            raise ProductionStartupError(
                "startup refused: the persisted safety state has the kill switch engaged."
            )
        if controller.mode not in {SafetyMode.NORMAL, SafetyMode.REDUCE_ONLY}:
            self._last_refusal = f"safety_mode_{controller.mode.value}"
            raise ProductionStartupError(
                "startup refused: the persisted safety posture is "
                f"{controller.mode.value!r}, which does not permit the production writer. "
                "Reconcile, acknowledge and restore an allowed posture through the operator "
                "break-glass channel first."
            )
        epoch = self._control.load_epoch()
        if epoch is not None and epoch.is_open:
            self._last_refusal = "break_glass_epoch_open"
            raise ProductionStartupError(
                f"startup refused: break-glass epoch {epoch.session_id} is open."
            )

    def _safety_transition_anchor(self) -> tuple[int, UUID | None]:
        """Return the append-only identity of accepted operator safety transitions."""
        transitions = tuple(
            record
            for record in self._control.load_ledger().records
            if record.accepted
            and record.previous_safety_state != record.resulting_safety_state
        )
        return (
            len(transitions),
            None if not transitions else transitions[-1].event_id,
        )

    def _require_startup_safety_anchor(self, expected: tuple[int, UUID | None]) -> None:
        if self._safety_transition_anchor() != expected:
            self._last_refusal = "safety_state_changed_during_startup"
            raise ProductionStartupError(
                "startup refused: the safety state changed during startup."
            )

    def _require_running(self) -> None:
        if self._state is not ExecutionRuntimeState.RUNNING:
            raise ProductionStateError(
                f"the production runtime is {self._state.value}; start() must succeed first."
            )

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ProductionStateError("the production runtime clock must return aware UTC time.")
        return now.astimezone(timezone.utc)
