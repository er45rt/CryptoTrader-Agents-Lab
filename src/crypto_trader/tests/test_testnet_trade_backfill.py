"""G4 pure deterministic Binance trade-backfill planner tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from crypto_trader.events import (
    ExecutionFillRecorded,
    FillRecordPayload,
    OrderSubmissionAttemptPayload,
    OrderSubmissionAttempted,
    VenueTradeRecorded,
    VenueTradeRecordPayload,
)
from crypto_trader.execution.binance_testnet.backfill import plan_trade_backfill
from crypto_trader.execution.binance_testnet.errors import TradeBackfillError
from crypto_trader.execution.binance_testnet.models import (
    TestnetFill as VenueFill,
    TestnetOrder as VenueOrderSnapshot,
    VenueTrade,
)
from crypto_trader.execution.binance_testnet.translate import (
    PRODUCER,
    canonical_fill_event_id,
    deterministic_order_uuid,
    legacy_fill_event_ids,
    testnet_order_to_phase3_events as build_phase3_events,
    venue_trade_event_id,
)
from crypto_trader.execution.ports import OrderLifecycleState

UTC = timezone.utc
BASE = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SYMBOL = "BTCUSDT"
REFERENCE = "ct-backfill"
VENUE_ORDER_ID = "42"
ORDER_ID = deterministic_order_uuid(REFERENCE)
INTENT_ID = UUID("10000000-0000-4000-8000-000000000001")
CORRELATION_ID = UUID("20000000-0000-4000-8000-000000000001")
STRATEGY_ID = "strategy.backfill"
VENUE_PRODUCER = PRODUCER


def attempt_event(*, side: str = "buy", quantity: str = "1") -> OrderSubmissionAttempted:
    return OrderSubmissionAttempted(
        event_id=UUID("30000000-0000-4000-8000-000000000001"),
        occurred_at=BASE,
        correlation_id=CORRELATION_ID,
        producer="application.production",
        strategy_id=STRATEGY_ID,
        order_id=ORDER_ID,
        symbol=SYMBOL,
        payload=OrderSubmissionAttemptPayload(
            venue_reference=REFERENCE,
            intent_id=INTENT_ID,
            side=side,
            order_type="limit",
            quantity=Decimal(quantity),
            limit_price=Decimal("100"),
            time_in_force="gtc",
            venue_producer=VENUE_PRODUCER,
        ),
    )


def trade(
    trade_id: str = "7",
    *,
    qty: str = "0.4",
    price: str = "100",
    quote: str | None = None,
    commission: str = "0.01",
    commission_asset: str = "USDT",
    seconds: int = 1,
    is_buyer: bool = True,
    is_maker: bool = False,
) -> VenueTrade:
    quantity = Decimal(qty)
    px = Decimal(price)
    return VenueTrade(
        symbol=SYMBOL,
        venue_order_id=VENUE_ORDER_ID,
        trade_id=trade_id,
        price=px,
        quantity=quantity,
        quote_quantity=Decimal(quote) if quote is not None else px * quantity,
        commission=Decimal(commission),
        commission_asset=commission_asset,
        trade_time=BASE + timedelta(seconds=seconds),
        is_buyer=is_buyer,
        is_maker=is_maker,
    )


def order(
    *,
    executed: str = "0.4",
    quote: str = "40",
    state: OrderLifecycleState = OrderLifecycleState.PARTIALLY_FILLED,
    status: str = "PARTIALLY_FILLED",
    original: str = "1",
) -> VenueOrderSnapshot:
    return VenueOrderSnapshot(
        symbol=SYMBOL,
        client_order_id=REFERENCE,
        venue_order_id=VENUE_ORDER_ID,
        state=state,
        status=status,
        original_quantity=Decimal(original),
        executed_quantity=Decimal(executed),
        cummulative_quote_quantity=Decimal(quote),
        transact_time=BASE + timedelta(seconds=2),
    )


def existing_fill(
    item: VenueTrade,
    *,
    event_id=None,
    remaining: str = "0.6",
    price: str | None = None,
    fee: str | None = None,
    fee_asset: str | None = None,
    liquidity: str | None = None,
) -> ExecutionFillRecorded:
    return ExecutionFillRecorded(
        event_id=event_id or canonical_fill_event_id(REFERENCE, item.trade_id),
        occurred_at=item.trade_time,
        correlation_id=CORRELATION_ID,
        producer=VENUE_PRODUCER,
        strategy_id=STRATEGY_ID,
        order_id=ORDER_ID,
        symbol=SYMBOL,
        payload=FillRecordPayload(
            quantity=item.quantity,
            price=Decimal(price) if price is not None else item.price,
            fee=Decimal(fee) if fee is not None else item.commission,
            fee_asset=item.commission_asset if fee_asset is None else fee_asset,
            remaining_quantity=Decimal(remaining),
            liquidity=("maker" if item.is_maker else "taker") if liquidity is None else liquidity,
        ),
    )


def existing_trade_event(item: VenueTrade, *, fill_event_id=None, commission: str | None = None):
    fill_id = fill_event_id or canonical_fill_event_id(REFERENCE, item.trade_id)
    return VenueTradeRecorded(
        event_id=venue_trade_event_id(SYMBOL, VENUE_ORDER_ID, item.trade_id),
        occurred_at=item.trade_time,
        correlation_id=CORRELATION_ID,
        causation_id=fill_id,
        producer=VENUE_PRODUCER,
        strategy_id=STRATEGY_ID,
        order_id=ORDER_ID,
        symbol=SYMBOL,
        payload=VenueTradeRecordPayload(
            venue_reference=REFERENCE,
            venue_order_id=VENUE_ORDER_ID,
            venue_trade_id=item.trade_id,
            fill_event_id=fill_id,
            price=item.price,
            quantity=item.quantity,
            quote_quantity=item.quote_quantity,
            commission=Decimal(commission) if commission is not None else item.commission,
            commission_asset=item.commission_asset,
            trade_time=item.trade_time,
            is_buyer=item.is_buyer,
            is_maker=item.is_maker,
        ),
    )


def plan(journal_events, order_value, trades, *, allowed=("USDT", "BTC")):
    return plan_trade_backfill(
        journal_events,
        orders={REFERENCE: order_value},
        trades_by_reference={REFERENCE: trades},
        allowed_fee_assets=allowed,
    )


def test_missing_fill_and_trade_metadata_are_planned_as_one_linked_pair():
    item = trade()
    result = plan([attempt_event()], order(), [item])

    assert result.references == (REFERENCE,)
    assert result.fill_count == 1
    assert result.venue_trade_count == 1
    fill_event, trade_event = result.events
    assert isinstance(fill_event, ExecutionFillRecorded)
    assert isinstance(trade_event, VenueTradeRecorded)
    assert fill_event.event_id == canonical_fill_event_id(REFERENCE, "7")
    assert fill_event.payload.remaining_quantity == Decimal("0.6")
    assert trade_event.causation_id == fill_event.event_id
    assert trade_event.payload.fill_event_id == fill_event.event_id


def test_multiple_trades_have_monotone_remaining_quantity_and_exact_counts():
    first = trade("7", qty="0.4", quote="40", seconds=1)
    second = trade("8", qty="0.6", quote="60", seconds=2, is_maker=True)
    result = plan(
        [attempt_event()],
        order(executed="1", quote="100", state=OrderLifecycleState.FILLED, status="FILLED"),
        [first, second],
    )

    fills = [event for event in result.events if isinstance(event, ExecutionFillRecorded)]
    assert [event.payload.remaining_quantity for event in fills] == [
        Decimal("0.6"),
        Decimal("0"),
    ]
    assert result.fill_count == 2
    assert result.venue_trade_count == 2


def test_existing_canonical_fill_and_metadata_make_the_plan_empty():
    item = trade()
    fill_event = existing_fill(item)
    metadata = existing_trade_event(item)
    result = plan([attempt_event(), fill_event, metadata], order(), [item])

    assert result.is_empty is True


def test_legacy_fill_is_adopted_and_only_metadata_is_planned():
    item = trade()
    legacy_id = next(iter(legacy_fill_event_ids(REFERENCE, item.trade_id, 1)))
    fill_event = existing_fill(item, event_id=legacy_id)
    result = plan([attempt_event(), fill_event], order(), [item])

    assert result.fill_count == 0
    assert result.venue_trade_count == 1
    metadata = result.events[0]
    assert isinstance(metadata, VenueTradeRecorded)
    assert metadata.payload.fill_event_id == legacy_id
    assert metadata.causation_id == legacy_id


def test_quantity_sum_must_equal_authoritative_executed_quantity():
    with pytest.raises(TradeBackfillError, match="quantity sum"):
        plan([attempt_event()], order(executed="0.5", quote="40"), [trade()])


def test_quote_sum_must_equal_authoritative_cumulative_quote():
    with pytest.raises(TradeBackfillError, match="quote sum"):
        plan([attempt_event()], order(executed="0.4", quote="41"), [trade()])


def test_trade_side_must_match_durable_attempt():
    with pytest.raises(TradeBackfillError, match="buyer/seller"):
        plan([attempt_event(side="buy")], order(), [trade(is_buyer=False)])


def test_fee_asset_must_have_an_approved_conversion():
    with pytest.raises(TradeBackfillError, match="fee asset"):
        plan([attempt_event()], order(), [trade(commission_asset="BNB")], allowed=("USDT", "BTC"))


@pytest.mark.parametrize("trades", [
    (trade("8", seconds=1), trade("7", seconds=2)),
    (trade("7", seconds=1), trade("7", seconds=2)),
])
def test_trade_ids_must_be_strictly_ordered_and_unique(trades):
    with pytest.raises(TradeBackfillError, match="strictly ordered"):
        plan(
            [attempt_event()],
            order(executed="0.8", quote="80"),
            trades,
        )


def test_existing_fill_economics_mismatch_fails_closed():
    item = trade()
    wrong = existing_fill(item, price="101")
    with pytest.raises(TradeBackfillError, match="economics"):
        plan([attempt_event(), wrong], order(), [item])


def test_existing_unmatched_fill_fails_closed():
    item = trade()
    ghost_trade = trade("99", qty="0.1", quote="10")
    ghost_fill = existing_fill(
        ghost_trade,
        event_id=canonical_fill_event_id(REFERENCE, "99"),
        remaining="0.9",
    )
    with pytest.raises(TradeBackfillError, match="not represented"):
        plan([attempt_event(), ghost_fill], order(), [item])


def test_canceled_partial_fill_keeps_nonzero_remaining_quantity():
    item = trade(qty="0.4", quote="40")
    result = plan(
        [attempt_event()],
        order(
            executed="0.4",
            quote="40",
            state=OrderLifecycleState.CANCELED,
            status="CANCELED",
        ),
        [item],
    )

    fill_event = next(event for event in result.events if isinstance(event, ExecutionFillRecorded))
    assert fill_event.payload.remaining_quantity == Decimal("0.6")


def test_order_and_trade_coverage_must_match_exactly():
    with pytest.raises(TradeBackfillError, match="coverage"):
        plan_trade_backfill(
            [attempt_event()],
            orders={REFERENCE: order()},
            trades_by_reference={},
            allowed_fee_assets={"USDT"},
        )


def test_existing_conflicting_venue_trade_metadata_fails_closed():
    item = trade()
    fill_event = existing_fill(item)
    bad_metadata = existing_trade_event(item, commission="0.02")
    with pytest.raises(TradeBackfillError, match="metadata conflicts"):
        plan([attempt_event(), fill_event, bad_metadata], order(), [item])


def test_zero_executed_order_with_no_trades_is_a_noop():
    result = plan(
        [attempt_event()],
        order(
            executed="0",
            quote="0",
            state=OrderLifecycleState.ACCEPTED,
            status="NEW",
        ),
        [],
    )

    assert result.is_empty is True


def test_same_timestamp_trades_use_sequence_numbers_and_replay_cleanly():
    first = trade("7", qty="0.4", quote="40", seconds=1)
    second = trade("8", qty="0.6", quote="60", seconds=1)
    result = plan(
        [attempt_event()],
        order(executed="1", quote="100", state=OrderLifecycleState.FILLED, status="FILLED"),
        [first, second],
    )

    fills = [event for event in result.events if isinstance(event, ExecutionFillRecorded)]
    assert [event.sequence_number for event in fills] == [1, 2]
    assert [event.payload.remaining_quantity for event in fills] == [
        Decimal("0.6"),
        Decimal("0"),
    ]


def test_trade_time_before_durable_attempt_is_refused():
    with pytest.raises(TradeBackfillError, match="precedes"):
        plan([attempt_event()], order(), [trade(seconds=-1)])


def test_mixed_existing_first_fill_and_planned_second_fill_same_timestamp_replays_cleanly():
    first = trade("7", qty="0.4", quote="40", seconds=1)
    second = trade("8", qty="0.6", quote="60", seconds=1)
    first_fill = existing_fill(first, remaining="0.6")
    result = plan(
        [attempt_event(), first_fill],
        order(executed="1", quote="100", state=OrderLifecycleState.FILLED, status="FILLED"),
        [first, second],
    )

    fills = [event for event in result.events if isinstance(event, ExecutionFillRecorded)]
    assert len(fills) == 1
    assert fills[0].event_id == canonical_fill_event_id(REFERENCE, "8")
    assert fills[0].sequence_number == 2
    assert fills[0].payload.remaining_quantity == Decimal("0")


def test_mixed_existing_later_fill_before_missing_earlier_fill_is_refused_by_replay_proof():
    first = trade("7", qty="0.4", quote="40", seconds=1)
    second = trade("8", qty="0.6", quote="60", seconds=1)
    later_fill = existing_fill(second, remaining="0")
    with pytest.raises(TradeBackfillError, match="replay"):
        plan(
            [attempt_event(), later_fill],
            order(executed="1", quote="100", state=OrderLifecycleState.FILLED, status="FILLED"),
            [first, second],
        )


def test_planned_fill_id_collision_with_another_order_is_refused():
    item = trade()
    collision_id = canonical_fill_event_id(REFERENCE, item.trade_id)
    other_reference = "ct-other"
    other_attempt = OrderSubmissionAttempted(
        event_id=collision_id,
        occurred_at=BASE,
        correlation_id=UUID("20000000-0000-4000-8000-000000000099"),
        producer="application.production",
        strategy_id=STRATEGY_ID,
        order_id=deterministic_order_uuid(other_reference),
        symbol=SYMBOL,
        payload=OrderSubmissionAttemptPayload(
            venue_reference=other_reference,
            intent_id=UUID("10000000-0000-4000-8000-000000000099"),
            side="buy",
            order_type="limit",
            quantity=Decimal("1"),
            limit_price=Decimal("100"),
            time_in_force="gtc",
            venue_producer=VENUE_PRODUCER,
        ),
    )
    with pytest.raises(TradeBackfillError, match="collides"):
        plan([attempt_event(), other_attempt], order(), [item])


def test_trade_time_must_not_move_backwards_as_trade_ids_increase():
    first = trade("7", qty="0.4", quote="40", seconds=2)
    second = trade("8", qty="0.6", quote="60", seconds=1)
    with pytest.raises(TradeBackfillError, match="time moved backwards"):
        plan(
            [attempt_event()],
            order(executed="1", quote="100", state=OrderLifecycleState.FILLED, status="FILLED"),
            [first, second],
        )


def test_allowed_fee_assets_rejects_accidental_string_input():
    with pytest.raises(TradeBackfillError, match="not a string"):
        plan_trade_backfill(
            [attempt_event()],
            orders={REFERENCE: order()},
            trades_by_reference={REFERENCE: [trade()]},
            allowed_fee_assets="USDT",
        )


def test_filled_state_at_last_trade_timestamp_allows_authoritative_late_fill_backfill():
    first = trade("7", qty="0.4", quote="40", seconds=1)
    second = trade("8", qty="0.6", quote="60", seconds=2)
    venue = order(
        executed="1",
        quote="100",
        state=OrderLifecycleState.FILLED,
        status="FILLED",
    )
    lifecycle = build_phase3_events(
        venue,
        correlation_id=CORRELATION_ID,
        occurred_at=second.trade_time,
    )[0]

    result = plan([attempt_event(), lifecycle], venue, [first, second])

    assert result.fill_count == 2
    assert result.venue_trade_count == 2
    fills = [event for event in result.events if isinstance(event, ExecutionFillRecorded)]
    assert fills[-1].payload.remaining_quantity == Decimal("0")


def test_real_live_writer_maker_fill_is_adopted_despite_legacy_taker_liquidity():
    item = trade(is_maker=True)
    observed = VenueOrderSnapshot(
        symbol=SYMBOL,
        client_order_id=REFERENCE,
        venue_order_id=VENUE_ORDER_ID,
        state=OrderLifecycleState.PARTIALLY_FILLED,
        status="PARTIALLY_FILLED",
        original_quantity=Decimal("1"),
        executed_quantity=Decimal("0.4"),
        cummulative_quote_quantity=Decimal("40"),
        fills=(
            VenueFill(
                price=item.price,
                quantity=item.quantity,
                commission=item.commission,
                commission_asset=item.commission_asset,
                trade_id=item.trade_id,
            ),
        ),
        transact_time=item.trade_time,
    )
    state_event, live_fill = build_phase3_events(
        observed,
        correlation_id=CORRELATION_ID,
        occurred_at=item.trade_time,
    )
    assert live_fill.payload.liquidity == "taker"

    result = plan([attempt_event(), state_event, live_fill], order(), [item])

    assert result.fill_count == 0
    assert result.venue_trade_count == 1
    metadata = result.events[0]
    assert isinstance(metadata, VenueTradeRecorded)
    assert metadata.payload.is_maker is True
    assert metadata.payload.fill_event_id == live_fill.event_id


def test_per_trade_quote_quantity_must_match_price_times_quantity():
    item = trade(quote="41")
    with pytest.raises(TradeBackfillError, match="price multiplied by quantity"):
        plan([attempt_event()], order(quote="41"), [item])


def test_rejected_order_cannot_carry_executed_quantity_or_trades():
    with pytest.raises(TradeBackfillError, match="status and executed quantity"):
        plan(
            [attempt_event()],
            order(
                executed="0.4",
                quote="40",
                state=OrderLifecycleState.REJECTED,
                status="REJECTED",
            ),
            [trade()],
        )


def test_planner_is_deterministic_on_repeated_calls():
    item = trade()
    first = plan([attempt_event()], order(), [item])
    second = plan([attempt_event()], order(), [item])

    assert first == second
    assert [event.event_id for event in first.events] == [
        event.event_id for event in second.events
    ]


def test_duplicate_venue_order_id_across_references_is_refused():
    other_reference = "ct-backfill-other"
    other_order_id = deterministic_order_uuid(other_reference)
    other_correlation = UUID("20000000-0000-4000-8000-000000000099")
    other_attempt = OrderSubmissionAttempted(
        event_id=UUID("30000000-0000-4000-8000-000000000099"),
        occurred_at=BASE,
        correlation_id=other_correlation,
        producer="application.production",
        strategy_id=STRATEGY_ID,
        order_id=other_order_id,
        symbol=SYMBOL,
        payload=OrderSubmissionAttemptPayload(
            venue_reference=other_reference,
            intent_id=UUID("10000000-0000-4000-8000-000000000099"),
            side="buy",
            order_type="limit",
            quantity=Decimal("1"),
            limit_price=Decimal("100"),
            time_in_force="gtc",
            venue_producer=VENUE_PRODUCER,
        ),
    )
    other_order = VenueOrderSnapshot(
        symbol=SYMBOL,
        client_order_id=other_reference,
        venue_order_id=VENUE_ORDER_ID,
        state=OrderLifecycleState.ACCEPTED,
        status="NEW",
        original_quantity=Decimal("1"),
        executed_quantity=Decimal("0"),
        cummulative_quote_quantity=Decimal("0"),
        transact_time=BASE + timedelta(seconds=2),
    )

    with pytest.raises(TradeBackfillError, match="same venue order id"):
        plan_trade_backfill(
            [attempt_event(), other_attempt],
            orders={REFERENCE: order(executed="0", quote="0", state=OrderLifecycleState.ACCEPTED, status="NEW"), other_reference: other_order},
            trades_by_reference={REFERENCE: [], other_reference: []},
            allowed_fee_assets={"USDT", "BTC"},
        )
