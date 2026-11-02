"""P-06 native journal intent provenance: strict opt-in without legacy replay changes.

All native executions are local NautilusTrader L2_MBP simulations with frozen data.
No broker, network, credentials, Testnet or live orders are used.
"""
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest

from crypto_trader.events import (
    EXECUTION_FILL_EVENT_TYPE, ORDER_STATE_EVENT_TYPE, TRADE_INTENT_EVENT_TYPE,
)
from crypto_trader.execution.nautilus import NautilusSimulationRunner
from crypto_trader.paper import PaperStateError, report_from_journal
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx
from crypto_trader.tests.test_paper_nautilus import aligned_runner


@pytest.fixture(scope="module")
def native_partial_events(tmp_path_factory):
    original = aligned_runner()
    simulator = NautilusSimulationRunner(
        instrument=original.instrument,
        book_updates=original.book_updates,
        trades=(*original.trades[:-1], replace(
            original.trades[-1], size=Decimal("0.60000000"),
            trade_id="P06-STRICT-PARTIAL")),
    )
    path = tmp_path_factory.mktemp("p06-native-provenance") / "journal.sqlite"
    with SQLiteEventJournal(path) as journal:
        session = fx.session(
            journal,
            decisions=fx.scripted(fx.buy_decision(
                quantity="1.5", limit_price="2700.00")),
            data_feed=fx.feed("2700.00"),
            simulator=simulator,
        )
        actual = session.run()
        assert actual.queue_aware is True
        assert actual.engine_version == "1.231.0"
        assert actual.book_type == "L2_MBP"
        events = tuple(journal.read_range())
    assert len(events) == len({event.event_id for event in events})
    assert any(event.event_type == EXECUTION_FILL_EVENT_TYPE for event in events)
    return events


def _replay(events, *, strict=True):
    return report_from_journal(
        events,
        session_id=fx.SESSION_ID,
        mode="paper",
        symbol="ETHUSDT",
        strategy_id="strategy-paper-1",
        strict_native_provenance=strict,
    )


def _changed(events, event_type, **changes):
    return tuple(
        event.model_copy(update=changes) if event.event_type == event_type else event
        for event in events
    )


def test_p06_strict_native_replay_accepts_verified_partial_and_remaining(native_partial_events):
    recovered = _replay(native_partial_events)
    assert len(recovered.orders) == len(recovered.fills) == 1
    assert recovered.orders[0].state == "partially_filled"
    assert recovered.orders[0].filled_quantity == Decimal("0.6")
    assert recovered.orders[0].remaining_quantity == Decimal("0.9")
    assert recovered.fills[0].remaining_quantity == Decimal("0.9")


def test_p06_native_completion_rejects_corrupted_persisted_origin(tmp_path, monkeypatch):
    """A native run must verify the durable intent link before returning success."""
    with SQLiteEventJournal(tmp_path / "native-run.sqlite") as journal:
        original_read = journal.read_by_correlation

        def read_corrupted(correlation_id):
            events = tuple(original_read(correlation_id))
            return tuple(
                event.model_copy(update={"causation_id": None})
                if event.event_type == ORDER_STATE_EVENT_TYPE else event
                for event in events
            )

        monkeypatch.setattr(journal, "read_by_correlation", read_corrupted)
        session = fx.session(
            journal,
            decisions=fx.scripted(fx.buy_decision(quantity="1", limit_price="2700.00")),
            data_feed=fx.feed("2700.00"),
            simulator=aligned_runner(),
        )
        with pytest.raises(PaperStateError, match="order-to-intent provenance"):
            session.run()


def test_p06_strict_native_rejects_missing_order_origin(native_partial_events):
    corrupted = _changed(native_partial_events, ORDER_STATE_EVENT_TYPE, causation_id=None)
    with pytest.raises(PaperStateError, match="order-to-intent provenance"):
        _replay(corrupted)


def test_p06_strict_native_rejects_unknown_order_origin(native_partial_events):
    foreign = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    corrupted = _changed(native_partial_events, ORDER_STATE_EVENT_TYPE, causation_id=foreign)
    with pytest.raises(PaperStateError, match="unknown durable intent"):
        _replay(corrupted)


def test_p06_strict_native_rejects_missing_fill_origin(native_partial_events):
    corrupted = _changed(native_partial_events, EXECUTION_FILL_EVENT_TYPE, causation_id=None)
    with pytest.raises(PaperStateError, match="fill-to-intent provenance"):
        _replay(corrupted)


def test_p06_strict_native_rejects_foreign_fill_origin(native_partial_events):
    foreign = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    corrupted = _changed(native_partial_events, EXECUTION_FILL_EVENT_TYPE, causation_id=foreign)
    with pytest.raises(PaperStateError, match="fill/order intent provenance mismatch"):
        _replay(corrupted)


def test_p06_strict_native_rejects_missing_durable_intent(native_partial_events):
    corrupted = tuple(
        event for event in native_partial_events
        if event.event_type != TRADE_INTENT_EVENT_TYPE
    )
    with pytest.raises(PaperStateError, match="unknown durable intent"):
        _replay(corrupted)


def test_p06_strict_native_rejects_falsified_remaining(native_partial_events):
    corrupted = tuple(
        event.model_copy(update={"payload": event.payload.model_copy(
            update={"remaining_quantity": Decimal("0.8")})})
        if event.event_type == EXECUTION_FILL_EVENT_TYPE else event
        for event in native_partial_events
    )
    with pytest.raises(PaperStateError, match="does not reconcile"):
        _replay(corrupted)


def test_p06_default_legacy_replay_is_unchanged(native_partial_events):
    corrupted = _changed(native_partial_events, ORDER_STATE_EVENT_TYPE, causation_id=None)
    recovered = _replay(corrupted, strict=False)
    assert recovered.orders[0].filled_quantity == Decimal("0.6")
    assert recovered.orders[0].remaining_quantity == Decimal("0.9")


def test_p06_native_provenance_flag_requires_bool(native_partial_events):
    with pytest.raises(PaperStateError, match="must be a boolean"):
        report_from_journal(
            native_partial_events,
            session_id=fx.SESSION_ID,
            mode="paper",
            symbol="ETHUSDT",
            strategy_id="strategy-paper-1",
            strict_native_provenance="yes",
        )


def _split_native_partial_fill(events, *, first_remaining):
    """Split one actual Nautilus L2_MBP partial fill into two durable fill events."""
    from decimal import Decimal
    from uuid import UUID

    native = [e for e in events if e.event_type == EXECUTION_FILL_EVENT_TYPE]
    assert len(native) == 1
    original = native[0]
    assert original.payload.quantity == Decimal("0.6")
    assert original.payload.remaining_quantity == Decimal("0.9")
    first = original.model_copy(update={
        "event_id": UUID("a3a3a3a3-aaaa-4aaa-8aaa-aaaaaaa3a3a3"),
        "payload": original.payload.model_copy(update={
            "quantity": Decimal("0.3"),
            "remaining_quantity": first_remaining,
        }),
    })
    second = original.model_copy(update={"payload": original.payload.model_copy(update={
        "quantity": Decimal("0.3"),
        "remaining_quantity": Decimal("0.9"),
    })})
    return tuple(
        part for event in events
        for part in ((first, second) if event is original else (event,))
    )


def test_p06_strict_multifill_accepts_each_valid_intermediate_leaves(native_partial_events):
    complete = _split_native_partial_fill(
        native_partial_events, first_remaining=Decimal("1.2"),
    )
    recovered = _replay(complete, strict=True)
    assert len(recovered.fills) == 2
    assert recovered.orders[0].filled_quantity == Decimal("0.6")
    assert recovered.orders[0].remaining_quantity == Decimal("0.9")


def test_p06_strict_multifill_rejects_bad_intermediate_leaves(native_partial_events):
    incorrect = _split_native_partial_fill(
        native_partial_events, first_remaining=Decimal("0.8"),
    )
    # The final order leaves still balance, but the first persisted fill is corrupt.
    assert len({e.event_id for e in incorrect}) == len(incorrect)
    with pytest.raises(PaperStateError, match="(remaining|reconcile|leaves)"):
        _replay(incorrect, strict=True)


def test_p06_non_strict_multifill_preserves_legacy_replay(native_partial_events):
    incorrect = _split_native_partial_fill(
        native_partial_events, first_remaining=Decimal("0.8"),
    )
    recovered = _replay(incorrect, strict=False)
    assert len(recovered.fills) == 2
    assert recovered.orders[0].remaining_quantity == Decimal("0.9")


def test_p06_strict_report_retains_verified_intent_ids_for_orders_and_fills(
    native_partial_events,
):
    """Verified paper order/fill report provenance must survive replay projection."""
    durable_intents = {
        event.event_id for event in native_partial_events
        if event.event_type == TRADE_INTENT_EVENT_TYPE
    }
    assert len(durable_intents) == 1
    expected = next(iter(durable_intents))
    strict = _replay(native_partial_events, strict=True)
    assert strict.orders and strict.fills
    assert all(order.intent_id == expected for order in strict.orders)
    assert all(fill.intent_id == expected for fill in strict.fills)
    # The non-strict report is a public legacy serialization; no byte drift.
    legacy = _replay(native_partial_events, strict=False)
    assert all(order.intent_id is None for order in legacy.orders)
    assert all(fill.intent_id is None for fill in legacy.fills)
