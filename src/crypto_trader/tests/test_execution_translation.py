"""Gate 5/6: inbound and outbound translation tests."""

from datetime import timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from crypto_trader.events import ExecutionFillRecorded, OrderStateChanged
from crypto_trader.execution import (
    ExecutionObservation,
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionSide,
    ExecutionTimeInForce,
    InstrumentSpec,
    OrderLifecycleState,
    TranslationError,
    UnsupportedExecutionRequestError,
)
from crypto_trader.execution.nautilus import inbound, outbound

from nautilus_trader.model.enums import OrderSide, TimeInForce

INSTRUMENT = InstrumentSpec(symbol="ETHUSDT")
TS_NS = 1_767_225_600_010_000_000


def make_request(**overrides) -> ExecutionRequest:
    fields = {
        "correlation_id": uuid4(),
        "symbol": "ETHUSDT",
        "side": ExecutionSide.BUY,
        "order_type": ExecutionOrderType.LIMIT,
        "quantity": Decimal("1.50000000"),
        "limit_price": Decimal("2700.00"),
    }
    fields.update(overrides)
    return ExecutionRequest.create(**fields)


# --- inbound ---

def test_inbound_side_and_time_in_force_mapping_is_explicit():
    assert inbound.SIDE_TO_ENGINE[ExecutionSide.BUY] is OrderSide.BUY
    assert inbound.SIDE_TO_ENGINE[ExecutionSide.SELL] is OrderSide.SELL
    assert inbound.TIF_TO_ENGINE[ExecutionTimeInForce.GTC] is TimeInForce.GTC
    assert inbound.TIF_TO_ENGINE[ExecutionTimeInForce.IOC] is TimeInForce.IOC
    assert inbound.TIF_TO_ENGINE[ExecutionTimeInForce.FOK] is TimeInForce.FOK


def test_correlation_tags_carry_intent_and_correlation_identity():
    request = make_request()

    tags = inbound.correlation_tags(request)

    assert f"intent:{request.intent_id}" in tags
    assert f"corr:{request.correlation_id}" in tags


def test_decimal_quantity_and_price_are_preserved_exactly():
    qty_value = Decimal("1.23000000")
    price_value = Decimal("2700.10")

    assert str(inbound.to_quantity(qty_value, INSTRUMENT)) == "1.23000000"
    assert str(inbound.to_price(price_value, INSTRUMENT)) == "2700.10"


def test_inbound_refuses_unrepresentable_precision():
    with pytest.raises(UnsupportedExecutionRequestError):
        inbound.to_price(Decimal("2700.001"), INSTRUMENT)

    with pytest.raises(UnsupportedExecutionRequestError):
        inbound.to_quantity(Decimal("1.123456789"), INSTRUMENT)


def test_inbound_book_and_trade_translation_uses_public_types():
    from crypto_trader.execution import BookLevel, MarketBookUpdate, MarketTrade

    deltas = inbound.to_book_deltas(
        MarketBookUpdate(
            levels=[BookLevel(side="BUY", price=Decimal("2700.00"), size=Decimal("100.00000000"), order_id=1)],
            ts_ns=TS_NS,
            sequence=1,
        ),
        INSTRUMENT,
    )
    tick = inbound.to_trade_tick(
        MarketTrade(
            price=Decimal("2700.00"),
            size=Decimal("80.00000000"),
            aggressor="SELLER",
            ts_ns=TS_NS,
            trade_id="T1",
        ),
        INSTRUMENT,
    )

    assert str(deltas.instrument_id) == "ETHUSDT.SIM"
    assert len(deltas.deltas) == 1
    assert str(tick.price) == "2700.00"


def test_inbound_refuses_unsupported_book_side_and_aggressor():
    from crypto_trader.execution import BookLevel, MarketBookUpdate, MarketTrade

    with pytest.raises(TranslationError):
        inbound.to_book_deltas(
            MarketBookUpdate(levels=[BookLevel(side="SIDEWAYS", price=Decimal("1"), size=Decimal("1"), order_id=1)], ts_ns=TS_NS),
            INSTRUMENT,
        )

    with pytest.raises(TranslationError):
        inbound.to_trade_tick(
            MarketTrade(price=Decimal("1"), size=Decimal("1"), aggressor="MAYBE", ts_ns=TS_NS, trade_id="T"),
            INSTRUMENT,
        )


# --- outbound ---

@pytest.mark.parametrize(
    "engine_status,expected",
    [
        ("SUBMITTED", OrderLifecycleState.SUBMITTED),
        ("ACCEPTED", OrderLifecycleState.ACCEPTED),
        ("PARTIALLY_FILLED", OrderLifecycleState.PARTIALLY_FILLED),
        ("FILLED", OrderLifecycleState.FILLED),
        ("CANCELED", OrderLifecycleState.CANCELED),
        ("REJECTED", OrderLifecycleState.REJECTED),
        ("EXPIRED", OrderLifecycleState.EXPIRED),
    ],
)
def test_outbound_status_mapping_is_explicit(engine_status, expected):
    assert outbound.state_from_engine_status(engine_status) is expected


@pytest.mark.parametrize("unknown", ["PENDING_UPDATE", "PENDING_CANCEL", "INITIALIZED", "EMULATED"])
def test_outbound_unknown_status_fails_visibly(unknown):
    with pytest.raises(TranslationError) as excinfo:
        outbound.state_from_engine_status(unknown)

    assert unknown in str(excinfo.value)


def test_order_uuid_mapping_is_deterministic_and_identity_preserving():
    first = outbound.deterministic_order_uuid("O-20260101-000000-001-000-1")
    second = outbound.deterministic_order_uuid("O-20260101-000000-001-000-1")
    other = outbound.deterministic_order_uuid("O-20260101-000000-001-000-2")

    assert isinstance(first, UUID)
    assert first == second
    assert first != other


def test_phase3_order_state_event_preserves_correlation_and_symbol():
    request = make_request()
    observation = ExecutionObservation(
        order_reference="O-1",
        intent_id=request.intent_id,
        correlation_id=request.correlation_id,
        symbol="ETHUSDT",
        state=OrderLifecycleState.ACCEPTED,
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("1.50000000"),
        ts_ns=TS_NS,
    )

    event = outbound.to_phase3_order_state_event(observation, sequence_number=1)

    assert isinstance(event, OrderStateChanged)
    assert event.correlation_id == request.correlation_id
    assert event.symbol == "ETHUSDT"
    assert event.payload.state == "accepted"
    assert event.occurred_at.tzinfo == timezone.utc
    assert event.order_id == outbound.deterministic_order_uuid("O-1")
    assert event.sequence_number == 1


def test_phase3_fill_event_preserves_decimal_quantities_and_prices():
    observation = ExecutionObservation(
        order_reference="O-1",
        intent_id=uuid4(),
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        state=OrderLifecycleState.PARTIALLY_FILLED,
        filled_quantity=Decimal("10.00000000"),
        remaining_quantity=Decimal("40.00000000"),
        last_fill_quantity=Decimal("10.00000000"),
        last_fill_price=Decimal("2700.00"),
        fee=Decimal("0.01000000"),
        fee_asset="USDT",
        ts_ns=TS_NS,
    )

    event = outbound.to_phase3_fill_event(observation)

    assert isinstance(event, ExecutionFillRecorded)
    assert event.payload.quantity == Decimal("10.00000000")
    assert event.payload.price == Decimal("2700.00")
    assert event.payload.remaining_quantity == Decimal("40.00000000")
    assert event.payload.fee == Decimal("0.01000000")
    assert event.symbol == "ETHUSDT"


def test_fill_event_translation_requires_a_real_fill():
    observation = ExecutionObservation(
        order_reference="O-1",
        intent_id=uuid4(),
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        state=OrderLifecycleState.ACCEPTED,
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("1"),
        ts_ns=TS_NS,
    )

    with pytest.raises(TranslationError):
        outbound.to_phase3_fill_event(observation)


def test_translation_refuses_to_guess_a_missing_timestamp():
    observation = ExecutionObservation(
        order_reference="O-1",
        intent_id=uuid4(),
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        state=OrderLifecycleState.ACCEPTED,
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("1"),
        ts_ns=0,
    )

    with pytest.raises(TranslationError):
        outbound.to_phase3_order_state_event(observation)


def test_explicit_nautilus_fill_identity_is_stable_and_distinguishes_partials():
    """Native-trade-backed IDs pass unchanged into the Phase 3 fill contract."""
    from uuid import UUID
    from decimal import Decimal
    from crypto_trader.execution.ports import ExecutionObservation, OrderLifecycleState
    from crypto_trader.execution.nautilus import outbound

    observation = ExecutionObservation(
        order_reference="SIM-SAME-ORDER",
        intent_id=UUID(int=99),
        correlation_id=UUID(int=100),
        symbol="ETHUSDT",
        state=OrderLifecycleState.PARTIALLY_FILLED,
        filled_quantity=Decimal("0.25"),
        remaining_quantity=Decimal("0.75"),
        last_fill_quantity=Decimal("0.25"),
        last_fill_price=Decimal("2700"),
        ts_ns=1_767_225_600_020_000_000,
    )
    first = outbound.to_phase3_fill_event(observation, event_id=UUID(int=700))
    replay = outbound.to_phase3_fill_event(observation, event_id=UUID(int=700))
    distinct_trade = outbound.to_phase3_fill_event(observation, event_id=UUID(int=701))
    assert first.event_id == replay.event_id
    assert first.event_id != distinct_trade.event_id
    assert first.order_id == distinct_trade.order_id


# --- Terminal-event audit provenance: explicit native reasons, never invented ---

@pytest.mark.parametrize(
    ("state", "native_reason"),
    [
        (OrderLifecycleState.REJECTED, "INSUFFICIENT_BALANCE"),
        (OrderLifecycleState.CANCELED, "source_event=OrderCanceled; venue_reason=not supplied"),
        (OrderLifecycleState.EXPIRED, "source_event=OrderExpired; venue_reason=not supplied"),
    ],
)
def test_terminal_phase3_translation_preserves_explicit_auditable_reason(
    state, native_reason
):
    """Terminal Phase3 events require a source-backed reason without contract changes."""
    observation = ExecutionObservation(
        order_reference="SIM-TERMINAL", intent_id=UUID(int=811),
        correlation_id=UUID(int=812), symbol="ETHUSDT", state=state,
        filled_quantity=Decimal("0"), remaining_quantity=Decimal("1"),
        ts_ns=TS_NS,
    )
    event = outbound.to_phase3_order_state_event(
        observation, reason=native_reason, event_id=UUID(int=813)
    )
    assert event.payload.state == state.value
    assert event.payload.reason == native_reason
    assert event.event_id == UUID(int=813)


def test_terminal_translator_rejects_missing_reason_and_blank_rejection():
    for state in (OrderLifecycleState.REJECTED, OrderLifecycleState.CANCELED, OrderLifecycleState.EXPIRED):
        observation = ExecutionObservation(
            order_reference="SIM-TERMINAL", intent_id=UUID(int=821),
            correlation_id=UUID(int=822), symbol="ETHUSDT", state=state,
            filled_quantity=Decimal("0"), remaining_quantity=Decimal("1"),
            ts_ns=TS_NS,
        )
        with pytest.raises(TranslationError, match="reason"):
            outbound.to_phase3_order_state_event(observation)
        with pytest.raises(TranslationError, match="reason"):
            outbound.to_phase3_order_state_event(observation, reason="   ")


def test_terminal_runtime_captures_native_reject_reason_and_source_limited_fallback():
    """Native event reason reaches Phase3 without extending the shared observation port."""
    from types import SimpleNamespace
    from crypto_trader.execution.nautilus.runtime import NautilusExecutionRuntime

    cases = (
        (OrderLifecycleState.REJECTED, "order_rejected", "RISK_LIMIT", "RISK_LIMIT"),
        (OrderLifecycleState.CANCELED, "order_canceled", None,
         "source_event=OrderCanceled; venue_reason=not supplied"),
        (OrderLifecycleState.EXPIRED, "order_expired", None,
         "source_event=OrderExpired; venue_reason=not supplied"),
    )
    for state, label, reason, expected in cases:
        runtime = NautilusExecutionRuntime(INSTRUMENT)
        request = make_request(
            intent_id=UUID(int=841),
            correlation_id=UUID(int=842),
            quantity=Decimal("1.00000000"),
        )
        reference = "SIM-TEST-TERMINAL"
        runtime._pending[reference] = request
        order = SimpleNamespace(
            client_order_id=reference,
            status=SimpleNamespace(name=state.name),
            filled_qty=Decimal("0"),
            leaves_qty=Decimal("1"),
            ts_last=TS_NS,
        )
        strategy = SimpleNamespace(
            cache=SimpleNamespace(order=lambda _: order),
        )
        event = SimpleNamespace(
            client_order_id=reference,
            ts_event=TS_NS,
            reason=reason,
        )
        runtime._record_order_event(strategy, label, event)
        assert len(runtime.phase3_events()) == 1
        assert runtime.phase3_events()[0].payload.reason == expected
        assert runtime.observations()[0].state is state


def test_rejected_native_event_without_actual_reason_fails_closed():
    from types import SimpleNamespace
    from crypto_trader.execution.nautilus.runtime import NautilusExecutionRuntime

    runtime = NautilusExecutionRuntime(INSTRUMENT)
    request = make_request(
        intent_id=UUID(int=861), correlation_id=UUID(int=862),
        quantity=Decimal("1.00000000"),
    )
    reference = "SIM-REJECTION-WITHOUT-REASON"
    runtime._pending[reference] = request
    order = SimpleNamespace(
        client_order_id=reference, status=SimpleNamespace(name="REJECTED"),
        filled_qty=Decimal("0"), leaves_qty=Decimal("1"), ts_last=TS_NS,
    )
    strategy = SimpleNamespace(cache=SimpleNamespace(order=lambda _: order))
    event = SimpleNamespace(client_order_id=reference, ts_event=TS_NS, reason=" ")
    with pytest.raises(TranslationError, match="reason"):
        runtime._record_order_event(strategy, "order_rejected", event)
    assert runtime.observations() == ()
    assert runtime.phase3_events() == ()


def test_real_offline_ioc_cancel_preserves_reason_and_reservation(tmp_path):
    """Native Nautilus IOC cancellation passes temporal validation and journal audit."""
    from crypto_trader.execution import ExecutionTimeInForce
    from crypto_trader.tests.test_paper_nautilus import runner, TS_BASE_NS, MS
    from crypto_trader.tests import paper_fixtures as fx
    from crypto_trader.paper import ScriptedSimulationRunner
    from crypto_trader.risk import PendingIntent, Side
    from crypto_trader.persistence import SQLiteEventJournal

    request = make_request(
        intent_id=UUID(int=11881),
        correlation_id=fx.SESSION_ID,
        quantity=Decimal("1.00000000"),
        limit_price=Decimal("2699.00"),  # never marketable against this frozen tape
        time_in_force=ExecutionTimeInForce.IOC,
    )
    outcome = runner().simulate([request])
    native_states = [obs.state.value for obs in outcome.observations]
    assert native_states == ["submitted", "accepted", "canceled"]
    marker = "source_event=OrderCanceled; venue_reason=not supplied"
    terminal_events = [
        evt for evt in outcome.phase3_events
        if evt.event_type == "order.state.changed" and evt.payload.state == "canceled"
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0].payload.reason == marker

    with SQLiteEventJournal(tmp_path / "ioc-terminal-audit.sqlite") as journal:
        paper = fx.session(
            journal, decisions=fx.scripted(fx.hold_decision()),
            data_feed=fx.feed("2700"), simulator=ScriptedSimulationRunner(),
        )
        paper._reservations[request.intent_id] = (
            Side.BUY, Decimal("1"), Decimal("2699"),
        )
        paper._pending.register(PendingIntent(
            intent_id=request.intent_id, symbol="ETHUSDT",
            strategy_id=paper.config.strategy_id, side=Side.BUY,
            quantity=Decimal("1"), notional=Decimal("2699"),
        ))
        paper._temporal_decided_ns[request.intent_id] = TS_BASE_NS - 1
        terminal, ids = paper._validate_temporal_delta(
            outcome, [request], boundary_ns=TS_BASE_NS + 20 * MS,
        )
        paper._journal_simulation(outcome)
        paper._reconcile_temporal_reservations(outcome)
        paper._temporal_terminal_intents = terminal
        paper._temporal_seen_event_ids.update(ids)
        assert request.intent_id in terminal
        assert request.intent_id not in paper._reservations
        assert all(item.intent_id != request.intent_id for item in paper._pending.items())
        persisted = [
            evt for evt in journal.read_range()
            if evt.event_type == "order.state.changed" and evt.payload.state == "canceled"
        ]
        assert len(persisted) == 1
        assert persisted[0].payload.reason == marker
