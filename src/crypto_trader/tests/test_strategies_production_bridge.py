"""D29 item 7 (owner option A): Strategy -> ProductionRuntime bridge, tested ONLY with a non-writing port."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from crypto_trader.execution.ports import ExecutionOrderType, ExecutionSide
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionAdmissionController
from crypto_trader.production.environment import MAINNET_ENABLED
from crypto_trader.risk import load_limits
from crypto_trader.strategies import Bar, Signal, Strategy, StrategyError
from crypto_trader.strategies.production_bridge import ProductionStrategyDriver
from crypto_trader.strategies.runtime import MarketSpec, RunSettings
from crypto_trader.tests import production_fixtures as fx

SESSION = UUID("5e55a0aa-0000-4000-8000-00000000d029")


class AlwaysLong(Strategy):
    name = "bridge-long"
    timeframe = "4h"

    def on_bar(self, bars):
        return Signal.long(1, "long")


class Flat(Strategy):
    name = "bridge-flat"
    timeframe = "4h"

    def on_bar(self, bars):
        return None


@dataclass
class Snap:
    position_quantity: Decimal = Decimal(0)
    average_cost: Decimal = Decimal(0)
    realized_pnl_quote: Decimal = Decimal(0)
    equity_quote: Decimal = Decimal("1000")
    open_sell_quantity: Decimal = Decimal(0)
    quote_free: Decimal = Decimal("1000")
    stop_exits: int = 0
    target_exits: int = 0


def bars_ending_at_base(n: int = 5) -> list[Bar]:
    """Closed 4h bars whose last close is the fixture's market price at the fixture's clock."""
    out = []
    for i in range(n):
        open_time = fx.BASE - timedelta(hours=4 * (n - i))
        out.append(Bar(open_time=open_time, open=fx.PRICE, high=fx.PRICE, low=fx.PRICE, close=fx.PRICE,
                       volume=Decimal(1), timeframe="4h"))
    return out


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as active:
        yield active


def _driver(journal, tmp_path, strategy, *, limits=None, position=None):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    port = fx.RecordingExecutionPort(fx.INSTRUMENT)
    runtime = fx.build_runtime(journal=journal, state_dir=state, execution=port,
                               limits_value=limits)
    runtime.start()
    admission = ProductionAdmissionController(runtime=runtime, max_size=4, max_per_strategy_queued=2,
                                              max_retired=16, max_per_strategy_retired=8,
                                              max_rejection_strategies=8, clock=fx.clock(fx.BASE))
    market = MarketSpec(symbol=fx.SYMBOL, quantity_step=Decimal("0.00001"))
    driver = ProductionStrategyDriver(admission, strategy, session_id=SESSION, strategy_id="d29.bridge-test",
                                      market=market, settings=RunSettings(capital_quote=Decimal("1000")),
                                      position=position or (lambda obs: Snap()))
    return port, driver


def test_mainnet_stays_disabled_in_source():
    assert MAINNET_ENABLED is False


def test_strategy_buy_flows_through_admission_validity_risk_and_reaches_the_port(journal, tmp_path):
    port, driver = _driver(journal, tmp_path, AlwaysLong())
    result = driver.on_closed_bars(bars_ending_at_base())
    assert result.action == "buy" and result.offered == (True, "accepted")
    assert result.submitted is True and result.blocked_reason_code is None
    assert port.submit_count == 1
    request = port.requests[0]
    assert request.side is ExecutionSide.BUY and request.order_type is ExecutionOrderType.MARKET
    assert Decimal(0) < request.quantity * fx.PRICE <= Decimal("1000")
    assert str(request.intent_id) == result.intent_id


def test_same_bar_is_never_decided_twice(journal, tmp_path):
    port, driver = _driver(journal, tmp_path, AlwaysLong())
    bars = bars_ending_at_base()
    driver.on_closed_bars(bars)
    assert driver.on_closed_bars(bars) is None
    assert port.submit_count == 1


def test_hold_decisions_never_reach_the_ingress(journal, tmp_path):
    port, driver = _driver(journal, tmp_path, Flat())
    result = driver.on_closed_bars(bars_ending_at_base())
    assert result.action == "hold" and result.offered is None and port.submit_count == 0


def test_hard_risk_blocks_before_the_port(journal, tmp_path):
    tight = load_limits({**fx.LIMIT_CONFIG, "max_order_notional_quote": "100"})
    port, driver = _driver(journal, tmp_path, AlwaysLong(), limits=tight)
    result = driver.on_closed_bars(bars_ending_at_base())
    assert result.action == "buy" and result.submitted is False and result.blocked_reason_code
    assert port.submit_count == 0


def test_rewritten_history_is_refused(journal, tmp_path):
    _, driver = _driver(journal, tmp_path, Flat())
    bars = bars_ending_at_base(5)
    driver.on_closed_bars(bars[:4])
    changed = list(bars)
    changed[0] = Bar(open_time=bars[0].open_time, open=fx.PRICE, high=fx.PRICE * 2, low=fx.PRICE,
                     close=fx.PRICE, volume=Decimal(1), timeframe="4h")
    with pytest.raises(StrategyError):
        driver.on_closed_bars(changed)


def test_only_the_production_ingress_is_accepted(journal, tmp_path):
    with pytest.raises(StrategyError):
        ProductionStrategyDriver(object(), AlwaysLong(), session_id=SESSION, strategy_id="x", market=MarketSpec(),
                                 settings=RunSettings(), position=lambda o: Snap())
