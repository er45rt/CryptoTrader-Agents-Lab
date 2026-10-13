"""R-02 cash acceptance; baseline probes use actual legacy behavior, not import failures."""
from decimal import Decimal
from uuid import uuid4

import pytest

from crypto_trader.paper import accounting
from crypto_trader.paper import PaperStateError, SimulationOutcome, ScriptedSimulationRunner
from crypto_trader.execution import ExecutionRequest, ExecutionSide, ExecutionOrderType
from crypto_trader.execution.ports import ExecutionObservation, OrderLifecycleState
from crypto_trader.tests import paper_fixtures as fx


def cash(quote="10"):
    cls = getattr(accounting, "SpotCashLedger", None)
    if cls is None:
        return accounting.PositionLedger(Decimal(quote))
    return cls(Decimal(quote), symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT")


def fill(ledger, side, qty, price, fee="0"):
    args = dict(symbol="ETHUSDT", side=side, quantity=Decimal(qty),
                price=Decimal(price), fee=Decimal(fee))
    if isinstance(ledger, getattr(accounting, "SpotCashLedger", ())):
        args["fee_asset"] = "USDT"
    return ledger.apply_fill(**args)


def test_cash_rejects_sale_without_inventory():
    ledger = cash()
    with pytest.raises(PaperStateError, match="inventory|base|sell"):
        fill(ledger, "sell", "1", "10")
    assert ledger.positions() == {}


def test_cash_rejects_buy_without_quote_including_fee():
    ledger = cash()
    with pytest.raises(PaperStateError, match="quote|balance|cash"):
        fill(ledger, "buy", "1", "10", "0.01")
    assert ledger.positions() == {}


def test_cash_path_rejects_nonquote_fee_before_ledger_mutation(tmp_path):
    from crypto_trader.persistence import SQLiteEventJournal
    intent = uuid4()
    request = ExecutionRequest.create(intent_id=intent, correlation_id=fx.SESSION_ID,
        symbol="ETHUSDT", side=ExecutionSide.BUY, order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("1"), limit_price=Decimal("10"))
    obs = ExecutionObservation("fee-test", intent, fx.SESSION_ID, "ETHUSDT",
        OrderLifecycleState.FILLED, Decimal("1"), Decimal("0"),
        Decimal("1"), Decimal("10"), Decimal("0.01"), "ETH", 1)
    with SQLiteEventJournal(tmp_path / "fee.db") as journal:
        session = fx.session(journal, decisions=fx.scripted(fx.hold_decision()),
                             data_feed=fx.feed("10"), simulator=ScriptedSimulationRunner())
        ledger = cash("100")
        with pytest.raises(PaperStateError, match="fee|Quote|quote"):
            session._apply_fills(SimulationOutcome(observations=(obs,)), [request], ledger)
        assert ledger.positions() == {}


def test_cash_roundtrip_partial_close_fees_pnl_and_equity_are_exact():
    ledger = cash("1000")
    fill(ledger, "buy", "2", "100", "0.20")
    fill(ledger, "sell", "1", "110", "0.10")
    state = ledger.economic_state(Decimal("105"))
    assert state == {"base_total": Decimal("1"), "base_free": Decimal("1"),
        "base_locked": Decimal("0"), "quote_total": Decimal("909.70"),
        "quote_free": Decimal("909.70"), "quote_locked": Decimal("0"),
        "average_cost": Decimal("100"), "fees_quote": Decimal("0.30"),
        "realized_pnl_quote": Decimal("9.70"), "unrealized_pnl_quote": Decimal("5"),
        "equity_quote": Decimal("1014.70"), "gross_exposure_quote": Decimal("105")}


def test_cash_locks_protect_competing_orders_and_partial_leaves():
    ledger = cash("1000")
    buy = uuid4()
    ledger.reserve(buy, "buy", Decimal("5"), Decimal("100"))
    assert (ledger.quote_free, ledger.quote_locked) == (Decimal("500"), Decimal("500"))
    with pytest.raises(PaperStateError):
        ledger.reserve(uuid4(), "buy", Decimal("6"), Decimal("100"))
    ledger.apply_fill(symbol="ETHUSDT", side="buy", quantity=Decimal("2"),
        price=Decimal("100"), fee=Decimal("0.20"), fee_asset="USDT", intent_id=buy)
    assert ledger.quote_locked == Decimal("300")
    assert ledger.quote_free == Decimal("499.80")
    sell = uuid4()
    ledger.reserve(sell, "sell", Decimal("1"), Decimal("100"))
    assert ledger.base_free == Decimal("1") and ledger.base_locked == Decimal("1")
    with pytest.raises(PaperStateError):
        ledger.reserve(uuid4(), "sell", Decimal("2"), Decimal("100"))


@pytest.mark.parametrize("asset", [None, "ETH", "BNB", ""])
def test_cash_rejects_unsupported_fee_asset_even_when_fee_zero(asset):
    ledger = cash("100")
    with pytest.raises(PaperStateError, match="fee"):
        ledger.apply_fill(symbol="ETHUSDT", side="buy", quantity=Decimal("1"),
            price=Decimal("10"), fee=Decimal("0"), fee_asset=asset)
    assert ledger.positions() == {}


def test_cash_cannot_borrow_for_sale_fee():
    ledger = cash("10")
    fill(ledger, "buy", "1", "10")
    with pytest.raises(PaperStateError, match="quote"):
        fill(ledger, "sell", "1", "10", "11")
    assert ledger.base_total == Decimal("1") and ledger.quote_total == Decimal("0")
