"""The mainnet read-only dry-run pipeline (Phase 15).

The complete decision pipeline runs against real mainnet market/account reads, and the final step is
a HARD NO-SUBMIT gate rather than a venue call::

    Market snapshot -> decision -> Trade Intent -> Decision Validity -> Deterministic Hard Risk
    -> safety / writer-fence gate -> OMS dry-run representation -> HARD NO-SUBMIT GATE

The result records WOULD_SUBMIT or WOULD_REJECT and never touches a mainnet order endpoint. The
permission profile is re-validated at every would-be execution boundary, because the key's
permissions can be changed externally without the key string changing. Every cycle is counted in the
durable soak state.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import math
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from crypto_trader.breakglass import BreakGlassControl, StaleWriterFenceError
from crypto_trader.execution import (
    ExecutionOrderType,
    ExecutionSide,
    ExecutionTimeInForce,
    ExecutionRequest,
    InstrumentSpec,
)
from crypto_trader.persistence import EventJournal
from crypto_trader.production import (
    ProductionFence,
    ProductionIntent,
    ProductionSafetyBlockedError,
    TradingEnvironment,
    production_control,
)
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
    MarketObservation,
    MarketStateProvider,
    ValidityRequest,
)

from .envelope import (
    CANARY_ENTRY_ORDER_TYPE,
    CANARY_ENTRY_TIME_IN_FORCE,
    EXECUTION_PRICE_RULES,
    ExecutionPriceRules,
    NoChasingPolicy,
    absolute_bound_price,
    build_canary_entry,
    require_admissible_spread,
    require_within_absolute_bound,
)
from .errors import (
    CanaryPolicyError,
    DryRunError,
    OldSnapshotReuseError,
    SymbolFilterError,
)
from .filters import SymbolFilters
from .nosubmit import HardNoSubmitGate, assert_no_mutation_surface, parse_dry_run_mode
from .permissions import ApiRestrictions
from .soak import SoakRecorder, SoakStateStore

__all__ = [
    "DryRunCycleResult",
    "DryRunDisposition",
    "MainnetDryRunPipeline",
    "OmsDryRunOrder",
]

_SIDE_TO_RISK = {
    ExecutionSide.BUY: RiskSide.BUY,
    ExecutionSide.SELL: RiskSide.SELL,
}


class DryRunDisposition(str, Enum):
    WOULD_SUBMIT = "WOULD_SUBMIT"
    WOULD_REJECT = "WOULD_REJECT"


@dataclass(frozen=True)
class OmsDryRunOrder:
    """The OMS dry-run representation of an order that must never be sent."""

    order_reference: str
    symbol: str
    side: ExecutionSide
    order_type: str
    time_in_force: str
    quantity: Decimal
    limit_price: Decimal
    reference_mid: Decimal
    market_snapshot_id: str
    spread_bps: Decimal
    execution_deviation_bps: Decimal
    within_normal_deviation: bool
    absolute_bound_price: Decimal
    submitted: bool = False

    def to_record(self) -> dict[str, object]:
        return {
            "order_reference": self.order_reference,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "quantity": str(self.quantity),
            "limit_price": str(self.limit_price),
            "reference_mid": str(self.reference_mid),
            "market_snapshot_id": self.market_snapshot_id,
            "spread_bps": str(self.spread_bps),
            "execution_deviation_bps": str(self.execution_deviation_bps),
            "within_normal_deviation": self.within_normal_deviation,
            "absolute_bound_price": str(self.absolute_bound_price),
            "submitted": self.submitted,
            "note": "OMS dry-run representation only; no venue order endpoint was called",
        }


@dataclass(frozen=True)
class DryRunCycleResult:
    """The complete, secret-free result of one dry-run pipeline cycle."""

    cycle_index: int
    symbol: str
    disposition: DryRunDisposition
    reason_code: str
    stages: tuple[str, ...]
    snapshot_reference: str | None
    order: OmsDryRunOrder | None
    mainnet_mutation_requests: int
    recorded_at: datetime

    @property
    def would_submit(self) -> bool:
        return self.disposition is DryRunDisposition.WOULD_SUBMIT

    def to_record(self) -> dict[str, object]:
        return {
            "cycle_index": self.cycle_index,
            "symbol": self.symbol,
            "disposition": self.disposition.value,
            "reason_code": self.reason_code,
            "stages": list(self.stages),
            "snapshot_reference": self.snapshot_reference,
            "order": None if self.order is None else self.order.to_record(),
            "mainnet_mutation_requests": self.mainnet_mutation_requests,
            "recorded_at": self.recorded_at.astimezone(timezone.utc).isoformat(),
        }


class MainnetDryRunPipeline:
    """The single ingress from a market snapshot to a WOULD_SUBMIT/WOULD_REJECT decision."""

    def __init__(
        self,
        *,
        journal: EventJournal,
        state_dir: str | Path,
        instrument: InstrumentSpec,
        market_state: MarketStateProvider,
        decision_source: Callable[[MarketObservation], ProductionIntent | None],
        limits: RiskLimits,
        risk_state_provider: Callable[[datetime], RiskStateSnapshot],
        risk_state_max_age_seconds: float,
        validity_gate: DecisionValidityGate,
        reconciliation_policy: ReconciliationPolicy,
        permission_check: Callable[[], ApiRestrictions],
        gate: HardNoSubmitGate,
        client: Any | None = None,
        filters_provider: Callable[[str], SymbolFilters] | None = None,
        quote_provider: Callable[[str], tuple[Decimal, Decimal]] | None = None,
        soak: SoakRecorder | None = None,
        no_chasing: NoChasingPolicy | None = None,
        price_rules: ExecutionPriceRules = EXECUTION_PRICE_RULES,
        mode: str | None = None,
        owner_id: str = "normal:mainnet-readonly-dryrun",
        control: BreakGlassControl | None = None,
        venue_sources: Sequence[VenueStateSource] = (),
        clock: Callable[[], datetime] | None = None,
        producer: str = "application.mainnet.dryrun",
    ) -> None:
        if not isinstance(instrument, InstrumentSpec):
            raise DryRunError("the dry-run pipeline requires an InstrumentSpec.")
        if not isinstance(limits, RiskLimits):
            raise DryRunError("the dry-run pipeline requires explicit RiskLimits.")
        if not callable(decision_source):
            raise DryRunError("the dry-run pipeline requires an injected decision source.")
        if not callable(permission_check):
            raise DryRunError("the dry-run pipeline requires a permission check.")
        if not isinstance(gate, HardNoSubmitGate):
            raise DryRunError("the dry-run pipeline requires the hard no-submit gate.")
        if not callable(risk_state_provider):
            raise DryRunError("the dry-run pipeline requires a risk-state provider.")
        if (
            isinstance(risk_state_max_age_seconds, bool)
            or not isinstance(risk_state_max_age_seconds, (int, float))
            or not math.isfinite(float(risk_state_max_age_seconds))
            or risk_state_max_age_seconds <= 0
        ):
            raise DryRunError(
                "the dry-run pipeline requires an explicit finite positive "
                "risk_state_max_age_seconds."
            )
        self._journal = journal
        self._state_dir = Path(state_dir)
        self._instrument = instrument
        self._market_state = market_state
        self._decision_source = decision_source
        self._limits = limits
        self._risk_state_provider = risk_state_provider
        self._risk_state_max_age_seconds = float(risk_state_max_age_seconds)
        self._validity_gate = validity_gate
        self._reconciliation_policy = reconciliation_policy
        self._permission_check = permission_check
        self._gate = gate
        self._client = client
        self._filters_provider = filters_provider
        self._quote_provider = quote_provider
        self._soak = soak
        self._no_chasing = no_chasing if no_chasing is not None else NoChasingPolicy()
        self._price_rules = price_rules
        self._mode = parse_dry_run_mode(mode)
        self._owner_id = owner_id
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._producer = producer
        self._pending = PendingIntentRegistry()
        self._venue_sources = tuple(venue_sources)
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
        self._fence: ProductionFence | None = None
        self._ready = False
        self._cycles = 0
        self._last_result: DryRunCycleResult | None = None

    # --- observability -------------------------------------------------------

    @property
    def control(self) -> BreakGlassControl:
        return self._control

    @property
    def gate(self) -> HardNoSubmitGate:
        return self._gate

    @property
    def mode(self) -> str:
        return self._mode.value

    def is_ready(self) -> bool:
        return self._ready

    def status(self) -> dict[str, object]:
        posture = self._control.status()
        return {
            "mode": self._mode.value,
            "ready": self._ready,
            "cycles": self._cycles,
            "environment": TradingEnvironment.MAINNET.value,
            "permission_scope": "read_only",
            "safety_mode": posture.safety_mode,
            "kill_switch_engaged": posture.kill_switch_engaged,
            "epoch_open": posture.epoch_open,
            "new_risk_permitted": posture.new_risk_permitted,
            "no_submit_gate": "armed",
            "mainnet_mutation_attempts": self._gate.mutation_attempts,
            "last_disposition": (
                None if self._last_result is None else self._last_result.disposition.value
            ),
            "last_reason_code": None if self._last_result is None else self._last_result.reason_code,
        }

    # --- lifecycle -----------------------------------------------------------

    def start(self, *, correlation_id: UUID | None = None) -> dict[str, object]:
        """Fail-closed startup for the mainnet read-only environment."""
        if self._ready:
            raise DryRunError("the dry-run pipeline is already started.")
        now = self._now()
        run_id = correlation_id or uuid4()
        if self._client is not None:
            assert_no_mutation_surface(self._client)
        assert_no_mutation_surface(self._gate)

        # 1. The strict permission profile is verified BEFORE any authenticated activity.
        restrictions = self._permission_check()
        if not isinstance(restrictions, ApiRestrictions) or not restrictions.is_read_only:
            raise DryRunError("the mainnet key did not pass the strict read-only profile.")

        # 2. Persisted safety state must permit a normal writer.
        controller = self._control.load_safety_state().controller
        if controller.kill_switch_engaged or controller.mode is not SafetyMode.NORMAL:
            raise DryRunError(
                f"the persisted safety posture {controller.mode.value!r} does not permit a dry-run "
                "writer; reconcile, acknowledge and restore normal first."
            )
        epoch = self._control.load_epoch()
        if epoch is not None and epoch.is_open:
            raise DryRunError("a break-glass epoch is open; refusing to start the dry-run.")

        # 3. Journal/recovery: unresolved order state blocks the dry-run.
        reconstructed, _ = reconstruct_order_states(self._journal)
        recovery = RestartRecovery(
            engine=ReconciliationEngine(self._reconciliation_policy),
            journal=self._journal,
            venue_sources=list(self._venue_sources)
            or [StaticVenueStateSource(entries=(), name="mainnet.no-venue-evidence")],
            producer=self._producer,
        )
        run = recovery.run(now=now, local_states=reconstructed.states(), correlation_id=run_id)
        if not run.report.is_fully_resolved:
            raise DryRunError(
                "order state is not fully reconciled; the dry-run refuses to start while "
                f"{sorted(run.report.unresolved_orders)} are unresolved."
            )

        # 4. Claim the single normal writer lease and bind the read-only fence to it.
        guard = self._control.claim_normal_writer(
            owner_id=self._owner_id, reason_code="mainnet_dryrun_started"
        )
        self._fence = ProductionFence(
            control=self._control,
            guard=guard,
            owner_id=self._owner_id,
            environment=TradingEnvironment.MAINNET,
        )
        self._fence.authorize_new_risk(now=now, correlation_id=run_id)
        self._ready = True
        return {
            "mode": self._mode.value,
            "environment": TradingEnvironment.MAINNET.value,
            "permission_scope": "read_only",
            "owner_id": self._owner_id,
            "fence_term": self._fence.fence_term,
            "safety_mode": controller.mode.value,
            "reconciled_orders": len(reconstructed.states()),
            "started_at": now.isoformat(),
            "no_submit_gate": "armed",
        }

    # --- the pipeline --------------------------------------------------------

    def run_cycle(self) -> DryRunCycleResult:
        """Run one complete pipeline cycle and record WOULD_SUBMIT or WOULD_REJECT."""
        if not self._ready or self._fence is None:
            raise DryRunError("the dry-run pipeline must be started before running a cycle.")
        now = self._now()
        self._cycles += 1
        symbol = self._instrument.symbol
        stages: list[str] = []

        snapshot = self._market_state.latest(symbol)
        if snapshot is None:
            return self._finish(
                symbol=symbol,
                disposition=DryRunDisposition.WOULD_REJECT,
                reason_code="market_state_unavailable",
                stages=tuple(stages + ["market_snapshot:unavailable"]),
                snapshot_reference=None,
                order=None,
                now=now,
                completed=False,
            )
        stages.append("market_snapshot")

        intent = self._decision_source(snapshot)
        if intent is None:
            return self._finish(
                symbol=symbol,
                disposition=DryRunDisposition.WOULD_REJECT,
                reason_code="no_decision",
                stages=tuple(stages + ["decision:none"]),
                snapshot_reference=snapshot.market_reference,
                order=None,
                now=now,
                completed=False,
            )
        stages.append("decision")
        if intent.market_reference != snapshot.market_reference:
            # A decision/intent produced against a DIFFERENT snapshot (for example one created
            # before a stream disconnect) is never executable: a fresh post-disconnect snapshot
            # and a fresh decision/validity/risk pass are required.
            return self._finish(
                symbol=symbol,
                disposition=DryRunDisposition.WOULD_REJECT,
                reason_code="decision_snapshot_mismatch",
                stages=tuple(stages + ["decision:snapshot_mismatch"]),
                snapshot_reference=snapshot.market_reference,
                order=None,
                now=now,
                completed=True,
            )
        controller = self._control.load_safety_state().controller

        # Decision validity (Phase 6).
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
        )
        stages.append(f"validity:{validity.disposition.value}")
        if validity.disposition is not Disposition.ACCEPTED:
            return self._finish(
                symbol=symbol,
                disposition=DryRunDisposition.WOULD_REJECT,
                reason_code=validity.reason_code,
                stages=tuple(stages),
                snapshot_reference=snapshot.market_reference,
                order=None,
                now=now,
                completed=True,
                validity_rejected=True,
            )

        # Deterministic Hard Risk (Phase 7), under the CURRENT persisted safety mode.
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
            if self._soak is not None and (
                {"trading_halted", "reduce_only_violation"} & set(decision.reason_codes)
            ):
                # A halted/reduce-only posture refused the cycle: that IS a halt exercise.
                self._soak.record_halt_exercise()
            return self._finish(
                symbol=symbol,
                disposition=DryRunDisposition.WOULD_REJECT,
                reason_code=",".join(decision.reason_codes),
                stages=tuple(stages),
                snapshot_reference=snapshot.market_reference,
                order=None,
                now=now,
                completed=True,
            )

        # Would-be execution boundary: the permission profile is re-validated here.
        restrictions = self._permission_check()
        if not restrictions.is_read_only:
            return self._finish(
                symbol=symbol,
                disposition=DryRunDisposition.WOULD_REJECT,
                reason_code="permission_profile_no_longer_read_only",
                stages=tuple(stages + ["permissions:changed"]),
                snapshot_reference=snapshot.market_reference,
                order=None,
                now=now,
                completed=True,
            )
        stages.append("permissions:read_only")

        # Safety / writer-fence gate (Phase 13/14), re-validated before the OMS representation.
        try:
            self._fence.authorize_new_risk(now=now, correlation_id=intent.correlation_id)
        except StaleWriterFenceError:
            # Distinguish "the operator halted/took over" from "a competing normal writer claimed
            # the lease": both make this writer stale, but only the first is a halt exercise.
            posture = self._control.load_safety_state().controller
            epoch = self._control.load_epoch()
            halted = (
                not posture.allow_new_risk()
                or posture.kill_switch_engaged
                or (epoch is not None and epoch.is_open)
            )
            if self._soak is not None:
                if halted:
                    self._soak.record_halt_exercise()
                else:
                    self._soak.record_stale_fence_rejection()
            return self._finish(
                symbol=symbol,
                disposition=DryRunDisposition.WOULD_REJECT,
                reason_code="stale_writer_fence",
                stages=tuple(stages + ["safety:stale_fence"]),
                snapshot_reference=snapshot.market_reference,
                order=None,
                now=now,
                completed=True,
            )
        except ProductionSafetyBlockedError as exc:
            reason = str(exc).split(":", 1)[0] or "safety_blocked"
            if self._soak is not None:
                self._soak.record_halt_exercise()
            return self._finish(
                symbol=symbol,
                disposition=DryRunDisposition.WOULD_REJECT,
                reason_code=f"safety_blocked:{reason}",
                stages=tuple(stages + ["safety:blocked"]),
                snapshot_reference=snapshot.market_reference,
                order=None,
                now=now,
                completed=True,
            )
        stages.append("safety:fence_authorized")

        # Dynamic filters + price/quality rules + the future canary entry shape.
        try:
            filters = self._symbol_filters(symbol)
            limit_price = intent.limit_price if intent.limit_price is not None else intent.reference_price
            filters.validate_order(price=limit_price, quantity=intent.quantity)
            stages.append("filters:validated")

            best_bid, best_ask = self._quote(symbol)
            observed_spread = require_admissible_spread(best_bid, best_ask, self._price_rules)
            stages.append("spread:admissible")
            reference_mid = (best_bid + best_ask) / Decimal(2)
            require_within_absolute_bound(
                reference_mid, limit_price, intent.side, self._price_rules
            )
            self._no_chasing.consume(snapshot.market_reference)
            build_canary_entry(
                symbol=symbol,
                side=intent.side,
                quantity=intent.quantity,
                limit_price=limit_price,
                reference_mid=reference_mid,
                market_snapshot_id=snapshot.market_reference,
                # The INTENT's own type/TIF are validated: the policy is never substituted in.
                order_type=intent.order_type.value.upper(),
                time_in_force=intent.time_in_force.value.upper(),
            )
            stages.append("canary_entry:limit_fok")
        except OldSnapshotReuseError:
            return self._finish(
                symbol=symbol,
                disposition=DryRunDisposition.WOULD_REJECT,
                reason_code="old_market_snapshot_reuse_refused",
                stages=tuple(stages + ["snapshot:reuse_refused"]),
                snapshot_reference=snapshot.market_reference,
                order=None,
                now=now,
                completed=True,
            )
        except SymbolFilterError as exc:
            return self._finish(
                symbol=symbol,
                disposition=DryRunDisposition.WOULD_REJECT,
                reason_code=f"filter_rejected:{type(exc).__name__}",
                stages=tuple(stages + ["filters:rejected"]),
                snapshot_reference=snapshot.market_reference,
                order=None,
                now=now,
                completed=True,
            )
        except CanaryPolicyError as exc:
            return self._finish(
                symbol=symbol,
                disposition=DryRunDisposition.WOULD_REJECT,
                reason_code=f"canary_policy:{type(exc).__name__}",
                stages=tuple(stages + ["canary_policy:rejected"]),
                snapshot_reference=snapshot.market_reference,
                order=None,
                now=now,
                completed=True,
            )

        deviation_bps = (
            abs(limit_price - reference_mid) / reference_mid * Decimal(10000)
        )
        order_reference = f"dry-{intent.intent_id.hex[:16]}"
        order = OmsDryRunOrder(
            order_reference=order_reference,
            symbol=symbol,
            side=intent.side,
            order_type=CANARY_ENTRY_ORDER_TYPE,
            time_in_force=CANARY_ENTRY_TIME_IN_FORCE,
            quantity=intent.quantity,
            limit_price=limit_price,
            reference_mid=reference_mid,
            market_snapshot_id=snapshot.market_reference,
            spread_bps=observed_spread,
            execution_deviation_bps=deviation_bps,
            within_normal_deviation=deviation_bps <= self._price_rules.max_normal_slippage_bps,
            absolute_bound_price=absolute_bound_price(reference_mid, intent.side, self._price_rules),
        )
        stages.append("oms:dry_run_representation")

        # THE HARD NO-SUBMIT GATE: the pipeline records the intent to submit and stops here.
        disposition = self._gate.guard_would_submit(order_reference)
        stages.append(f"no_submit_gate:{disposition.lower()}")
        return self._finish(
            symbol=symbol,
            disposition=DryRunDisposition.WOULD_SUBMIT,
            reason_code="dry_run_would_submit",
            stages=tuple(stages),
            snapshot_reference=snapshot.market_reference,
            order=order,
            now=now,
            completed=True,
        )

    def try_to_submit(self, order: OmsDryRunOrder) -> None:
        """Negative path: prove a venue submission is unreachable. Always raises."""
        if not isinstance(order, OmsDryRunOrder):
            raise DryRunError("try_to_submit expects an OmsDryRunOrder.")
        request = ExecutionRequest(
            intent_id=uuid4(),
            correlation_id=uuid4(),
            symbol=order.symbol,
            side=order.side,
            order_type=ExecutionOrderType.LIMIT,
            quantity=order.quantity,
            limit_price=order.limit_price,
            time_in_force=ExecutionTimeInForce.FOK,
        )
        self._gate.submit(request)
        raise DryRunError(  # pragma: no cover - the gate always refuses first
            "the hard no-submit gate returned a reference, which must be impossible in this phase."
        )

    # --- internals -----------------------------------------------------------

    def _symbol_filters(self, symbol: str) -> SymbolFilters:
        if self._filters_provider is not None:
            return self._filters_provider(symbol)
        if self._client is not None:
            return self._client.exchange_info(symbol)
        raise DryRunError("no symbol-filter provider is configured.")

    def _quote(self, symbol: str) -> tuple[Decimal, Decimal]:
        if self._quote_provider is not None:
            return self._quote_provider(symbol)
        if self._client is not None:
            return self._client.book_ticker(symbol)
        raise DryRunError("no top-of-book provider is configured.")

    def _finish(
        self,
        *,
        symbol: str,
        disposition: DryRunDisposition,
        reason_code: str,
        stages: tuple[str, ...],
        snapshot_reference: str | None,
        order: OmsDryRunOrder | None,
        now: datetime,
        completed: bool,
        validity_rejected: bool = False,
    ) -> DryRunCycleResult:
        if self._soak is not None:
            self._soak.record_cycle(completed=completed, validity_rejected=validity_rejected)
        result = DryRunCycleResult(
            cycle_index=self._cycles,
            symbol=symbol,
            disposition=disposition,
            reason_code=reason_code,
            stages=stages,
            snapshot_reference=snapshot_reference,
            order=order,
            mainnet_mutation_requests=self._gate.mutation_attempts,
            recorded_at=now,
        )
        self._last_result = result
        return result

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise DryRunError("the dry-run clock must return timezone-aware time.")
        return now.astimezone(timezone.utc)
