"""Phase 15: LIVE Binance MAINNET READ-ONLY validation (opt-in, never in the offline suite).

Skipped unless BOTH conditions hold:

* ``CRYPTO_TRADER_RUN_MAINNET_READONLY=1`` (explicit opt-in), and
* ``BINANCE_MAINNET_READONLY_API_KEY`` / ``BINANCE_MAINNET_READONLY_API_SECRET`` are present.

Scope: authenticated READ-ONLY mainnet access only (account state, permissions, open orders,
filters, top of book). NO order is submitted, cancelled or replaced; NO real capital is used; NO
withdrawal capability exists; credential values are never printed, journaled or asserted.
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

from crypto_trader.execution import (
    ExecutionOrderType,
    ExecutionSide,
    ExecutionTimeInForce,
    InstrumentSpec,
)
from crypto_trader.execution.binance_testnet import RequestsTransport
# Aliased so pytest never mistakes the adapter's clock for a test class.
from crypto_trader.execution.binance_testnet import TestnetClock as HttpClock
from crypto_trader.mainnet import (
    API_KEY_ENV_VAR,
    API_SECRET_ENV_VAR,
    DryRunDisposition,
    DryRunError,
    MainnetMutationForbiddenError,
    MainnetReadOnlyClient,
    credentials_from_environment,
)
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionIntent
from crypto_trader.tests import mainnet_fixtures as fx
from crypto_trader.validity import LiveMarketStateProvider, MarketObservation

RUN_FLAG = "CRYPTO_TRADER_RUN_MAINNET_READONLY"
SYMBOL = "BTCUSDT"
INSTRUMENT = InstrumentSpec(
    symbol=SYMBOL, venue="BINANCE_SPOT_MAINNET_READONLY", base_currency="BTC", quote_currency="USDT"
)


def live_run_enabled() -> bool:
    if os.environ.get(RUN_FLAG) != "1":
        return False
    return bool(os.environ.get(API_KEY_ENV_VAR)) and bool(os.environ.get(API_SECRET_ENV_VAR))


pytestmark = [
    pytest.mark.mainnet_readonly,
    pytest.mark.skipif(
        not live_run_enabled(),
        reason=f"live mainnet read-only validation requires {RUN_FLAG}=1 plus the read-only key pair",
    ),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _live_client() -> MainnetReadOnlyClient:
    client = MainnetReadOnlyClient(
        credentials=credentials_from_environment(),
        transport=RequestsTransport(timeout_seconds=15.0),
        clock=HttpClock(time_source=time.time, recv_window_ms=5_000),
        symbol=SYMBOL,
    )
    # The signed permission/account reads require a measured server offset: the clock is
    # fail-closed until it has been synchronized with the venue (ClockDriftError otherwise).
    client.synchronize_clock()
    return client


def _decision_source(client: MainnetReadOnlyClient, filters):
    def decide(snapshot):
        rules = filters
        bid, ask = client.book_ticker(SYMBOL)
        mid = (bid + ask) / Decimal(2)
        tick = rules.tick_size.normalize()
        step = rules.step_size.normalize()
        limit_price = ((mid / tick).to_integral_value(rounding=ROUND_FLOOR) * tick).quantize(tick)
        minimum_quantity = max(rules.min_notional / limit_price, rules.min_quantity)
        quantity = (
            (minimum_quantity / step).to_integral_value(rounding=ROUND_CEILING) * step
        ).quantize(step)
        stamp = _now()
        return ProductionIntent(
            correlation_id=uuid4(),
            symbol=SYMBOL,
            side=ExecutionSide.BUY,
            order_type=ExecutionOrderType.LIMIT,
            quantity=quantity,
            strategy_id="strategy.mainnet-readonly-live",
            decision_started_at=stamp,
            decision_completed_at=stamp,
            market_event_ts=snapshot.market_event_ts,
            reference_price=mid,
            market_reference=snapshot.market_reference,
            limit_price=limit_price,
            time_in_force=ExecutionTimeInForce.FOK,
        )

    return decide


def _live_market_state(client: MainnetReadOnlyClient) -> LiveMarketStateProvider:
    def latest(symbol: str) -> MarketObservation:
        stamp = _now()
        bid, ask = client.book_ticker(symbol)
        return MarketObservation(
            symbol=symbol,
            price=(bid + ask) / Decimal(2),
            market_event_ts=stamp,
            local_ingest_ts=stamp,
            market_reference=f"live-{stamp.timestamp()}",
        )

    return LiveMarketStateProvider(latest)


def test_live_mainnet_reads_satisfy_the_strict_read_only_profile(tmp_path: Path):
    client = _live_client()

    restrictions = client.verify_read_only_permissions()
    account = client.account_snapshot()
    open_orders = client.open_orders(SYMBOL)
    rules = client.exchange_info(SYMBOL)
    bid, ask = client.book_ticker(SYMBOL)

    assert restrictions.is_read_only is True
    assert restrictions.enable_reading is True
    assert restrictions.writable_capabilities == ()
    assert isinstance(account, dict)
    assert isinstance(open_orders, tuple)
    assert rules.symbol == SYMBOL
    assert rules.tick_size > 0 and rules.step_size > 0
    assert bid > 0 and ask >= bid
    assert all("GET" in entry for entry in client.request_log())
    rendered = json.dumps({"log": list(client.request_log()), "rules": rules.to_record()})
    for name in (API_KEY_ENV_VAR, API_SECRET_ENV_VAR):
        value = os.environ.get(name, "")
        if value:
            assert value not in rendered


def test_live_read_only_dry_run_cycle_never_mutates_anything(tmp_path: Path):
    state_dir = tmp_path / "state"
    client = _live_client()
    rules = client.exchange_info(SYMBOL)
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        recorder = fx.soak(tmp_path)
        pipeline = fx.pipeline(
            journal=journal,
            state_dir=state_dir,
            tmp_path=tmp_path,
            client_instance=client,
            permission_check=client.verify_read_only_permissions,
            decision_source=_decision_source(client, rules),
            market=_live_market_state(client),
            soak_instance=recorder,
        )
        pipeline.start()
        result = pipeline.run_cycle()

        assert result.disposition in {DryRunDisposition.WOULD_SUBMIT, DryRunDisposition.WOULD_REJECT}
        assert pipeline.status()["mainnet_mutation_attempts"] == 0
        assert recorder.state().counters.completed_pipeline_cycles == 1
        if result.would_submit:
            with pytest.raises(MainnetMutationForbiddenError):
                pipeline.try_to_submit(result.order)
        else:
            assert result.reason_code

        rendered = json.dumps(recorder.snapshot())
        for name in (API_KEY_ENV_VAR, API_SECRET_ENV_VAR):
            value = os.environ.get(name, "")
            if value:
                assert value not in rendered


def test_live_venue_order_endpoint_is_unreachable(tmp_path: Path):
    gate = fx.gate()
    gate.start()
    with pytest.raises(MainnetMutationForbiddenError):
        gate.refuse("order_submit")
    assert gate.mutation_attempts == 1
    pipeline = fx.pipeline(
        journal=SQLiteEventJournal(tmp_path / "journal.db"),
        state_dir=tmp_path / "state",
        tmp_path=tmp_path,
    )
    with pytest.raises(DryRunError):
        pipeline.run_cycle()
