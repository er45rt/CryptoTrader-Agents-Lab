"""Phase 10: paper mode against the real offline queue-aware Nautilus simulator.

Offline only: a simulated in-process venue, frozen public-shaped market data, no network, no
credential, no live venue. The paper session itself stays engine-independent; the engine is wired
in as infrastructure through ``NautilusSimulationRunner``.
"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_trader.execution import (
    BookLevel,
    InstrumentSpec,
    MarketBookUpdate,
    MarketTrade,
    QueueAwareConfigurationError,
)
from crypto_trader.execution.nautilus import NautilusSimulationRunner, QueueAwareSettings
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx

INSTRUMENT = InstrumentSpec(symbol="ETHUSDT")
PRICE = Decimal("2700.00")
TS_BASE_NS = 1_767_225_600_000_000_000
MS = 1_000_000


def l2_fixture() -> tuple[tuple[MarketBookUpdate, ...], tuple[MarketTrade, ...]]:
    """A frozen L2 market-by-price fixture with explicit aggressor-side trades."""
    book = MarketBookUpdate(
        levels=[
            BookLevel(side="BUY", price=PRICE, size=Decimal("100.00000000"), order_id=1),
            BookLevel(
                side="SELL", price=Decimal("2701.00"), size=Decimal("100.00000000"), order_id=2
            ),
        ],
        ts_ns=TS_BASE_NS,
        sequence=1,
    )
    trades = (
        # Clears the displayed queue ahead, then fills our order.
        MarketTrade(
            price=PRICE,
            size=Decimal("100.00000000"),
            aggressor="SELLER",
            ts_ns=TS_BASE_NS + 10 * MS,
            trade_id="PF-1",
        ),
        MarketTrade(
            price=PRICE,
            size=Decimal("1.00000000"),
            aggressor="SELLER",
            ts_ns=TS_BASE_NS + 20 * MS,
            trade_id="PF-2",
        ),
    )
    return (book,), trades


def runner(**overrides) -> NautilusSimulationRunner:
    book_updates, trades = l2_fixture()
    return NautilusSimulationRunner(
        instrument=INSTRUMENT,
        book_updates=book_updates,
        trades=trades,
        **overrides,
    )


def aligned_runner() -> NautilusSimulationRunner:
    """Separate positive fixture: book at decision time, trades only afterward."""
    books, trades = l2_fixture()
    offset = int(fx.BASE.timestamp() * 1_000_000_000) - TS_BASE_NS
    return NautilusSimulationRunner(
        instrument=INSTRUMENT,
        book_updates=tuple(replace(book, ts_ns=book.ts_ns + offset) for book in books),
        trades=(
            replace(trades[0], ts_ns=trades[0].ts_ns + offset, trade_id="PF-TRIGGER"),
            replace(trades[0], ts_ns=trades[0].ts_ns + offset + 10 * MS, trade_id="PF-CLEAR"),
            replace(trades[1], ts_ns=trades[1].ts_ns + offset + 10 * MS, trade_id="PF-FILL"),
        ),
    )


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as instance:
        yield instance


def test_paper_session_fills_through_the_queue_aware_simulator(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1")),
        data_feed=fx.feed("2700.00"),
        simulator=aligned_runner(),
    )

    report = session.run()

    assert report.mode == "paper"
    assert report.queue_aware is True
    assert report.book_type == "L2_MBP"
    assert report.engine_version == "1.231.0"
    assert len(report.orders) == 1
    assert len(report.fills) == 1
    assert report.orders[0].state == "filled"
    assert report.fills[0].price == PRICE
    assert report.fills[0].quantity == Decimal("1.00000000")
    assert report.positions == {"ETHUSDT": Decimal("1.00000000")}


def test_paper_session_report_contains_no_engine_types(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1")),
        data_feed=fx.feed("2700.00"),
        simulator=aligned_runner(),
    )

    report = session.run()
    payload = report.to_json()

    for token in ("nautilus", "OrderBookDeltas", "InstrumentId", "BacktestEngine"):
        assert token.lower() not in payload.lower()


def test_paper_session_journals_engine_produced_phase3_contracts(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1")),
        data_feed=fx.feed("2700.00"),
        simulator=aligned_runner(),
    )

    report = session.run()
    types = {event.event_type for event in journal.read_range()}

    assert {"order.state.changed", "execution.fill.recorded"} <= types
    order_events = [
        event for event in journal.read_range() if event.event_type == "order.state.changed"
    ]
    assert all(event.order_id is not None for event in order_events)
    assert all(event.correlation_id == fx.SESSION_ID for event in order_events)
    assert report.fills[0].correlation_id == fx.SESSION_ID


def test_queue_aware_configuration_is_enforced_by_the_adapter():
    with pytest.raises(QueueAwareConfigurationError):
        runner(queue_settings=QueueAwareSettings(True, True, "L1_MBP")).simulate(())


def test_runner_requires_a_non_queue_aware_opt_in_for_other_book_types():
    # Explicitly disabling queue-awareness is allowed, but it must be explicit.
    outcome = runner(
        queue_aware=False, queue_settings=QueueAwareSettings(False, False, "L1_MBP")
    ).simulate(())

    assert outcome.queue_aware is False
    assert outcome.book_type is None
    assert outcome.observations == ()


def test_phase10_fill_must_follow_decision_completion(journal):
    """P0 regression: never replay a midnight fill against a noon decision."""
    from crypto_trader.paper import PaperStateError
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1")),
        data_feed=fx.feed("2700.00"),
        simulator=runner(),
    )
    with pytest.raises(PaperStateError, match="unsubmitted queued intents"):
        session.run()
    events = tuple(journal.read_range())
    decisions = [event for event in events if event.event_type == "strategy.decision.recorded"]
    fills = [event for event in events if event.event_type == "execution.fill.recorded"]
    assert len(decisions) == 1
    assert not fills  # The entire historical midnight tape predates the noon decision.


def test_phase10_eligible_fill_timestamp_is_strictly_after_decision(journal):
    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1")),
        data_feed=fx.feed("2700.00"),
        simulator=aligned_runner(),
    )
    report = session.run()
    assert len(report.fills) == 1
    fills = [
        event for event in journal.read_range()
        if event.event_type == "execution.fill.recorded"
    ]
    assert len(fills) == 1
    assert fills[0].occurred_at > fx.BASE


def test_phase10_native_event_ids_repeat_across_fresh_simulations():
    """Idempotency depends on identities, not identical price/quantity alone."""
    from uuid import UUID
    from crypto_trader.execution import ExecutionRequest, ExecutionSide, ExecutionOrderType

    request = ExecutionRequest.create(
        intent_id=UUID(int=777),
        correlation_id=fx.SESSION_ID,
        symbol="ETHUSDT",
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=PRICE,
    )
    first = runner().simulate([request])
    second = runner().simulate([request])
    assert len(first.phase3_events) >= 3
    assert [
        (event.event_type, event.event_id) for event in first.phase3_events
    ] == [
        (event.event_type, event.event_id) for event in second.phase3_events
    ]
    assert len({event.event_id for event in first.phase3_events}) == len(
        first.phase3_events
    )


def test_phase10_next_risk_decision_observes_prior_fill(journal, monkeypatch):
    """Second risk decision must see the first fill, fees, and new mark."""
    from crypto_trader.paper import PaperStateError
    from crypto_trader.paper.session import TradingSession

    snapshots = []
    original = TradingSession._risk_state

    def inspect_state(self, ledger, observation, now):
        state = original(self, ledger, observation, now)
        snapshots.append(state)
        return state

    monkeypatch.setattr(TradingSession, "_risk_state", inspect_state)
    session = fx.session(
        journal,
        decisions=fx.scripted(
            fx.buy_decision(quantity="1", limit_price="2700.00"),
            fx.buy_decision(quantity="0.25", limit_price="2705.00"),
        ),
        data_feed=fx.feed("2700.00", "2705.00"),
        simulator=aligned_runner(),
    )
    # The second approved intent is beyond this tape. Fail closed at finish,
    # while preserving the first fill and the second decision's risk snapshot.
    with pytest.raises(PaperStateError, match="unsubmitted queued intents"):
        session.run()
    assert sum(event.event_type == "execution.fill.recorded"
               for event in journal.read_range()) == 1
    assert len(snapshots) == 2
    assert snapshots[0].equity_quote == Decimal("100000")
    assert snapshots[1].equity_quote > Decimal("100000")
    assert snapshots[1].peak_equity_quote >= snapshots[1].equity_quote
    assert snapshots[1].position_quantity["ETHUSDT"] >= Decimal("1")


def test_temporal_paper_resume_without_restored_engine_fails_closed(journal):
    from crypto_trader.paper import PaperStateError

    session = fx.session(
        journal,
        decisions=fx.scripted(
            fx.buy_decision(quantity="1"),
            fx.buy_decision(quantity="0.25", limit_price="2705"),
        ),
        data_feed=fx.feed("2700.00", "2705.00"),
        simulator=aligned_runner(),
    )
    with pytest.raises(PaperStateError, match="temporal execution resume"):
        session.run(start_index=1)


def test_temporal_invalid_fill_is_rejected_before_journal_write(journal):
    """A malformed simulator result must not persist a fill that risk cannot accept."""
    from crypto_trader.paper import PaperStateError, ScriptedSimulationRunner, SimulationOutcome

    class EarlyFillTemporal:
        def simulate(self, requests):
            return ScriptedSimulationRunner().simulate(requests)

        def start_temporal(self):
            self.request = None

        def advance_until(self, timestamp_ns):
            return SimulationOutcome()

        def submit_temporal(self, request, *, decided_ns):
            self.request = request

        def finish_temporal(self):
            assert self.request is not None
            return ScriptedSimulationRunner().simulate([self.request])

        def abort_temporal(self):
            pass

    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1")),
        data_feed=fx.feed("2700"),
        simulator=EarlyFillTemporal(),
    )
    with pytest.raises(PaperStateError, match="before its decision"):
        session.run()
    assert not [
        event for event in journal.read_range()
        if event.event_type == "execution.fill.recorded"
    ]


def test_temporal_overlapping_decisions_fail_before_journaling(journal):
    """A decision completing after the next observation cannot consume past data."""
    from datetime import timedelta
    from crypto_trader.paper import PaperStateError

    session = fx.session(
        journal,
        decisions=fx.scripted(
            fx.buy_decision(quantity="1", decided_at=fx.BASE + timedelta(seconds=2)),
            fx.hold_decision(),
        ),
        data_feed=fx.feed("2700", "2701"),
        simulator=aligned_runner(),
    )
    with pytest.raises(PaperStateError, match="overlap|next market observation|non-monotonic"):
        session.run()
    assert not [
        event for event in journal.read_range()
        if event.event_type == "strategy.decision.recorded"
    ]


def test_temporal_pending_order_requires_market_time_coverage():
    """No risk snapshot may ignore a pending timed fill between market ticks."""
    from uuid import UUID
    from crypto_trader.execution import ExecutionRequest, ExecutionSide, ExecutionOrderType

    sim = aligned_runner()
    base = int(fx.BASE.timestamp() * 1_000_000_000)
    sim.start_temporal()
    try:
        sim.advance_until(base)
        request = ExecutionRequest.create(
            intent_id=UUID(int=9876),
            correlation_id=fx.SESSION_ID,
            symbol="ETHUSDT",
            side=ExecutionSide.BUY,
            order_type=ExecutionOrderType.LIMIT,
            quantity=Decimal("1"),
            limit_price=PRICE,
        )
        sim.submit_temporal(request, decided_ns=base)
        sim.advance_until(base + 10 * MS)
        with pytest.raises(RuntimeError, match="unverified|market|latency|timer"):
            sim.advance_until(base + 18 * MS)
    finally:
        sim.abort_temporal()


def test_temporal_three_windows_do_not_replay_or_double_apply_fills():
    """One persistent engine must deliver each native order/fill event exactly once."""
    from uuid import UUID
    from crypto_trader.execution import ExecutionRequest, ExecutionSide, ExecutionOrderType

    sim = aligned_runner()
    base = int(fx.BASE.timestamp() * 1_000_000_000)
    sim.start_temporal()
    try:
        sim.advance_until(base)
        request = ExecutionRequest.create(
            intent_id=UUID(int=10101),
            correlation_id=fx.SESSION_ID,
            symbol="ETHUSDT",
            side=ExecutionSide.BUY,
            order_type=ExecutionOrderType.LIMIT,
            quantity=Decimal("1"),
            limit_price=PRICE,
        )
        sim.submit_temporal(request, decided_ns=base)
        early = sim.advance_until(base + 10 * MS)
        middle = sim.advance_until(base + 20 * MS)
        final = sim.advance_until(base + 30 * MS)
        assert len(early.phase3_events) < len(middle.phase3_events) + len(final.phase3_events)
        assert len(final.observations) >= len(middle.observations) >= len(early.observations)
        fill_observations = [
            obs for obs in final.observations if obs.last_fill_quantity is not None
        ]
        assert len(fill_observations) == 1
        assert sum((obs.last_fill_quantity for obs in fill_observations), Decimal("0")) == Decimal("1")
        fill_events = [
            evt for evt in final.phase3_events if evt.event_type == "execution.fill.recorded"
        ]
        assert len(fill_events) == 1
        assert len({evt.event_id for evt in final.phase3_events}) == len(final.phase3_events)
        assert [obs.ts_ns for obs in final.observations] == sorted(
            obs.ts_ns for obs in final.observations
        )
        completed = sim.finish_temporal()
        assert [evt.event_id for evt in completed.phase3_events] == [
            evt.event_id for evt in final.phase3_events
        ]
    finally:
        sim.abort_temporal()


def test_temporal_orphan_phase3_fill_never_journaled(journal):
    """An event not backed by native observations cannot enter the journal."""
    from crypto_trader.paper import PaperStateError, ScriptedSimulationRunner, SimulationOutcome

    class OrphanTemporal:
        def simulate(self, requests):
            return ScriptedSimulationRunner().simulate(requests)

        def start_temporal(self):
            self.request = None

        def advance_until(self, timestamp_ns):
            return SimulationOutcome()

        def submit_temporal(self, request, *, decided_ns):
            self.request = request

        def finish_temporal(self):
            outcome = ScriptedSimulationRunner().simulate([self.request])
            return SimulationOutcome(observations=(), phase3_events=outcome.phase3_events)

        def abort_temporal(self):
            pass

    session = fx.session(
        journal,
        decisions=fx.scripted(fx.buy_decision(quantity="1")),
        data_feed=fx.feed("2700"),
        simulator=OrphanTemporal(),
    )
    with pytest.raises(PaperStateError):
        session.run()
    assert not [
        event for event in journal.read_range()
        if event.event_type == "execution.fill.recorded"
    ]


def test_temporal_session_rejects_nonfinite_fees_and_future_observations_before_journal(journal):
    """No NaN fee or execution timestamp from the future may reach the ledger."""
    from dataclasses import replace
    from decimal import Decimal
    from crypto_trader.paper import PaperStateError, SimulationOutcome, ScriptedSimulationRunner
    from crypto_trader.execution.ports import ExecutionObservation, OrderLifecycleState

    class PoisonedTemporal:
        def __init__(self, kind):
            self.kind = kind

        def simulate(self, requests):
            return ScriptedSimulationRunner().simulate(requests)

        def start_temporal(self):
            self.request = None

        def advance_until(self, timestamp_ns):
            return SimulationOutcome()

        def submit_temporal(self, request, *, decided_ns):
            self.request = request

        def finish_temporal(self):
            native = ScriptedSimulationRunner().simulate([self.request])
            fill = next(x for x in native.observations if x.last_fill_quantity is not None)
            boundary = int(fx.BASE.timestamp() * 1_000_000_000)
            if self.kind == "fee":
                fill = replace(fill, ts_ns=boundary + 1, fee=Decimal("NaN"))
            else:
                fill = replace(fill, ts_ns=boundary + 1_000_000_000, fee=Decimal("0"))
            return SimulationOutcome(observations=(fill,), phase3_events=native.phase3_events)

        def abort_temporal(self):
            pass

    for kind in ("fee", "future"):
        with pytest.raises(PaperStateError):
            fx.session(
                journal,
                decisions=fx.scripted(fx.buy_decision(quantity="1")),
                data_feed=fx.feed("2700"),
                simulator=PoisonedTemporal(kind),
            ).run()
        assert not [
            item for item in journal.read_range()
            if item.event_type == "execution.fill.recorded"
        ]


def test_temporal_terminal_cancel_then_fill_is_rejected_before_mutation(journal):
    from dataclasses import replace
    from uuid import UUID
    from crypto_trader.paper import PaperStateError, SimulationOutcome
    from crypto_trader.execution.ports import ExecutionRequest, ExecutionSide, ExecutionOrderType, ExecutionObservation, OrderLifecycleState
    from crypto_trader.risk import Side

    session = fx.session(
        journal, decisions=fx.scripted(fx.hold_decision()),
        data_feed=fx.feed("2700"), simulator=aligned_runner(),
    )
    req = ExecutionRequest.create(
        intent_id=UUID(int=10001), correlation_id=fx.SESSION_ID, symbol="ETHUSDT",
        side=ExecutionSide.BUY, order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("1"), limit_price=PRICE,
    )
    base_ns = int(fx.BASE.timestamp() * 1_000_000_000)
    session._reservations[req.intent_id] = (Side.BUY, Decimal("1"), PRICE)
    session._temporal_decided_ns[req.intent_id] = base_ns
    cancel = ExecutionObservation(
        order_reference="SIM-ORDER", intent_id=req.intent_id, correlation_id=fx.SESSION_ID,
        symbol="ETHUSDT", state=OrderLifecycleState.CANCELED,
        filled_quantity=Decimal("0"), remaining_quantity=Decimal("1"),
        ts_ns=base_ns + 10_000_000,
    )
    fill = replace(cancel, state=OrderLifecycleState.PARTIALLY_FILLED,
                   filled_quantity=Decimal("0.25"), remaining_quantity=Decimal("0.75"),
                   last_fill_quantity=Decimal("0.25"), last_fill_price=PRICE,
                   ts_ns=base_ns + 20_000_000)
    with pytest.raises(PaperStateError, match="terminal"):
        session._validate_temporal_delta(
            SimulationOutcome(observations=(cancel, fill)), [req],
            boundary_ns=base_ns + 20_000_000,
        )
    assert session._reservations[req.intent_id][1] == Decimal("1")


def test_native_duplicate_fill_callback_is_rejected_without_extra_observation(monkeypatch):
    """Stable fill IDs must be enforced, not merely generated."""
    from uuid import UUID
    from crypto_trader.execution.nautilus.runtime import NautilusExecutionRuntime
    from crypto_trader.execution.errors import RuntimeStateError
    from crypto_trader.execution import ExecutionRequest, ExecutionSide, ExecutionOrderType

    original = NautilusExecutionRuntime._record_fill_event
    witnessed = []

    def double_callback(self, strategy, event):
        original(self, strategy, event)
        first = (len(self.observations()), len(self.phase3_events()))
        with pytest.raises(RuntimeStateError, match="duplicate native fill"):
            original(self, strategy, event)
        assert (len(self.observations()), len(self.phase3_events())) == first
        witnessed.append(True)

    monkeypatch.setattr(NautilusExecutionRuntime, "_record_fill_event", double_callback)
    request = ExecutionRequest.create(
        intent_id=UUID(int=555), correlation_id=fx.SESSION_ID,
        symbol="ETHUSDT", side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("1"), limit_price=PRICE,
    )
    outcome = runner().simulate([request])
    assert witnessed
    assert sum(obs.last_fill_quantity is not None for obs in outcome.observations) == 1
