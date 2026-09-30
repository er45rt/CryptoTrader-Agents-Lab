"""Phase 11: LIVE Binance SPOT TESTNET integration tests (opt-in, never in the offline suite).

These tests are skipped unless BOTH conditions hold:

* ``CRYPTO_TRADER_RUN_TESTNET=1`` (explicit opt-in), and
* ``BINANCE_TESTNET_API_KEY`` / ``BINANCE_TESTNET_API_SECRET`` are present in the environment.

They run against the documented Spot Testnet host only, with test funds, and place at most a few
tiny resting limit orders that are always cancelled. NO mainnet endpoint, no real capital and no
withdrawal capability is ever used. A testnet outage fails these tests - it is never disguised as
an application PASS, and the offline suite stays green independently.

Credential values are read from the environment by the adapter and are never printed here.
"""

from __future__ import annotations

import os
import time
import uuid
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

import pytest

from crypto_trader.execution import (
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionSide,
    InstrumentSpec,
)
from crypto_trader.execution.binance_testnet import (
    AmbiguousOrderError,
    BinanceSpotTestnetClient,
    RequestsTransport,
    TransportTimeout,
    WithdrawalPolicy,
    credentials_from_environment,
)
from crypto_trader.execution.binance_testnet import (
    TestnetClock as SpotClock,
    TestnetOrderRuntime as OrderRuntime,
    TestnetOrderRequest as SpotOrderRequest,
)
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.recovery import ReconciliationEngine, ReconciliationOutcome, load_policy
from crypto_trader.safety import SafetyController
from crypto_trader.tests import testnet_fixtures as fx

RUN_FLAG = "CRYPTO_TRADER_RUN_TESTNET"
INSTRUMENT = InstrumentSpec(symbol="BTCUSDT", base_currency="BTC", starting_balance="100000 USDT")


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
        reason=f"live testnet integration requires {RUN_FLAG}=1 plus testnet credentials",
    ),
]


def _new_client():
    """A real client built from the ENVIRONMENT credentials (never from test fixtures).

    ``RECORD_ONLY`` is used because Binance Spot Testnet grants the withdrawal permission bit on
    its default testnet keys while this adapter provably implements no withdrawal endpoint. The
    observed flag is recorded in the environment record for the readiness matrix.
    """
    return BinanceSpotTestnetClient(
        credentials=credentials_from_environment(),
        transport=RequestsTransport(timeout_seconds=15.0),
        clock=SpotClock(time_source=time.time, recv_window_ms=5_000),
        symbol="BTCUSDT",
        withdrawal_policy=WithdrawalPolicy.RECORD_ONLY,
    )


def _resting_limit_request(client, *, side: str = "BUY") -> SpotOrderRequest:
    """A tiny resting limit order that cannot cross the market (it never fills).

    Sizing is computed against the ORDER PRICE (not the market price), because the venue's minimum
    notional filter is evaluated at the limit price.
    """
    rules = client.symbol_rules(client.symbol)
    market = client.price()
    # The venue reports tickSize/stepSize with trailing zeros (e.g. "0.01000000"); normalizing
    # gives the canonical exponent that our boundary precision checks expect.
    tick = rules.price_tick.normalize()
    step = rules.quantity_step.normalize()

    # Keep resting prices inside the venue's percent-price band, as in the other live tests.
    offset = Decimal("0.9") if side == "BUY" else Decimal("1.1")
    raw_price = market * offset
    price = ((raw_price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick).quantize(tick)

    minimum_quantity = max(rules.min_notional / price, rules.min_quantity)
    steps = (minimum_quantity / step).to_integral_value(rounding=ROUND_CEILING)
    quantity = (steps * step).quantize(step)
    assert quantity * price >= rules.min_notional, "sizing must satisfy the notional minimum"
    return SpotOrderRequest(
        client_order_id=f"ct-live-{uuid.uuid4().hex[:12]}",
        symbol=client.symbol,
        side=side,
        order_type="LIMIT",
        quantity=quantity,
        price=price,
        time_in_force="GTC",
    )


def _instrument_for(client) -> InstrumentSpec:
    """Derive the instrument spec from the VENUE's rules instead of hard-coding precision."""
    rules = client.symbol_rules(client.symbol)
    return InstrumentSpec(
        symbol=rules.symbol,
        venue="BINANCE_TESTNET",
        base_currency=rules.base_asset,
        quote_currency=rules.quote_asset,
        price_precision=max(0, -rules.price_tick.normalize().as_tuple().exponent),
        size_precision=max(0, -rules.quantity_step.normalize().as_tuple().exponent),
    )


def _execution_request(client, intent_id, correlation_id) -> ExecutionRequest:
    resting = _resting_limit_request(client)
    return ExecutionRequest.create(
        correlation_id=correlation_id,
        symbol=INSTRUMENT.symbol,
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=resting.quantity,
        limit_price=resting.price,
        intent_id=intent_id,
    )


@pytest.fixture(scope="module")
def live_client():
    """A connected testnet client whose open orders are always cancelled on teardown."""
    client = _new_client()
    client.verify_environment()
    try:
        yield client
    finally:
        try:
            client.cancel_all_open_orders(client.symbol)
        except Exception:  # pragma: no cover - cleanup is best effort
            pass


def test_live_environment_is_spot_testnet_and_least_privilege(live_client):
    environment = live_client.verify_environment()

    assert environment.base_url == "https://testnet.binance.vision"
    assert environment.venue_label == "binance-spot-testnet"
    assert environment.account.can_trade is True
    assert environment.symbol == "BTCUSDT"
    assert environment.account.permissions
    # The withdrawal flag is RECORDED (never assumed); the adapter has no withdrawal capability.
    record = environment.to_record()
    assert "can_withdraw" in record
    assert "no withdrawal endpoint exists anywhere in this adapter" in " ".join(environment.notes)


def test_live_clock_is_synchronized_and_stable(live_client):
    first = live_client.synchronize_clock()
    second = live_client.synchronize_clock()

    assert abs(first) < 60_000
    assert abs(second - first) < 5_000


def test_live_order_place_and_cancel_lifecycle(tmp_path: Path, live_client):
    with SQLiteEventJournal(tmp_path / "live.db") as journal:
        runtime = OrderRuntime(
            client=live_client, instrument=_instrument_for(live_client), journal=journal
        )
        runtime.start()
        intent_id = uuid.uuid4()
        reference = runtime.submit(_execution_request(live_client, intent_id, uuid.uuid4()))

        placed = runtime.refresh(reference)
        assert placed.venue_order_id is not None
        assert placed.state.value in {"accepted", "partially_filled"}

        canceled = runtime.cancel(reference)
        assert canceled.state.value == "canceled"
        journaled = [event.event_type for event in journal.read_range()]
        assert "order.state.changed" in journaled
        assert runtime.observations()[-1].intent_id == intent_id


def test_live_duplicate_client_order_id_is_idempotent(live_client):
    request = _resting_limit_request(live_client)
    first = fx.place_order(live_client, request)
    try:
        second = fx.place_order(live_client, request)
    finally:
        live_client.cancel_order(request.symbol, request.client_order_id)

    assert first.deduplicated is False
    assert second.deduplicated is True
    assert second.venue_order_id == first.venue_order_id


class _InterruptAfterSend:
    """Sends the real request, then drops the response (a realistic ambiguous write)."""

    def __init__(self, transport) -> None:
        self._transport = transport
        self._interrupted = False

    def send(self, request):
        response = self._transport.send(request)
        if request.is_write and not self._interrupted:
            self._interrupted = True
            raise TransportTimeout("simulated missing response")
        return response


def test_live_ambiguous_submission_is_reconciled_not_retried(tmp_path: Path, live_client):
    intent_id = uuid.uuid4()
    correlation_id = uuid.uuid4()
    with SQLiteEventJournal(tmp_path / "live-ambiguity.db") as journal:
        # Stage 2: a venue write requires a durable attempt journal before the POST.
        runtime = OrderRuntime(client=live_client, instrument=INSTRUMENT, journal=journal)
        runtime.start()
        request = _execution_request(live_client, intent_id, correlation_id)

        original = runtime.client.transport
        runtime.client.transport = _InterruptAfterSend(original)
        try:
            with pytest.raises(AmbiguousOrderError):
                runtime.submit(request)
        finally:
            runtime.client.transport = original

        # The restart path adopts the order that really exists instead of re-sending it.
        restarted = OrderRuntime(client=live_client, instrument=INSTRUMENT, journal=journal)
        restarted.start()
        adopted = restarted.recover_order(intent_id, correlation_id=correlation_id)
    assert adopted is not None
    assert adopted.client_order_id == restarted.client_order_id_for(intent_id)

    engine = ReconciliationEngine(load_policy({"policy_id": "recovery-v1"}))
    report = engine.reconcile(restarted.local_states(), restarted.venue_state_source().reports())
    assert report.outcome_for(adopted.client_order_id) in {
        ReconciliationOutcome.MATCHED,
        ReconciliationOutcome.RESOLVED_FROM_VENUE,
    }
    assert report.requires_halt_new_risk is False

    live_client.cancel_order(INSTRUMENT.symbol, adopted.client_order_id)


def test_live_safety_cancel_all_stops_every_open_order(live_client):
    placed = [
        fx.place_order(live_client, _resting_limit_request(live_client)) for _ in range(2)
    ]
    assert len(placed) == 2
    safety = SafetyController()
    safety.engage_kill_switch(reason_code="live_check", reason="live safety-lane check")
    assert safety.allow_new_risk() is False

    results = live_client.cancel_all_open_orders(live_client.symbol)

    assert all(order.state.value == "canceled" for order in results)
    assert live_client.open_orders(live_client.symbol) == ()


def test_live_reconnect_after_an_idle_period(live_client):
    environment = live_client.reconnect()

    assert environment.base_url == "https://testnet.binance.vision"
    assert environment.account.can_trade is True
