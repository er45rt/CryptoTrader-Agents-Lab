"""Phase 13: LIVE Binance SPOT TESTNET break-glass validation (opt-in, never in the offline suite).

Skipped unless BOTH conditions hold:

* ``CRYPTO_TRADER_RUN_TESTNET=1`` (explicit opt-in), and
* ``BINANCE_TESTNET_API_KEY`` / ``BINANCE_TESTNET_API_SECRET`` are present in the environment.

Scope: the documented Spot Testnet host only, test funds only, a tiny resting limit order that is
always cancelled by the break-glass path. NO mainnet endpoint, no real capital, no withdrawal, no
liquidation and no push. A testnet outage fails these tests - it is never disguised as an
application PASS, and the offline suite stays green independently.

Credential values are read from the environment by the adapter and are never printed here.

Integration note (retained limitation): the runtime is handed the CURRENT persisted safety state
(and the writer fence is asserted separately), because the Phase 11 runtime loop - which Phase 13
must not modify - does not poll the break-glass state on its own. A live deployment must consult
``BreakGlassControl.normal_writer_guard()`` / the persisted safety state at its decision boundary.
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
    BreakGlassControl,
    StaleWriterFenceError,
    parse_operator_command,
)
from crypto_trader.breakglass.venue import BinanceSpotTestnetEmergencyControl
from crypto_trader.execution import (
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionSide,
    ExecutionTimeInForce,
    InstrumentSpec,
)
from crypto_trader.execution.binance_testnet import (
    BinanceSpotTestnetClient,
    BinanceTestnetError,
    RequestsTransport,
    WithdrawalPolicy,
    credentials_from_environment,
)
from crypto_trader.execution.binance_testnet import TestnetClock as SpotClock
from crypto_trader.execution.binance_testnet import TestnetOrderRuntime as OrderRuntime
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.recovery import load_policy as load_recovery_policy

RUN_FLAG = "CRYPTO_TRADER_RUN_TESTNET"
SYMBOL = "BTCUSDT"
INSTRUMENT = InstrumentSpec(symbol=SYMBOL, base_currency="BTC", starting_balance="100000 USDT")


def live_run_enabled() -> bool:
    """True only when explicitly opted in AND credentials are present (no value inspected)."""
    if os.environ.get(RUN_FLAG) != "1":
        return False
    return bool(os.environ.get("BINANCE_TESTNET_API_KEY")) and bool(
        os.environ.get("BINANCE_TESTNET_API_SECRET")
    )


pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        not live_run_enabled(),
        reason=f"live break-glass validation requires {RUN_FLAG}=1 plus testnet credentials",
    ),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _command(command_type: str, *, seed: str, operator: str = "jamal", reason: str):
    return parse_operator_command(
        {
            "command_type": command_type,
            "command_id": str(uuid4()),
            "operator_label": operator,
            "reason": f"{reason} [{seed}]",
            "source": "operator-cli-live-testnet",
        },
        delivered_at=_now(),
    )


def _resting_limit_request(client: BinanceSpotTestnetClient, *, side: str = "BUY"):
    """A tiny resting limit order that cannot cross the market (it never fills).

    Sizing is computed against the ORDER PRICE (not the market price), because the venue's minimum
    notional filter is evaluated at the limit price. The same sizing discipline as the venue-validated
    Phase 11 live test is used here.
    """
    rules = client.symbol_rules(SYMBOL)
    market = client.price(SYMBOL)
    # The venue reports tickSize/stepSize with trailing zeros (e.g. "0.01000000"); normalizing gives
    # the canonical exponent our boundary precision checks expect.
    tick = rules.price_tick.normalize()
    step = rules.quantity_step.normalize()

    # Stay comfortably INSIDE the venue's PERCENT_PRICE_BY_SIDE band, which is evaluated against
    # the 5-minute average price (BTCUSDT testnet: bid 0.5x-1.2x, ask 0.8x-2.0x). A price at
    # exactly 0.5x/2.0x sits on the edge and is rejected as -1013 filter failure.
    offset = Decimal("0.9") if side == "BUY" else Decimal("1.1")
    raw_price = market * offset
    limit_price = ((raw_price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick).quantize(tick)

    minimum_quantity = max(rules.min_notional / limit_price, rules.min_quantity)
    steps = (minimum_quantity / step).to_integral_value(rounding=ROUND_CEILING)
    quantity = (steps * step).quantize(step)
    assert quantity * limit_price >= rules.min_notional, "sizing must satisfy the notional minimum"
    from crypto_trader.execution.binance_testnet import TestnetOrderRequest as SpotOrderRequest

    return SpotOrderRequest(
        client_order_id=f"ct-bg-live-{uuid4().hex[:12]}",
        symbol=SYMBOL,
        side=side,
        order_type="LIMIT",
        quantity=quantity,
        price=limit_price,
        time_in_force="GTC",
    )


@pytest.fixture
def live_context(tmp_path: Path):
    """A real testnet client, runtime and break-glass control over one journal + state dir."""
    client = BinanceSpotTestnetClient(
        credentials=credentials_from_environment(),
        transport=RequestsTransport(timeout_seconds=15.0),
        clock=SpotClock(time_source=time.time, recv_window_ms=5_000),
        symbol=SYMBOL,
        withdrawal_policy=WithdrawalPolicy.RECORD_ONLY,
    )
    environment = client.verify_environment(symbol=SYMBOL)
    journal = SQLiteEventJournal(tmp_path / "journal.db")
    runtime = OrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    runtime.start()
    venue = BinanceSpotTestnetEmergencyControl(
        client=client, runtime=runtime, withdrawal_policy=WithdrawalPolicy.RECORD_ONLY
    )
    control = BreakGlassControl(
        journal=journal,
        state_dir=tmp_path / "state",
        venue=venue,
        reconciliation_policy=load_recovery_policy({"policy_id": "recovery-breakglass-live-v1"}),
        clock=_now,
    )
    control.claim_normal_writer(owner_id="normal:live-testnet-runtime")
    _refresh_runtime_safety(control, runtime)
    yield control, runtime, client, journal, environment, tmp_path
    try:
        runtime.cancel_all()
    finally:
        runtime.stop()
        journal.close()


def _refresh_runtime_safety(control: BreakGlassControl, runtime: OrderRuntime) -> None:
    """Hand the runtime the CURRENT persisted safety state.

    The Phase 11 runtime consults ``runtime.safety`` at its decision boundary; a live deployment
    reloads that state from the authoritative journal/state store (or consults the writer fence)
    before creating risk. The live test does the same explicitly so the gate under test is the real
    one rather than an unset attribute.
    """
    runtime.safety = control.load_safety_state().controller


def test_live_preflight_confirms_testnet_clock_and_permissions(live_context):
    control, _, _, _, environment, _ = live_context
    record = environment.to_record()

    assert record["venue_label"] == "binance-spot-testnet"
    assert record["host"].endswith("binance.vision")
    assert record["can_trade"] is True
    assert any("no withdrawal endpoint exists" in note for note in record["notes"])
    assert control.status().safety_mode == "normal"
    assert control.status().new_risk_permitted is True


def test_live_halt_blocks_new_risk_and_cancel_open_cancels_through_the_safety_path(live_context):
    control, runtime, client, _, _, _ = live_context
    guard = control.normal_writer_guard()
    assert guard.may_create_new_risk() is True
    venue_request = _resting_limit_request(client)
    # Stage 2: the fresh write goes through the production runtime (prepare -> durable attempt
    # marker -> single send), never through a direct client write.
    reference = runtime.submit(
        ExecutionRequest.create(
            correlation_id=uuid4(),
            symbol=venue_request.symbol,
            side=ExecutionSide.BUY,
            order_type=ExecutionOrderType.LIMIT,
            quantity=venue_request.quantity,
            limit_price=venue_request.price,
            time_in_force=ExecutionTimeInForce.GTC,
            intent_id=uuid4(),
        )
    )
    order = runtime.refresh(reference)
    assert order.is_terminal is False

    halt = control.handle(_command("halt_new_risk", seed="live-1", reason="live validation"))
    assert halt.accepted and halt.resulting_safety_state == "halt_new_risk"
    assert control.status().new_risk_permitted is False

    # (a) the emergency takeover invalidates the normal writer's fencing token.
    fence_refused = False
    try:
        guard.require_current()
    except StaleWriterFenceError:
        fence_refused = True
    assert fence_refused, "the normal writer's fencing token stayed valid after break-glass"

    # (b) a runtime holding the current persisted safety state refuses the new-risk order.
    _refresh_runtime_safety(control, runtime)
    blocked_reason = ""
    try:
        runtime.submit(
            ExecutionRequest.create(
                correlation_id=uuid4(),
                symbol=SYMBOL,
                side=ExecutionSide.BUY,
                order_type=ExecutionOrderType.LIMIT,
                # Expressed within the Phase 4 boundary precision (the venue reports trailing
                # zeros) so that the SAFETY gate - not a precision guard - is what refuses it.
                quantity=order.original_quantity.normalize(),
                limit_price=order.price.quantize(Decimal("0.01")),
            )
        )
    except BinanceTestnetError as exc:
        blocked_reason = str(exc)
    assert "halted" in blocked_reason.lower(), (
        "a new-risk order was not refused by the safety controller after break-glass: "
        f"{blocked_reason!r}"
    )

    cancel = control.handle(
        _command("cancel_open", seed="live-2", reason="cancel open orders at the venue")
    )
    assert cancel.venue_control == "confirmed"
    assert control.status().new_risk_permitted is False
    open_order_ids = {item.client_order_id for item in client.open_orders(SYMBOL)}
    assert order.client_order_id not in open_order_ids, "the resting order stayed open"


def test_live_duplicate_delivery_is_idempotent_and_restore_is_gated(live_context):
    control, _, _, journal, _, _ = live_context
    command_id = uuid4()
    first = control.handle(
        parse_operator_command(
            {
                "command_type": "enter_degraded",
                "command_id": str(command_id),
                "operator_label": "jamal",
                "reason": "live duplicate validation",
                "source": "operator-cli-live-testnet",
            },
            delivered_at=_now(),
        )
    )
    second = control.handle(
        parse_operator_command(
            {
                "command_type": "enter_degraded",
                "command_id": str(command_id),
                "operator_label": "jamal",
                "reason": "live duplicate validation",
                "source": "operator-cli-live-testnet",
            },
            delivered_at=_now(),
        )
    )
    assert first.accepted
    assert second.duplicate and second.disposition == "accepted"

    denied = control.handle(_command("restore_normal", seed="live-3", reason="premature restore"))
    assert denied.disposition == "rejected"
    assert denied.reason_code in {
        "restore_denied_reconciliation_pending",
        "restore_denied_acknowledgement_missing",
    }

    control.handle(_command("reconcile", seed="live-4", reason="reconcile after emergency action"))
    if control.status().reconciliation_status == "resolved":
        control.handle(_command("acknowledge", seed="live-5", reason="explicit acknowledgement"))
        restored = control.handle(_command("restore_normal", seed="live-6", reason="restore normal"))
        assert restored.accepted
        assert control.status().safety_mode == "normal"
        assert control.status().kill_switch_engaged is False

    rendered = json.dumps(
        [event.to_json() for event in journal.read_range()]
    )
    for secret in (
        os.environ.get("BINANCE_TESTNET_API_KEY", ""),
        os.environ.get("BINANCE_TESTNET_API_SECRET", ""),
    ):
        if secret:
            assert secret not in rendered
