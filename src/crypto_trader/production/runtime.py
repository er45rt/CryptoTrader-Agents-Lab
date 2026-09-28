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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import math
from pathlib import Path
from uuid import UUID, uuid4

from crypto_trader.breakglass import BreakGlassControl, BreakGlassError, WriterGuard
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
from crypto_trader.persistence import EventJournal
from crypto_trader.recovery import (
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

__all__ = [
    "PRODUCTION_PRODUCER",
    "IntentPreparation",
    "ProductionExecutionOutcome",
    "ProductionIntent",
    "ProductionRuntime",
    "ProductionStartupReport",
    "ProductionSubmitResult",
    "production_control",
]

PRODUCTION_PRODUCER = "application.production"

_DEFAULT_OWNER_ID = "normal:production-runtime"

_SIDE_TO_RISK = {
    ExecutionSide.BUY: RiskSide.BUY,
    ExecutionSide.SELL: RiskSide.SELL,
}


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
        if (
            not isinstance(self.quantity, Decimal)
            or not self.quantity.is_finite()
            or self.quantity <= 0
        ):
            raise ProductionStateError("an intent requires a finite positive Decimal quantity.")
        if (
            not isinstance(self.reference_price, Decimal)
            or not self.reference_price.is_finite()
            or self.reference_price <= 0
        ):
            raise ProductionStateError(
                "an intent requires a finite positive Decimal reference price: Hard Risk needs an "
                "explicit price and never guesses one."
            )
        if self.order_type is ExecutionOrderType.LIMIT and (
            not isinstance(self.limit_price, Decimal)
            or not self.limit_price.is_finite()
            or self.limit_price <= 0
        ):
            raise ProductionStateError(
                "a limit intent requires a finite positive Decimal limit price."
            )
        if self.order_type is ExecutionOrderType.MARKET and self.limit_price is not None:
            raise ProductionStateError("a market intent must not carry a limit price.")
        if not isinstance(self.time_in_force, ExecutionTimeInForce):
            raise ProductionStateError("an intent requires an ExecutionTimeInForce.")
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

    @property
    def risk_price(self) -> Decimal:
        """The deterministic price Hard Risk evaluates against (never a guessed market price)."""
        return self.limit_price if self.limit_price is not None else self.reference_price


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
        risk_state_provider: Callable[[datetime], RiskStateSnapshot],
        risk_state_max_age_seconds: float,
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
        if (
            isinstance(risk_state_max_age_seconds, bool)
            or not isinstance(risk_state_max_age_seconds, (int, float))
            or not math.isfinite(float(risk_state_max_age_seconds))
            or risk_state_max_age_seconds <= 0
        ):
            raise ProductionStateError(
                "the production runtime requires an explicit finite positive "
                "risk_state_max_age_seconds."
            )
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ProductionStateError("the production runtime requires a non-empty owner id.")

        self._journal = journal
        self._state_dir = Path(state_dir)
        self._instrument = instrument
        self._execution = execution
        self._validity_gate = validity_gate
        self._market_state = market_state
        self._limits = limits
        self._risk_state_provider = risk_state_provider
        self._risk_state_max_age_seconds = float(risk_state_max_age_seconds)
        self._reconciliation_policy = reconciliation_policy
        self._requested_environment = environment
        self._mainnet_authorized = mainnet_authorized
        self._mainnet_base_url = mainnet_base_url
        self._owner_id = owner_id.strip()
        self._venue_sources = tuple(venue_sources)
        # Stage 3 capability gate (construction-time; capability and journal are construction-time
        # facts, so no later mutation can change this decision).
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

    def status(self) -> dict[str, object]:
        """Read-only runtime posture (never writes, never repairs, never caches an approval)."""
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
        """Fail-closed startup: environment, posture, recovery, then claim the normal lease."""
        if self._state is ExecutionRuntimeState.RUNNING:
            raise ProductionStateError("the production runtime is already running.")
        now = self._now()
        run_id = correlation_id or uuid4()

        # 1. Environment first: a disabled production environment is refused before anything else.
        decision = resolve_environment(
            self._requested_environment,
            mainnet_authorized=self._mainnet_authorized,
            mainnet_base_url=self._mainnet_base_url,
        )

        # 2. Persisted safety state: a normal trading writer requires a normal posture.
        load = self._control.load_safety_state()
        self._require_startup_posture(load.controller)

        # 3. Journal/recovery state: unresolved orders block new risk.
        reconstructed, _rebuild = reconstruct_order_states(self._journal)
        local_states = reconstructed.states()
        recovery = RestartRecovery(
            engine=ReconciliationEngine(self._reconciliation_policy),
            journal=self._journal,
            venue_sources=list(self._venue_sources)
            or [StaticVenueStateSource(entries=(), name="production.no-venue-evidence")],
            producer=self._producer,
        )
        run = recovery.run(now=now, local_states=local_states, correlation_id=run_id)
        if not run.report.is_fully_resolved:
            self._last_refusal = "recovery_required"
            raise ProductionRecoveryRequiredError(
                "startup refused: order state is not fully reconciled (unresolved: "
                f"{sorted(run.report.unresolved_orders)}); new risk stays blocked until evidence "
                "resolves it through Phase 8 reconciliation."
            )

        # 4. Claim the single authoritative normal writer lease (a fresh fencing term).
        try:
            guard = self._control.claim_normal_writer(
                owner_id=self._owner_id, reason_code="production_runtime_started"
            )
        except BreakGlassError as exc:
            self._last_refusal = "writer_lease_unavailable"
            raise ProductionStateError(
                "startup refused: the normal writer lease could not be claimed "
                f"({type(exc).__name__}): {exc}"
            ) from exc

        # 5. Bind the fence to the held token and prove it is current before serving traffic.
        fence = ProductionFence(
            control=self._control,
            guard=guard,
            owner_id=self._owner_id,
            environment=decision.environment,
        )
        check = fence.authorize_new_risk(now=now, correlation_id=run_id)

        # 6. Start the execution boundary. For the Spot Testnet adapter this verifies the endpoint,
        #    the clock and the account's least-privilege permissions (trading enabled, withdrawals
        #    unavailable) BEFORE any order can be attempted; a failure here leaves the runtime
        #    stopped with no traffic.
        try:
            self._execution.start()
        except Exception as exc:  # noqa: BLE001 - the typed error is re-raised unchanged
            self._last_refusal = f"execution_boundary_start_failed:{type(exc).__name__}"
            raise

        self._environment = decision
        self._guard = guard
        self._fence = fence
        self._last_fence = check
        self._last_outcome = None
        self._last_refusal = None
        self._state = ExecutionRuntimeState.RUNNING
        self._startup = ProductionStartupReport(
            environment=decision.environment.value,
            base_url=decision.base_url,
            owner_id=self._owner_id,
            fence_term=check.fence_term,
            safety_mode=check.safety_mode,
            safety_state_source=check.safety_state_source,
            recovered_orders=len(local_states),
            reconciliation_state=run.recovery_state,
            started_at=now,
            journaled_event_ids=tuple(run.journaled_event_ids),
            notes=decision.notes,
        )
        return self._startup

    def stop(self) -> None:
        """Stop serving. The held lease record stays on disk; a restart takes a fresh term."""
        self._execution.stop()
        self._state = ExecutionRuntimeState.STOPPED

    # --- decision path -------------------------------------------------------

    def prepare_intent(
        self,
        intent: ProductionIntent,
        *,
        capacity_available: bool = True,
        latest_scope_intent_id: UUID | None = None,
    ) -> IntentPreparation:
        """Run validity then Hard Risk. Contacts no venue and grants no trading authority."""
        self._require_running()
        if not isinstance(intent, ProductionIntent):
            raise ProductionStateError("prepare_intent expects a ProductionIntent.")
        now = self._now()
        controller = self._control.load_safety_state().controller
        stages: list[str] = []

        # Stage 1: deterministic decision validity / staleness.
        validity = self._validity_gate.evaluate(
            ValidityRequest(
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
            ),
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
            max_state_age_seconds=self._risk_state_max_age_seconds,
        )
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
            self._risk_state_provider(now),
            now=now,
        )
        stages.append("risk:allowed" if decision.allowed else "risk:blocked")
        if not decision.allowed:
            return self._blocked(intent, stages, ",".join(decision.reason_codes), controller, now)

        # Stage 3 is the fence, and it happens in submit_prepared immediately before the venue call.
        stages.append("safety:fence_deferred_to_submit")
        return IntentPreparation(
            intent_id=intent.intent_id,
            correlation_id=intent.correlation_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            time_in_force=intent.time_in_force,
            approved=True,
            stages=tuple(stages),
            blocked_reason_code=None,
            safety_mode=controller.mode.value,
            prepared_at=now,
        )

    def submit_prepared(self, preparation: IntentPreparation) -> ProductionSubmitResult:
        """The final fence check, immediately followed by the venue submission."""
        self._require_running()
        if not isinstance(preparation, IntentPreparation):
            raise ProductionStateError("submit_prepared expects an IntentPreparation.")
        if not preparation.approved:
            raise ProductionSafetyBlockedError(
                "the intent was not approved "
                f"({preparation.blocked_reason_code}); nothing is submitted."
            )
        if self._fence is None:
            raise ProductionStateError("the runtime holds no writer fence; refusing to submit.")

        now = self._now()
        request = ExecutionRequest(
            intent_id=preparation.intent_id,
            correlation_id=preparation.correlation_id,
            symbol=preparation.symbol,
            side=preparation.side,
            order_type=preparation.order_type,
            quantity=preparation.quantity,
            limit_price=preparation.limit_price,
            time_in_force=preparation.time_in_force,
        )
        try:
            # Hold the shared ownership exclusion from the final, fresh fence check through the
            # single venue send. A break-glass/competing takeover therefore linearizes either
            # strictly before this authorization (and blocks it) or strictly after the send.
            with self._fence.submission_guard(
                now=now, correlation_id=preparation.correlation_id
            ) as check:
                self._last_fence = check
                try:
                    # Venue-writing execution goes through the approved durable submission sequence
                    # (marker -> authorization -> one send). Non-writing execution keeps its port.
                    if self._durable_venue_writes:
                        # Construction-time identity is necessary but not sufficient: an adapter
                        # field could be rebound later. Re-prove that the durable marker still
                        # lands in this runtime's authoritative journal at the instant of submit.
                        attempt_journal = self._execution.durable_attempt_journal()
                        if attempt_journal is not self._journal:
                            raise NonDurableExecutionError(
                                "the venue-writing execution port's durable journal binding changed "
                                "after runtime construction; refusing to send any request."
                            )
                        reference = self._execution.submit_durably(request)
                    else:
                        reference = self._execution.submit(request)
                except AmbiguousOrderError:
                    # UNKNOWN_PENDING_RECONCILIATION: preserved exactly, never retried, never masked.
                    self._last_outcome = "unknown_pending_reconciliation"
                    raise
        except ProductionSafetyBlockedError as exc:
            self._last_refusal = str(exc)
            raise
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
    ) -> ProductionExecutionOutcome:
        """Convenience path: prepare then submit. A blocked decision never reaches the venue."""
        preparation = self.prepare_intent(
            intent,
            capacity_available=capacity_available,
            latest_scope_intent_id=latest_scope_intent_id,
        )
        if not preparation.approved:
            return ProductionExecutionOutcome(
                submitted=False,
                preparation=preparation,
                submission=None,
                blocked_reason_code=preparation.blocked_reason_code,
            )
        submission = self.submit_prepared(preparation)
        return ProductionExecutionOutcome(
            submitted=True,
            preparation=preparation,
            submission=submission,
            blocked_reason_code=None,
        )

    # --- internals -----------------------------------------------------------

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
        if controller.mode is not SafetyMode.NORMAL:
            self._last_refusal = f"safety_mode_{controller.mode.value}"
            raise ProductionStartupError(
                "startup refused: the persisted safety posture is "
                f"{controller.mode.value!r}, which does not permit a normal trading writer. "
                "Reconcile, acknowledge and restore normal through the operator break-glass channel "
                "first."
            )
        epoch = self._control.load_epoch()
        if epoch is not None and epoch.is_open:
            self._last_refusal = "break_glass_epoch_open"
            raise ProductionStartupError(
                f"startup refused: break-glass epoch {epoch.session_id} is open."
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
