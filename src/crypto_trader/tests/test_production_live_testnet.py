"""Phase 14: LIVE Binance SPOT TESTNET validation of the production runtime (opt-in).

Skipped unless BOTH conditions hold:

* ``CRYPTO_TRADER_RUN_TESTNET=1`` (explicit opt-in), and
* ``BINANCE_TESTNET_API_KEY`` / ``BINANCE_TESTNET_API_SECRET`` are present in the environment.

Scope: the documented Spot Testnet host only, test funds only, one tiny resting limit order that the
break-glass safety path cancels before the test ends. NO mainnet endpoint, no real capital, no
withdrawal and no liquidation. Credential values are read from the environment by the Phase 11
adapter and are never printed, journaled or asserted into a failure message.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from crypto_trader.breakglass import (
    BinanceSpotTestnetEmergencyControl,
    BreakGlassControl,
    StaleWriterFenceError,
    parse_operator_command,
)
from crypto_trader.execution import (
    ExecutionOrderType,
    ExecutionSide,
    ExecutionTimeInForce,
    InstrumentSpec,
)
from crypto_trader.execution.binance_testnet import (
    BinanceSpotTestnetClient,
    RequestsTransport,
    TestnetOrderRuntime as OrderRuntime,
    WithdrawalPolicy,
    credentials_from_environment,
)
# Aliased so pytest never mistakes the adapter's classes for test classes.
from crypto_trader.execution.binance_testnet import TestnetClock as SpotClock
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import (
    ProductionIntent,
    ProductionRuntime,
    resolve_environment,
)
from crypto_trader.recovery import load_policy as load_recovery_policy
from crypto_trader.risk import load_limits
from crypto_trader.validity import (
    DecisionValidityGate,
    LiveMarketStateProvider,
    MarketObservation,
)
from crypto_trader.validity import load_policy as load_validity_policy

RUN_FLAG = "CRYPTO_TRADER_RUN_TESTNET"
SYMBOL = "BTCUSDT"
INSTRUMENT = InstrumentSpec(
    symbol=SYMBOL, base_currency="BTC", starting_balance="100000 USDT"
)
LIVE_POLICY_ID = "production-runtime-live-v1"

#: Live-validation limits (TEST configuration only, deliberately small and explicit).
LIVE_LIMITS = {
    "limits_version": "risk-production-live-test-v1",
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


def live_run_enabled() -> bool:
    if os.environ.get(RUN_FLAG) != "1":
        return False
    return bool(os.environ.get("BINANCE_TESTNET_API_KEY")) and bool(
        os.environ.get("BINANCE_TESTNET_API_SECRET")
    )


pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        not live_run_enabled(),
        reason=f"live validation requires {RUN_FLAG}=1 plus testnet credentials",
    ),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _command(command_type: str, *, seed: str, reason: str):
    return parse_operator_command(
        {
            "command_type": command_type,
            "command_id": str(uuid4()),
            "operator_label": "jamal",
            "reason": f"{reason} [{seed}]",
            "source": "operator-cli-live-testnet",
        },
        delivered_at=_now(),
    )


def _live_market_state(client: BinanceSpotTestnetClient) -> LiveMarketStateProvider:
    def latest(symbol: str) -> MarketObservation:
        stamp = _now()
        return MarketObservation(
            symbol=symbol,
            price=client.price(symbol),
            market_event_ts=stamp,
            local_ingest_ts=stamp,
            market_reference=f"live-{stamp.timestamp()}",
        )

    return LiveMarketStateProvider(latest)


def _resting_limit_terms(client: BinanceSpotTestnetClient) -> tuple[Decimal, Decimal, Decimal]:
    """A tiny resting BUY that cannot cross the market, plus the market reference price."""
    rules = client.symbol_rules(SYMBOL)
    market = client.price(SYMBOL)
    tick = rules.price_tick.normalize()
    step = rules.quantity_step.normalize()
    raw_price = market * Decimal("0.9")
    limit_price = ((raw_price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick).quantize(tick)
    minimum_quantity = max(rules.min_notional / limit_price, rules.min_quantity)
    quantity = ((minimum_quantity / step).to_integral_value(rounding=ROUND_CEILING) * step).quantize(
        step
    )
    assert quantity * limit_price >= rules.min_notional
    return market, limit_price, quantity


def _intent(*, market: Decimal, limit_price: Decimal, quantity: Decimal) -> ProductionIntent:
    stamp = _now()
    return ProductionIntent(
        correlation_id=uuid4(),
        symbol=SYMBOL,
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=quantity,
        strategy_id="strategy.production-live-test",
        decision_started_at=stamp,
        decision_completed_at=stamp,
        market_event_ts=stamp,
        reference_price=market,
        market_reference=f"live-{stamp.timestamp()}",
        limit_price=limit_price,
        time_in_force=ExecutionTimeInForce.GTC,
    )


@pytest.fixture
def live_context(tmp_path: Path):
    """A real testnet client wired through the REAL production runtime and its writer fence."""
    client = BinanceSpotTestnetClient(
        credentials=credentials_from_environment(),
        transport=RequestsTransport(timeout_seconds=15.0),
        clock=SpotClock(time_source=time.time, recv_window_ms=5_000),
        symbol=SYMBOL,
        withdrawal_policy=WithdrawalPolicy.RECORD_ONLY,
    )
    journal = SQLiteEventJournal(tmp_path / "journal.db")
    state_dir = tmp_path / "state"
    port = OrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    venue = BinanceSpotTestnetEmergencyControl(
        client=client, runtime=port, withdrawal_policy=WithdrawalPolicy.RECORD_ONLY
    )
    control = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=venue,
        reconciliation_policy=load_recovery_policy({"policy_id": LIVE_POLICY_ID}),
        clock=_now,
    )
    runtime = ProductionRuntime(
        journal=journal,
        state_dir=state_dir,
        instrument=INSTRUMENT,
        execution=port,
        validity_gate=DecisionValidityGate(
            load_validity_policy(
                {
                    "policy_id": LIVE_POLICY_ID,
                    "max_age_seconds": 120,
                    "max_market_move_bps": "200",
                }
            )
        ),
        market_state=_live_market_state(client),
        limits=load_limits(LIVE_LIMITS),
        risk_state_provider=lambda now: _flat_risk_state(now),
        risk_state_max_age_seconds=5.0,
        reconciliation_policy=load_recovery_policy({"policy_id": LIVE_POLICY_ID}),
        control=control,
        clock=_now,
        owner_id="normal:production-live-test",
    )
    yield runtime, control, client, journal
    try:
        client.cancel_all_open_orders(SYMBOL)
    except Exception:  # pragma: no cover - best-effort cleanup of a testnet-only order
        pass
    try:
        port.stop()
    finally:
        journal.close()


def _flat_risk_state(now: datetime):
    from crypto_trader.risk import RiskStateSnapshot

    return RiskStateSnapshot(
        equity_quote=Decimal("100000"),
        peak_equity_quote=Decimal("100000"),
        realized_pnl_quote=Decimal("0"),
        gross_exposure_quote=Decimal("0"),
        net_exposure_quote=Decimal("0"),
        open_orders=0,
        open_positions=0,
        snapshot_ts=now,
    )


def test_live_runtime_claims_the_normal_lease_and_verifies_the_testnet_venue(live_context):
    runtime, control, _client, _journal = live_context

    report = runtime.start()

    decision = resolve_environment()
    assert decision.is_testnet is True
    assert report.environment == "testnet"
    assert report.base_url.endswith("binance.vision")
    assert report.safety_mode == "normal"
    assert report.fence_term >= 1
    assert control.status().new_risk_permitted is True
    assert control.ownership_store.require_fence().owner_kind.value == "normal"


def test_live_new_risk_is_fenced_and_the_break_glass_path_removes_the_resting_order(live_context):
    runtime, control, client, journal = live_context
    runtime.start()

    market, limit_price, quantity = _resting_limit_terms(client)
    first = _intent(market=market, limit_price=limit_price, quantity=quantity)
    outcome = runtime.execute_intent(first)

    assert outcome.submitted is True, "the production runtime did not submit the testnet order"
    reference = outcome.submission.order_reference
    resting = {order.client_order_id for order in client.open_orders(SYMBOL)}
    assert reference in resting, "the submitted testnet order is not resting at the venue"

    # A second intent is approved by validity + Hard Risk, then the operator takes over.
    market, limit_price, quantity = _resting_limit_terms(client)
    second = _intent(market=market, limit_price=limit_price, quantity=quantity)
    preparation = runtime.prepare_intent(second)
    assert preparation.approved is True
    control.handle(_command("halt_new_risk", seed="live-halt", reason="live fencing validation"))

    with pytest.raises(StaleWriterFenceError):
        runtime.submit_prepared(preparation)
    second_reference = f"ct-{second.intent_id.hex[:20]}"
    assert second_reference not in {order.client_order_id for order in client.open_orders(SYMBOL)}

    cancel = control.handle(
        _command("cancel_open", seed="live-cancel", reason="cancel open orders at the venue")
    )
    assert cancel.venue_control == "confirmed"
    assert reference not in {order.client_order_id for order in client.open_orders(SYMBOL)}
    assert runtime.status()["new_risk_permitted"] is False

    # Reconciliation + acknowledgement are mandatory before normal can be restored.
    control.handle(_command("reconcile", seed="live-reconcile", reason="reconcile after emergency"))
    if control.status().reconciliation_status == "resolved":
        control.handle(_command("acknowledge", seed="live-ack", reason="operator acknowledgement"))
        restored = control.handle(
            _command("restore_normal", seed="live-restore", reason="restore normal")
        )
        assert restored.accepted

    rendered = json.dumps(
        {
            "status": runtime.status(),
            "startup": runtime.startup_report().to_record(),
            "submission": outcome.submission.to_record(),
            "journal": [event.to_json() for event in journal.read_range()],
        }
    )
    for secret in (
        os.environ.get("BINANCE_TESTNET_API_KEY", ""),
        os.environ.get("BINANCE_TESTNET_API_SECRET", ""),
    ):
        if secret:
            assert secret not in rendered, "a credential value reached an observable surface"


def test_live_new_risk_after_takeover_is_refused_by_the_runtime(live_context):
    """A halted posture must refuse new risk through the production runtime, not just the control."""
    runtime, control, client, _journal = live_context
    runtime.start()
    control.handle(_command("halt_new_risk", seed="live-halt-2", reason="halt before new risk"))

    market, limit_price, quantity = _resting_limit_terms(client)
    blocked = runtime.execute_intent(
        _intent(market=market, limit_price=limit_price, quantity=quantity)
    )

    assert blocked.submitted is False
    assert blocked.blocked_reason_code
    assert control.status().new_risk_permitted is False


def test_live_production_environment_stays_disabled(live_context):
    """The production environment remains refused on a machine holding a valid testnet key."""
    from crypto_trader.production import MainnetDisabledError

    with pytest.raises(MainnetDisabledError):
        resolve_environment("mainnet")
