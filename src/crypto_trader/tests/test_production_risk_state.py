"""G5b canonical production RiskState derivation tests."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from crypto_trader.events import (
    AccountEquityRecordPayload,
    AccountEquityRecorded,
    ExecutionFillRecorded,
    FillRecordPayload,
    OrderSubmissionAttemptPayload,
    OrderSubmissionAttempted,
    VenueTradeRecordPayload,
    VenueTradeRecorded,
)
from crypto_trader.execution import InstrumentSpec
from crypto_trader.execution.binance_testnet import (
    MarkEvidence,
    VenueAccountSnapshot,
    VenueAssetBalance,
)
from crypto_trader.production import EquityChainState, ProductionStateError
from crypto_trader.production.risk_state import derive_risk_state
from crypto_trader.risk import HardRiskEngine, RiskRequest, RiskStateSnapshot, Side
from crypto_trader.tests.production_fixtures import limits as production_limits

UTC = timezone.utc
BASE = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SYMBOL = "BTCUSDT"
BASE_ASSET = "BTC"
QUOTE_ASSET = "USDT"
ACCOUNT = "123456789"
ENV = "binance-spot-testnet"
ROOT_ID = UUID("10000000-0000-4000-8000-000000000001")
ORDER_ID = UUID("20000000-0000-4000-8000-000000000001")
ATTEMPT_ID = UUID("30000000-0000-4000-8000-000000000001")
FILL_ID = UUID("40000000-0000-4000-8000-000000000001")
TRADE_ID = UUID("50000000-0000-4000-8000-000000000001")
CORRELATION_ID = UUID("60000000-0000-4000-8000-000000000001")
INTENT_ID = UUID("70000000-0000-4000-8000-000000000001")
VENUE = "infrastructure.binance_testnet"
STRATEGY = "strategy.alpha"


def instrument() -> InstrumentSpec:
    return InstrumentSpec(
        symbol=SYMBOL,
        venue="BINANCE_TESTNET",
        base_currency=BASE_ASSET,
        quote_currency=QUOTE_ASSET,
        price_precision=2,
        size_precision=8,
    )


def root(*, base="1", quote="10000", mark="30000", seconds=0) -> AccountEquityRecorded:
    equity = Decimal(base) * Decimal(mark) + Decimal(quote)
    occurred = BASE + timedelta(seconds=seconds)
    return AccountEquityRecorded(
        event_id=ROOT_ID,
        occurred_at=occurred,
        correlation_id=CORRELATION_ID,
        producer="application.production.equity",
        symbol=SYMBOL,
        payload=AccountEquityRecordPayload(
            kind="baseline",
            environment=ENV,
            venue_account_id=ACCOUNT,
            base_asset=BASE_ASSET,
            quote_asset=QUOTE_ASSET,
            base_balance=Decimal(base),
            quote_balance=Decimal(quote),
            mark_price=Decimal(mark),
            mark_reference="binance.ticker.price:BTCUSDT",
            mark_event_ts=occurred,
            equity_quote=equity,
            peak_equity_quote=equity,
            baseline_event_id=ROOT_ID,
            previous_event_id=None,
            fence_term=1,
            reason="test baseline",
        ),
    )


def chain(root_event: AccountEquityRecorded) -> EquityChainState:
    p = root_event.payload
    return EquityChainState(
        baseline_event_id=root_event.event_id,
        last_event_id=root_event.event_id,
        last_kind=p.kind,
        symbol=SYMBOL,
        environment=ENV,
        venue_account_id=ACCOUNT,
        base_asset=BASE_ASSET,
        quote_asset=QUOTE_ASSET,
        peak_equity_quote=p.peak_equity_quote,
        last_equity_quote=p.equity_quote,
        fence_term=p.fence_term,
        last_occurred_at=root_event.occurred_at,
        root_occurred_at=root_event.occurred_at,
        root_base_balance=p.base_balance,
        root_quote_balance=p.quote_balance,
        root_mark_price=p.mark_price,
        root_mark_reference=p.mark_reference,
    )


def attempt(
    *,
    side="buy",
    qty="0.1",
    price="30000",
    seconds=1,
    strategy=STRATEGY,
    order_id=ORDER_ID,
    intent_id=INTENT_ID,
) -> OrderSubmissionAttempted:
    return OrderSubmissionAttempted(
        event_id=ATTEMPT_ID if order_id == ORDER_ID else UUID("30000000-0000-4000-8000-000000000099"),
        occurred_at=BASE + timedelta(seconds=seconds),
        correlation_id=CORRELATION_ID,
        producer="application.production",
        strategy_id=strategy,
        order_id=order_id,
        symbol=SYMBOL,
        payload=OrderSubmissionAttemptPayload(
            venue_reference=f"ct-{str(order_id)[-8:]}",
            intent_id=intent_id,
            side=side,
            order_type="limit",
            quantity=Decimal(qty),
            limit_price=Decimal(price),
            time_in_force="gtc",
            venue_producer=VENUE,
        ),
    )


def fill(
    *,
    qty="0.1",
    price="30000",
    fee="0",
    fee_asset=None,
    remaining="0",
    seconds=2,
    strategy=None,
    order_id=ORDER_ID,
) -> ExecutionFillRecorded:
    return ExecutionFillRecorded(
        event_id=FILL_ID if order_id == ORDER_ID else UUID("40000000-0000-4000-8000-000000000099"),
        occurred_at=BASE + timedelta(seconds=seconds),
        correlation_id=CORRELATION_ID,
        producer=VENUE,
        strategy_id=strategy,
        order_id=order_id,
        symbol=SYMBOL,
        sequence_number=1,
        payload=FillRecordPayload(
            quantity=Decimal(qty),
            price=Decimal(price),
            fee=Decimal(fee),
            fee_asset=fee_asset,
            remaining_quantity=Decimal(remaining),
            liquidity="taker",
        ),
    )


def trade_record(fill_event: ExecutionFillRecorded, *, price=None, commission_asset=None):
    p = fill_event.payload
    return VenueTradeRecorded(
        event_id=TRADE_ID,
        occurred_at=fill_event.occurred_at,
        correlation_id=CORRELATION_ID,
        causation_id=fill_event.event_id,
        producer=VENUE,
        strategy_id=STRATEGY,
        order_id=fill_event.order_id,
        symbol=SYMBOL,
        payload=VenueTradeRecordPayload(
            venue_reference=f"ct-{str(fill_event.order_id)[-8:]}",
            venue_order_id="42",
            venue_trade_id="7",
            fill_event_id=fill_event.event_id,
            price=Decimal(price) if price is not None else p.price,
            quantity=p.quantity,
            quote_quantity=(Decimal(price) if price is not None else p.price) * p.quantity,
            commission=p.fee,
            commission_asset=commission_asset or p.fee_asset or QUOTE_ASSET,
            trade_time=fill_event.occurred_at,
            is_buyer=True,
            is_maker=False,
        ),
    )


def account(*, base="1.1", quote="7000", seconds=3) -> VenueAccountSnapshot:
    observed = BASE + timedelta(seconds=seconds)
    return VenueAccountSnapshot(
        venue_account_id=ACCOUNT,
        venue_update_time_ms=int(observed.timestamp() * 1000),
        fetched_at=observed,
        balances=(
            VenueAssetBalance(asset=BASE_ASSET, free=Decimal(base), locked=Decimal("0")),
            VenueAssetBalance(asset=QUOTE_ASSET, free=Decimal(quote), locked=Decimal("0")),
        ),
    )


def mark(*, price="32000", seconds=3) -> MarkEvidence:
    return MarkEvidence(
        symbol=SYMBOL,
        price=Decimal(price),
        observed_at=BASE + timedelta(seconds=seconds),
        reference=f"binance.ticker.price:{SYMBOL}",
    )


def derive(events, *, acct=None, mark_ev=None, root_event=None, now=None):
    root_event = root_event or events[0]
    return derive_risk_state(
        events,
        chain=chain(root_event),
        account=acct or account(),
        mark=mark_ev or mark(),
        instrument=instrument(),
        now=now or BASE + timedelta(seconds=4),
    )


def test_live_fill_without_trade_metadata_is_accepted_when_balance_identity_proves_completeness():
    r = root()
    a = attempt()
    f = fill()

    state = derive([r, a, f])

    assert state.equity_quote == Decimal("42200")
    assert state.peak_equity_quote == Decimal("42200")
    assert state.position_quantity[SYMBOL] == Decimal("1.1")
    assert state.realized_pnl_quote == Decimal("0")
    assert state.open_orders == 0
    assert state.open_positions == 1
    assert state.strategy_exposure_quote[STRATEGY] == Decimal("3200")
    assert state.snapshot_ts == BASE + timedelta(seconds=3)


def test_balance_identity_mismatch_fails_closed():
    r = root()
    with pytest.raises(ProductionStateError, match="do not reconcile"):
        derive([r, attempt(), fill()], acct=account(base="1.09", quote="7000"))


def test_third_asset_fee_fails_closed():
    r = root()
    bad_fill = fill(fee="0.001", fee_asset="BNB")
    with pytest.raises(ProductionStateError, match="fee asset"):
        derive([r, attempt(), bad_fill])


def test_post_root_attempt_missing_strategy_fails_closed():
    r = root()
    with pytest.raises(ProductionStateError, match="missing durable strategy"):
        derive([r, attempt(strategy=None), fill()])


def test_linked_venue_trade_must_exactly_corroborate_fill():
    r = root()
    f = fill(fee="1", fee_asset="USDT")
    with pytest.raises(ProductionStateError, match="venue trade metadata disagrees"):
        derive(
            [r, attempt(), f, trade_record(f, price="30001", commission_asset="USDT")],
            acct=account(base="1.1", quote="6999"),
        )


def test_pre_root_order_activity_is_absorbed_by_root_not_replayed_into_pnl():
    a = attempt(seconds=1)
    f = fill(seconds=2)
    r = root(base="1.1", quote="7000", mark="30000", seconds=10)

    state = derive(
        [a, f, r],
        acct=account(base="1.1", quote="7000", seconds=11),
        mark_ev=mark(price="32000", seconds=11),
        root_event=r,
        now=BASE + timedelta(seconds=12),
    )

    assert state.realized_pnl_quote == Decimal("0")
    assert state.position_quantity[SYMBOL] == Decimal("1.1")
    assert STRATEGY not in state.strategy_exposure_quote


def test_post_root_sell_realizes_average_cost_pnl_and_satisfies_equity_invariant():
    r = root(base="0", quote="40000")
    buy_attempt = attempt(side="buy", qty="1", price="30000", seconds=1)
    buy_fill = fill(qty="1", price="30000", seconds=2)
    sell_order_id = UUID("20000000-0000-4000-8000-000000000099")
    sell_attempt = attempt(
        side="sell",
        qty="0.2",
        price="35000",
        seconds=3,
        order_id=sell_order_id,
        intent_id=UUID("70000000-0000-4000-8000-000000000099"),
    )
    sell_fill = fill(
        qty="0.2", price="35000", seconds=4, order_id=sell_order_id
    )

    state = derive(
        [r, buy_attempt, buy_fill, sell_attempt, sell_fill],
        acct=account(base="0.8", quote="17000", seconds=5),
        mark_ev=mark(price="34000", seconds=5),
        root_event=r,
        now=BASE + timedelta(seconds=6),
    )

    assert state.realized_pnl_quote == Decimal("1000")
    assert state.position_quantity[SYMBOL] == Decimal("0.8")
    assert state.strategy_exposure_quote[STRATEGY] == Decimal("27200")
    root_equity = Decimal("40000")
    unrealized = (Decimal("34000") - Decimal("30000")) * Decimal("0.8")
    assert state.equity_quote - root_equity == state.realized_pnl_quote + unrealized


def test_strategy_cannot_sell_unattributed_root_inventory():
    r = root()
    a = attempt(side="sell", qty="0.2", price="35000")
    f = fill(qty="0.2", price="35000")

    with pytest.raises(ProductionStateError, match="attributed inventory"):
        derive(
            [r, a, f],
            acct=account(base="0.8", quote="17000"),
            mark_ev=mark(price="34000"),
        )


def test_oversell_fails_closed_before_balance_identity_can_mask_it():
    r = root(base="0.05", quote="10000")
    a = attempt(side="sell", qty="0.1", price="35000")
    f = fill(qty="0.1", price="35000")
    with pytest.raises(ProductionStateError, match="oversell"):
        derive(
            [r, a, f],
            acct=account(base="0", quote="13500"),
            root_event=r,
        )


def test_future_account_or_mark_evidence_fails_closed():
    r = root()
    future = BASE + timedelta(seconds=10)
    with pytest.raises(ProductionStateError, match="future"):
        derive(
            [r],
            acct=VenueAccountSnapshot(
                venue_account_id=ACCOUNT,
                venue_update_time_ms=int((BASE + timedelta(seconds=3)).timestamp() * 1000),
                fetched_at=future,
                balances=(
                    VenueAssetBalance(asset=BASE_ASSET, free=Decimal("1"), locked=Decimal("0")),
                    VenueAssetBalance(asset=QUOTE_ASSET, free=Decimal("10000"), locked=Decimal("0")),
                ),
            ),
            mark_ev=mark(seconds=3),
            now=BASE + timedelta(seconds=4),
        )


def test_open_limit_order_is_included_in_worst_case_symbol_and_strategy_exposure():
    r = root()
    a = attempt(side="buy", qty="0.1", price="31000")

    state = derive(
        [r, a],
        acct=account(base="1", quote="10000"),
        mark_ev=mark(price="32000"),
    )

    assert state.open_orders == 1
    assert state.net_exposure_quote == Decimal("35100")
    assert state.gross_exposure_quote == Decimal("35100")
    assert state.symbol_exposure_quote[SYMBOL] == Decimal("35100")
    assert state.strategy_exposure_quote[STRATEGY] == Decimal("3100")


def test_open_sell_cannot_reserve_unattributed_root_inventory():
    r = root()
    a = attempt(side="sell", qty="0.2", price="31000")

    with pytest.raises(ProductionStateError, match="open sell quantity.*attributed inventory"):
        derive(
            [r, a],
            acct=account(base="1", quote="10000"),
            mark_ev=mark(price="32000"),
        )


def test_quote_fee_is_realized_immediately_not_hidden_in_inventory_cost():
    r = root(base="0", quote="10000")
    a = attempt(side="buy", qty="0.1", price="30000")
    f = fill(qty="0.1", price="30000", fee="3", fee_asset="USDT")
    state = derive(
        [r, a, f],
        acct=account(base="0.1", quote="6997"),
        root_event=r,
    )
    assert state.realized_pnl_quote == Decimal("-3")


def test_base_fee_is_realized_immediately_at_average_cost():
    r = root(base="0", quote="10000")
    a = attempt(side="buy", qty="0.1", price="30000")
    f = fill(qty="0.1", price="30000", fee="0.001", fee_asset="BTC")
    state = derive(
        [r, a, f],
        acct=account(base="0.099", quote="7000"),
        root_event=r,
    )
    assert state.realized_pnl_quote == Decimal("-30")
    assert state.position_quantity[SYMBOL] == Decimal("0.099")


def test_stale_account_and_mark_evidence_fail_closed():
    r = root()
    now = BASE + timedelta(seconds=20)
    with pytest.raises(ProductionStateError, match="account evidence is stale"):
        derive(
            [r],
            acct=account(base="1", quote="10000", seconds=3),
            mark_ev=mark(price="32000", seconds=19),
            now=now,
        )
    with pytest.raises(ProductionStateError, match="mark evidence is stale"):
        derive(
            [r],
            acct=account(base="1", quote="10000", seconds=19),
            mark_ev=mark(price="32000", seconds=3),
            now=now,
        )


def _reservation_state(*, strategy_quantity="0") -> RiskStateSnapshot:
    return RiskStateSnapshot(
        equity_quote=Decimal("100000"),
        peak_equity_quote=Decimal("100000"),
        realized_pnl_quote=Decimal("0"),
        gross_exposure_quote=Decimal("0"),
        net_exposure_quote=Decimal("0"),
        open_orders=0,
        open_positions=0,
        symbol_exposure_quote={SYMBOL: Decimal("0")},
        strategy_exposure_quote={STRATEGY: Decimal("0")},
        position_quantity={SYMBOL: Decimal(strategy_quantity)},
        strategy_position_quantity={STRATEGY: Decimal(strategy_quantity)},
        strategy_open_sell_quantity={STRATEGY: Decimal("0")},
        snapshot_ts=BASE,
    )


@pytest.mark.parametrize(
    "limit_field,reason_code",
    [
        ("max_symbol_exposure_quote", "max_symbol_exposure_exceeded"),
        ("max_strategy_exposure_quote", "max_strategy_exposure_exceeded"),
        ("max_gross_exposure_quote", "max_gross_exposure_exceeded"),
        ("max_net_exposure_quote", "max_net_exposure_exceeded"),
    ],
)
def test_pending_same_side_intent_reserves_exposure_before_second_prepare(
    limit_field, reason_code
):
    resolved = replace(production_limits(), **{limit_field: Decimal("400")})
    engine = HardRiskEngine(
        resolved,
        include_pending_reservations=True,
        enforce_attributed_inventory=True,
    )
    state = _reservation_state()
    first = RiskRequest(
        correlation_id=uuid4(),
        symbol=SYMBOL,
        side=Side.BUY,
        quantity=Decimal("0.01"),
        price=Decimal("27000"),
        strategy_id=STRATEGY,
        intent_id=uuid4(),
    )
    second = RiskRequest(
        correlation_id=uuid4(),
        symbol=SYMBOL,
        side=Side.BUY,
        quantity=Decimal("0.01"),
        price=Decimal("27000"),
        strategy_id=STRATEGY,
        intent_id=uuid4(),
    )

    assert engine.evaluate(first, state).allowed
    decision = engine.evaluate(second, state)

    assert not decision.allowed
    assert reason_code in decision.reason_codes
    assert len(engine.pending) == 1


def test_pending_sells_cannot_jointly_exceed_attributed_strategy_inventory():
    engine = HardRiskEngine(
        production_limits(),
        include_pending_reservations=True,
        enforce_attributed_inventory=True,
    )
    state = _reservation_state(strategy_quantity="1")
    first = RiskRequest(
        correlation_id=uuid4(),
        symbol=SYMBOL,
        side=Side.SELL,
        quantity=Decimal("0.6"),
        price=Decimal("100"),
        strategy_id=STRATEGY,
        intent_id=uuid4(),
    )
    second = RiskRequest(
        correlation_id=uuid4(),
        symbol=SYMBOL,
        side=Side.SELL,
        quantity=Decimal("0.6"),
        price=Decimal("100"),
        strategy_id=STRATEGY,
        intent_id=uuid4(),
    )

    assert engine.evaluate(first, state).allowed
    decision = engine.evaluate(second, state)

    assert not decision.allowed
    assert "insufficient_attributed_inventory" in decision.reason_codes
    assert len(engine.pending) == 1
