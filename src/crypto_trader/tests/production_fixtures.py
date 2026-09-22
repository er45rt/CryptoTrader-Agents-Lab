"""Deterministic fixtures for the Phase 14 production-runtime tests (not a test module).

Every value here is synthetic. The capital/risk numbers below are TEST fixtures, not production
configuration: the production runtime requires explicit limits and invents none.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

from crypto_trader.breakglass import (
    BreakGlassControl,
    OperatorCommandType,
    SafetyStateStore,
    parse_operator_command,
)
from crypto_trader.execution import (
    ExecutionObservation,
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionRuntimeState,
    ExecutionSide,
    ExecutionTimeInForce,
    InstrumentSpec,
)
from crypto_trader.execution.binance_testnet import BinanceSpotTestnetClient, TestnetOrderRuntime
from crypto_trader.persistence import EventJournal
from crypto_trader.production import ProductionIntent, ProductionRuntime
from crypto_trader.recovery import ReconciliationPolicy, load_policy as load_recovery_policy
from crypto_trader.risk import (
    PendingIntentRegistry,
    RiskLimits,
    RiskStateSnapshot,
    load_limits,
)
from crypto_trader.safety import restore_safety_controller
from crypto_trader.validity import (
    DecisionValidityGate,
    MarketObservation,
    ReplayMarketStateProvider,
)
from crypto_trader.validity import load_policy as load_validity_policy

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SYMBOL = "BTCUSDT"
PRICE = Decimal("27000.00")
NAMESPACE = UUID("1c7d3a52-9f04-4d21-8b6e-2a5f9c1e3d70")

INSTRUMENT = InstrumentSpec(
    symbol=SYMBOL,
    venue="BINANCE_SPOT_TESTNET",
    base_currency="BTC",
    quote_currency="USDT",
    price_precision=2,
    size_precision=5,
    starting_balance="100000 USDT",
)

#: Synthetic risk-limit fixtures (TEST values only; production limits are an owner decision).
LIMIT_CONFIG = {
    "limits_version": "risk-production-test-v1",
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

RECOVERY_POLICY_ID = "production-runtime-test-v1"


def limits() -> RiskLimits:
    return load_limits(LIMIT_CONFIG)


def recovery_policy() -> ReconciliationPolicy:
    return load_recovery_policy({"policy_id": RECOVERY_POLICY_ID})


def risk_state(*, now: datetime | None = None) -> RiskStateSnapshot:
    return RiskStateSnapshot(
        equity_quote=Decimal("100000"),
        peak_equity_quote=Decimal("100000"),
        realized_pnl_quote=Decimal("0"),
        gross_exposure_quote=Decimal("0"),
        net_exposure_quote=Decimal("0"),
        open_orders=0,
        open_positions=0,
        snapshot_ts=now or BASE,
    )


def risk_state_provider(stamp: datetime | None = None):
    return lambda now: risk_state(now=stamp or now)


def validity_gate() -> DecisionValidityGate:
    return DecisionValidityGate(
        load_validity_policy(
            {
                "policy_id": "production-runtime-test-v1",
                "max_age_seconds": 60,
                "max_market_move_bps": "50",
            }
        )
    )


def market_state(*, price: Decimal = PRICE, when: datetime = BASE) -> ReplayMarketStateProvider:
    return ReplayMarketStateProvider(
        [
            MarketObservation(
                symbol=SYMBOL,
                price=price,
                market_event_ts=when,
                local_ingest_ts=when,
                market_reference="mkt-production-test-1",
                sequence=1,
            )
        ]
    )


def intent(seed: str = "intent-1", **overrides) -> ProductionIntent:
    values: dict[str, object] = {
        "correlation_id": uuid5(NAMESPACE, f"corr:{seed}"),
        "symbol": SYMBOL,
        "side": ExecutionSide.BUY,
        "order_type": ExecutionOrderType.LIMIT,
        "quantity": Decimal("0.01000"),
        "strategy_id": "strategy.production-test",
        "decision_started_at": BASE - timedelta(seconds=1),
        "decision_completed_at": BASE,
        "market_event_ts": BASE,
        "reference_price": PRICE,
        "market_reference": "mkt-production-test-1",
        "intent_id": uuid5(NAMESPACE, seed),
        "limit_price": PRICE,
        "time_in_force": ExecutionTimeInForce.GTC,
    }
    values.update(overrides)
    return ProductionIntent(**values)  # type: ignore[arg-type]


def clock(stamp: datetime = BASE):
    return lambda: stamp


def control_for(
    journal: EventJournal,
    state_dir: Path,
    *,
    when: datetime = BASE,
    venue=None,
) -> BreakGlassControl:
    return BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=venue,
        reconciliation_policy=recovery_policy(),
        clock=clock(when),
    )


def operator_command(
    command_type: OperatorCommandType | str,
    *,
    seed: str,
    requested_at: datetime = BASE,
    **overrides,
):
    payload = {
        "command_type": command_type,
        "command_id": str(uuid5(NAMESPACE, f"command:{seed}")),
        "operator_label": "jamal",
        "reason": "phase 14 production-runtime test",
        "source": "operator-cli",
        "requested_at": requested_at.isoformat(),
    }
    payload.update(overrides)
    return parse_operator_command(payload, delivered_at=requested_at)


def persist_safety_state(
    state_dir: Path,
    *,
    mode: str,
    kill_switch_engaged: bool,
    reason_code: str = "persisted_by_test",
    sequence: int = 1,
) -> None:
    """Write a persisted safety state directly (no journal event), for fail-closed startup tests."""
    controller = restore_safety_controller(
        {
            "schema_version": 1,
            "mode": mode,
            "kill_switch_engaged": kill_switch_engaged,
            "reason_code": reason_code,
            "reason": f"persisted {mode} state for a Phase 14 test",
            "sequence": sequence,
            "session_id": str(uuid5(NAMESPACE, f"session:{mode}:{kill_switch_engaged}")),
        }
    )
    SafetyStateStore(state_dir / "safety_state.json").save(controller)


class RecordingExecutionPort:
    """Deterministic engine-independent execution port double (records, never networks)."""

    def __init__(self, instrument: InstrumentSpec) -> None:
        self._instrument = instrument
        self._state = ExecutionRuntimeState.STOPPED
        self.requests: list[ExecutionRequest] = []
        self.references: list[str] = []
        self.start_calls = 0

    @property
    def submit_count(self) -> int:
        return len(self.requests)

    def start(self) -> None:
        if self._state is ExecutionRuntimeState.RUNNING:
            raise RuntimeError("port already running")
        self.start_calls += 1
        self._state = ExecutionRuntimeState.RUNNING

    def stop(self) -> None:
        self._state = ExecutionRuntimeState.STOPPED

    def state(self) -> ExecutionRuntimeState:
        return self._state

    def observations(self) -> tuple[ExecutionObservation, ...]:
        return ()

    def submit(self, request: ExecutionRequest) -> str:
        if self._state is not ExecutionRuntimeState.RUNNING:
            raise RuntimeError("port is not running")
        request.validate_for(self._instrument)
        self.requests.append(request)
        reference = f"PROD-ORD-{request.intent_id.hex[:12]}"
        self.references.append(reference)
        return reference


def build_runtime(
    *,
    journal: EventJournal,
    state_dir: Path,
    execution,
    control: BreakGlassControl | None = None,
    environment=None,
    mainnet_authorized: bool = False,
    mainnet_base_url: str | None = None,
    owner_id: str = "normal:production-runtime",
    risk_state_source=None,
    market=None,
    limits_value: RiskLimits | None = None,
    venue_sources=(),
    pending: PendingIntentRegistry | None = None,
    when: datetime = BASE,
) -> ProductionRuntime:
    return ProductionRuntime(
        journal=journal,
        state_dir=state_dir,
        instrument=INSTRUMENT,
        execution=execution,
        validity_gate=validity_gate(),
        market_state=market or market_state(),
        limits=limits_value or limits(),
        risk_state_provider=risk_state_source or risk_state_provider(),
        reconciliation_policy=recovery_policy(),
        environment=environment,
        mainnet_authorized=mainnet_authorized,
        mainnet_base_url=mainnet_base_url,
        owner_id=owner_id,
        venue_sources=venue_sources,
        control=control,
        clock=clock(when),
        pending=pending,
    )


def testnet_runtime(
    *,
    journal: EventJournal,
    state_dir: Path,
    client: BinanceSpotTestnetClient,
    control: BreakGlassControl | None = None,
    instrument: InstrumentSpec = INSTRUMENT,
) -> tuple[ProductionRuntime, TestnetOrderRuntime]:
    """Wire the production runtime to the real Phase 11 Spot Testnet adapter (scripted transport)."""
    port = TestnetOrderRuntime(client=client, instrument=instrument, journal=journal)
    runtime = build_runtime(
        journal=journal,
        state_dir=state_dir,
        execution=port,
        control=control,
    )
    return runtime, port
