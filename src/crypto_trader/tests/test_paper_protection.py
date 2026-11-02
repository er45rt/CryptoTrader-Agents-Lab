"""Stop-loss / take-profit through the real session and the native simulator, offline.

Tapes here are synthetic, so these tests prove mechanics only, never a strategy's quality.
"""

import socket
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from crypto_trader.execution import BookLevel, InstrumentSpec, MarketBookUpdate, MarketTrade
from crypto_trader.execution.nautilus import NautilusSimulationRunner
from crypto_trader.paper import (
    PaperStateError, ProtectedDecision, ProtectionBook,
    ProtectionPlan, ScriptedSimulationRunner, TakeProfitLevel, report_from_journal,
)
from crypto_trader.paper.config import SpotCashSettings
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx

INSTRUMENT = InstrumentSpec(symbol="ETHUSDT")
MS = 1_000_000
SECOND = 1_000_000_000
BASE_NS = int(fx.BASE.timestamp()) * SECOND
D = Decimal


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("network refused by offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as instance:
        yield instance


def book(sequence, second, bid, ask, size="100", previous=None, offset=0):
    """A top-of-book replacement: the previous levels leave, then the new ones arrive."""
    ts = BASE_NS + second * SECOND + offset
    rows = []
    if previous is not None:
        rows.append(MarketBookUpdate(levels=[
            BookLevel(side="BUY", price=D(previous[0]), size=D(size), order_id=1),
            BookLevel(side="SELL", price=D(previous[1]), size=D(size), order_id=2),
        ], ts_ns=ts, sequence=sequence * 10, action="DELETE"))
    rows.append(MarketBookUpdate(levels=[
        BookLevel(side="BUY", price=D(bid), size=D(size), order_id=1),
        BookLevel(side="SELL", price=D(ask), size=D(size), order_id=2),
    ], ts_ns=ts, sequence=sequence * 10 + 5))
    return rows


def tape(steps, *, size="100", cash=True):
    """steps: (second, bid, ask, trade price or None, aggressor, trade size)."""
    books, trades, previous = [], [], None
    for sequence, (second, bid, ask, price, aggressor, trade_size) in enumerate(steps, start=1):
        if previous != (bid, ask):
            books.extend(book(sequence, second, bid, ask, size=size, previous=previous))
        previous = (bid, ask)
        if price is not None:
            for offset in (10, 20):
                trades.append(MarketTrade(price=D(price), size=D(trade_size), aggressor=aggressor,
                                          ts_ns=BASE_NS + second * SECOND + offset * MS,
                                          trade_id=f"T{second}-{offset}"))
    runner = NautilusSimulationRunner(instrument=INSTRUMENT, book_updates=tuple(books), trades=tuple(trades))
    if cash:
        runner.configure_paper_cash(SpotCashSettings(initial_quote=D("100000")))
    return runner


def protected(*, quantity="1", limit="2701.00", stop="2650.00", targets=(("2800.00", "100"),),
              valid_until=None, decided_at=None):
    return ProtectedDecision(
        action="buy", confidence="high", rationale="synthetic entry", quantity=D(quantity),
        order_type="limit", limit_price=D(limit), decided_at=decided_at,
        protection=ProtectionPlan(D(stop), tuple(TakeProfitLevel(D(p), D(a)) for p, a in targets)),
        valid_until=valid_until,
    )


def run(journal, decision, steps, *, prices=("2700.00",), size="100", limits=None, safety=None):
    decisions = fx.scripted(decision, *[fx.hold_decision() for _ in prices[1:]])
    simulator = tape(steps, size=size)
    session = fx.session(journal, decisions=decisions, data_feed=fx.feed(*prices),
                         simulator=simulator, limits_override=limits, safety=safety)
    return session.run(), session


def fills(report):
    return [(fill.quantity, fill.price) for fill in report.fills]


ENTRY = (0, "2700.00", "2701.00", "2700.00", "SELLER", "100")
QUIET = (1, "2700.00", "2701.00", "2700.00", "BUYER", "1")


def test_stop_exits_with_a_market_order_at_the_book_not_at_the_stop_price(journal):
    report, session = run(journal, protected(), [
        ENTRY, QUIET,
        (2, "2649.00", "2650.00", "2649.00", "SELLER", "1"),
        (3, "2649.00", "2650.00", "2649.00", "SELLER", "1"),
    ])
    assert fills(report) == [(D("1"), D("2701.00")), (D("1"), D("2649.00"))]
    assert report.positions == {"ETHUSDT": D("0E-8")} or report.positions["ETHUSDT"] == 0
    assert session.protections.snapshot() == []


def test_a_gap_through_the_stop_fills_at_the_worse_gap_price(journal):
    report, _ = run(journal, protected(), [
        ENTRY, QUIET,
        (2, "2500.00", "2501.00", "2500.00", "SELLER", "1"),
        (3, "2500.00", "2501.00", "2500.00", "SELLER", "1"),
    ])
    assert fills(report)[1] == (D("1"), D("2500.00"))


def test_take_profit_fills_at_its_level_and_a_gap_up_only_improves_it(journal):
    report, _ = run(journal, protected(), [
        ENTRY, QUIET,
        (2, "2800.00", "2801.00", "2800.00", "BUYER", "1"),
        (3, "2800.00", "2801.00", "2800.00", "BUYER", "5"),
    ])
    assert fills(report) == [(D("1"), D("2701.00")), (D("1"), D("2800.00"))]


def test_ladder_sells_each_allocation_once_and_the_last_level_takes_the_rest(journal):
    decision = protected(quantity="1", targets=(("2750.00", "33.3"), ("2800.00", "66.7")))
    report, session = run(journal, decision, [
        ENTRY, QUIET,
        (2, "2750.00", "2751.00", "2750.00", "BUYER", "1"),
        (3, "2750.00", "2751.00", "2750.00", "BUYER", "5"),
        (4, "2750.00", "2751.00", "2750.00", "BUYER", "5"),
        (5, "2800.00", "2801.00", "2800.00", "BUYER", "1"),
        (6, "2800.00", "2801.00", "2800.00", "BUYER", "5"),
        (7, "2800.00", "2801.00", "2800.00", "BUYER", "5"),
    ])
    assert [fill.quantity for fill in report.fills] == [D("1"), D("0.333"), D("0.667")]
    assert session.protections.snapshot() == []
    assert report.positions["ETHUSDT"] == 0


def test_stop_outranks_a_target_when_one_timestamp_reaches_both(journal):
    # The same tape timestamp prints above the target and below the stop.
    runner = tape([ENTRY, QUIET])
    late = BASE_NS + 2 * SECOND
    extra = (*runner.trades,
             MarketTrade(price=D("2810.00"), size=D("1"), aggressor="BUYER", ts_ns=late, trade_id="H"),
             MarketTrade(price=D("2640.00"), size=D("1"), aggressor="SELLER", ts_ns=late, trade_id="L"))
    follow = book(9, 3, "2640.00", "2641.00", previous=("2700.00", "2701.00"))
    runner.trades = (*extra, MarketTrade(price=D("2640.00"), size=D("1"), aggressor="SELLER",
                                         ts_ns=BASE_NS + 3 * SECOND + 10 * MS, trade_id="Z"),
                     MarketTrade(price=D("2640.00"), size=D("1"), aggressor="SELLER",
                                 ts_ns=BASE_NS + 3 * SECOND + 20 * MS, trade_id="Y"))
    runner.book_updates = (*runner.book_updates, *follow)
    report, = (fx.session(journal, decisions=fx.scripted(protected()), data_feed=fx.feed("2700.00"),
                          simulator=runner).run(),)
    assert fills(report)[1][1] == D("2640.00")
    intents = [e.payload.rationale for e in journal.read_range() if e.event_type == "trade.intent.created"]
    assert sum(text.startswith("protective:stop_loss") for text in intents) == 1
    assert not any(text.startswith("protective:take_profit") for text in intents)


def test_partial_immediate_entry_protects_only_what_filled(journal):
    report, _ = run(journal, protected(quantity="20", limit="2701.00"), [
        ENTRY, QUIET,
        (2, "2640.00", "2641.00", "2640.00", "SELLER", "1"),
        (3, "2640.00", "2641.00", "2640.00", "SELLER", "1"),
    ], size="10", limits=fx.limits(max_order_notional_quote="60000", max_symbol_exposure_quote="90000",
                                   max_gross_exposure_quote="200000", max_net_exposure_quote="90000",
                                   max_strategy_exposure_quote="90000", concentration_limit_pct="100"))
    assert report.fills[0].quantity == D("10")
    assert sum(fill.quantity for fill in report.fills[1:]) == D("10")
    assert report.positions["ETHUSDT"] == 0


def test_a_stop_waits_while_a_strategy_sell_is_in_flight_so_it_cannot_oversell(journal):
    decisions = fx.scripted(protected(), fx.sell_decision(quantity="1", limit_price="2705.00"))
    simulator = tape([ENTRY, (1, "2700.00", "2701.00", "2700.00", "BUYER", "1"),
                      (2, "2640.00", "2641.00", "2640.00", "BUYER", "1"),
                      (3, "2640.00", "2641.00", "2640.00", "BUYER", "1")])
    report = fx.session(journal, decisions=decisions, data_feed=fx.feed("2700.00", "2700.00"),
                        simulator=simulator).run()
    intents = [e for e in journal.read_range() if e.event_type == "trade.intent.created"]
    assert not any((e.payload.rationale or "").startswith("protective") for e in intents)
    assert report.positions["ETHUSDT"] == D("1")


def test_an_exit_never_rests_its_unfilled_part_ends_and_a_new_exit_follows(journal):
    runner = tape([ENTRY, QUIET,
                   *[(second, "2640.00", "2641.00", "2640.00", "SELLER", "1") for second in range(2, 7)]],
                  size="5")
    runner.book_updates = tuple(
        replace(update, levels=[replace(level, size=D("0.4")) for level in update.levels])
        if update.ts_ns >= BASE_NS + 2 * SECOND else update for update in runner.book_updates)
    report = fx.session(journal, decisions=fx.scripted(protected()), data_feed=fx.feed("2700.00"),
                        simulator=runner).run()
    exits = [e for e in journal.read_range() if e.event_type == "trade.intent.created"
             and e.payload.rationale.startswith("protective:stop_loss")]
    assert len(exits) >= 2
    assert all(order.state in {"filled", "canceled", "rejected", "expired"} for order in report.orders)


def test_a_stop_fills_at_most_the_position_and_can_never_go_short(journal):
    report, _ = run(journal, protected(), [
        ENTRY, QUIET,
        *[(second, "2640.00", "2641.00", "2640.00", "SELLER", "1") for second in range(2, 8)],
    ])
    assert report.positions["ETHUSDT"] == 0
    sold = sum(fill.quantity for fill in report.fills if fill.price == D("2640.00"))
    assert sold == D("1")


def test_a_recovered_price_does_not_cancel_a_triggered_stop(journal):
    report, _ = run(journal, protected(), [
        ENTRY, QUIET,
        (2, "2649.00", "2650.00", "2649.00", "SELLER", "1"),
        (3, "2690.00", "2691.00", "2690.00", "BUYER", "1"),
        (4, "2690.00", "2691.00", "2690.00", "BUYER", "1"),
    ])
    assert report.positions["ETHUSDT"] == 0


def test_a_host_entry_is_immediate_so_an_unfilled_part_ends_and_nothing_rests(journal):
    report, session = run(journal, protected(limit="2699.00"), [ENTRY, QUIET, QUIET[:0] or
                          (2, "2700.00", "2701.00", "2700.00", "BUYER", "1")])
    assert report.fills == ()
    assert [order.state for order in report.orders] in (["canceled"], ["expired"])
    assert session.protections.snapshot() == []


def test_a_decision_past_its_valid_until_stops_at_validity(journal):
    decision = protected(decided_at=fx.BASE + timedelta(seconds=5), valid_until=fx.BASE + timedelta(seconds=1))
    decisions = fx.scripted(decision, fx.hold_decision())
    simulator = tape([ENTRY, (5, "2700.00", "2701.00", "2700.00", "BUYER", "1"),
                      (6, "2700.00", "2701.00", "2700.00", "BUYER", "1"),
                      (7, "2700.00", "2701.00", "2700.00", "BUYER", "1")])
    data_feed = fx.feed("2700.00", start=fx.BASE)
    report = fx.session(journal, decisions=decisions, data_feed=data_feed, simulator=simulator).run()
    assert report.decisions[0].submission == "stopped_by_validity"
    assert report.fills == ()


def test_exits_go_through_hard_risk_and_are_audited_as_reduce_only(journal):
    run(journal, protected(), [
        ENTRY, QUIET,
        (2, "2640.00", "2641.00", "2640.00", "SELLER", "1"),
        (3, "2640.00", "2641.00", "2640.00", "SELLER", "1"),
    ])
    rows = [e for e in journal.read_range() if e.event_type == "risk.evaluation.recorded"]
    assert [row.payload.decision for row in rows] == ["allowed", "allowed"]
    decisions = [e for e in journal.read_range() if e.event_type == "risk.decision.recorded"]
    assert len(decisions) == 2


def test_a_realized_loss_limit_does_not_trap_the_position(journal):
    limits = fx.limits(loss_limit_quote="5")
    report, _ = run(journal, protected(quantity="1"), [
        ENTRY, QUIET,
        (2, "2600.00", "2601.00", "2600.00", "SELLER", "1"),
        (3, "2600.00", "2601.00", "2600.00", "SELLER", "1"),
    ], limits=limits)
    assert report.positions["ETHUSDT"] == 0


def test_kill_switch_refusal_is_audited_and_the_exit_stays_armed(journal):
    from crypto_trader.safety import SafetyController
    safety = SafetyController(correlation_id=fx.SESSION_ID)
    report, session = run(journal, protected(), [
        ENTRY, QUIET,
        (2, "2640.00", "2641.00", "2640.00", "SELLER", "1"),
        (3, "2640.00", "2641.00", "2640.00", "SELLER", "1"),
    ], limits=fx.limits(max_order_notional_quote="5000", max_gross_exposure_quote="3000"), safety=safety)
    assert report.positions["ETHUSDT"] == D("1")
    assert [row["event"] for row in session.protections.log if row["event"] == "exit_blocked"]
    assert session.protections.snapshot()[0]["blocked_attempts"] >= 1


def test_replays_of_a_growing_tape_keep_every_earlier_event_and_add_no_duplicates(tmp_path):
    steps = [ENTRY, QUIET,
             (2, "2640.00", "2641.00", "2640.00", "SELLER", "1"),
             (3, "2640.00", "2641.00", "2640.00", "SELLER", "1"),
             (4, "2640.00", "2641.00", "2640.00", "SELLER", "1")]
    with SQLiteEventJournal(tmp_path / "rolling.db") as rolling:
        seen = set()
        for length in range(1, len(steps) + 1):
            report, _ = run(rolling, protected(), steps[:length])
            ids = {event.event_id for event in rolling.read_range()}
            assert seen <= ids
            seen = ids
        rebuilt = report_from_journal(rolling.read_range(), session_id=fx.SESSION_ID, mode="paper",
                                      symbol="ETHUSDT", strategy_id="strategy-paper-1",
                                      strict_native_provenance=True)
    with SQLiteEventJournal(tmp_path / "once.db") as once:
        run(once, protected(), steps)
        assert {event.event_id for event in once.read_range()} == seen
    assert [fill.quantity for fill in rebuilt.fills] == [D("1"), D("1")]


def test_cash_ledger_matches_the_native_account_after_a_round_trip(journal):
    decisions = fx.scripted(protected())
    simulator = tape([
        ENTRY, QUIET,
        (2, "2640.00", "2641.00", "2640.00", "SELLER", "1"),
        (3, "2640.00", "2641.00", "2640.00", "SELLER", "1"),
    ])
    session = fx.session(journal, decisions=decisions, data_feed=fx.feed("2700.00"), simulator=simulator)
    report = session.run()
    native, ledger = simulator.cash_end_evidence(), session._r02_ledger
    assert native.quote_total == ledger.quote_total
    assert native.base_total == ledger.base_total == 0
    assert native.fees_quote == ledger.fees_quote() > 0
    assert report.equity_quote == ledger.quote_total


def test_a_buy_without_protection_keeps_the_legacy_behavior(journal):
    report, session = run(journal, fx.buy_decision(quantity="1"), [
        ENTRY, QUIET,
        (2, "2500.00", "2501.00", "2500.00", "SELLER", "1"),
        (3, "2500.00", "2501.00", "2500.00", "SELLER", "1"),
    ])
    assert report.positions["ETHUSDT"] == D("1")
    assert len(report.fills) == 1


def test_r02_refuses_a_protected_decision_type():
    from crypto_trader.paper.decisions import FrozenRunInputs
    from crypto_trader.paper.feed import FrozenMarketDataFeed as Feed
    seen = fx.feed("2700.00")
    with pytest.raises(PaperStateError, match="unproven scripted decision type"):
        FrozenRunInputs.capture(
            feed=Feed(entries=tuple(seen.observations())), decisions=fx.scripted(protected()),
            config=fx.config(), instrument=INSTRUMENT, book_updates=(), trades=(),
            cash=SpotCashSettings(initial_quote=D("100000")), limits=fx.limits(),
            validity_policy=None, safety_mode="normal", code_tree="0" * 40,
        )


@pytest.mark.parametrize("kwargs", [
    dict(stop="2701.00"), dict(stop="2750.00"),
])
def test_a_stop_at_or_above_the_entry_is_refused(kwargs):
    with pytest.raises(PaperStateError):
        protected(**kwargs)


def test_target_below_stop_and_unsorted_ladder_are_refused():
    with pytest.raises(PaperStateError):
        ProtectionPlan(D("100"), (TakeProfitLevel(D("90"), D("100")),))
    with pytest.raises(PaperStateError):
        ProtectionPlan(D("100"), (TakeProfitLevel(D("120"), D("50")), TakeProfitLevel(D("110"), D("50"))))


def test_book_arms_only_after_a_fill_and_only_for_later_trades():
    book_ = ProtectionBook(D("0.0001"))
    entry = uuid4()
    book_.register_entry(entry, ProtectionPlan(D("90")))
    assert book_.evaluate(5, D("80"), D("80"), position=D("1")) is None
    book_.on_fill(entry, "buy", D("1"), 10)
    assert book_.evaluate(10, D("80"), D("80"), position=D("1")) is None
    assert book_.evaluate(11, D("80"), D("80"), position=D("1")).leg == "stop_loss"


def test_book_protects_only_filled_quantity_of_a_partial_entry():
    book_ = ProtectionBook(D("0.0001"))
    entry = uuid4()
    book_.register_entry(entry, ProtectionPlan(D("90")))
    book_.on_fill(entry, "buy", D("0.6"), 10)
    assert book_.needs_every_tick()  # The remainder is still working.
    trigger = book_.evaluate(11, D("80"), D("80"), position=D("0.6"))
    assert trigger.quantity == D("0.6")
    book_.on_terminal(entry, 10)
    exit_id = uuid4()
    book_.exit_placed(trigger, exit_id)
    assert book_.evaluate(12, D("80"), D("80"), position=D("0.6")) is None  # One stop exit at a time.
    book_.on_fill(exit_id, "sell", D("0.2"), 13)
    book_.on_terminal(exit_id, 13)
    again = book_.evaluate(14, D("95"), D("95"), position=D("0.4"))
    assert (again.quantity, again.sequence) == (D("0.4"), 1)  # Sticky, sized to what is left.


def test_book_refuses_an_exit_fill_beyond_its_protected_quantity():
    book_ = ProtectionBook(D("0.0001"))
    entry, exit_id = uuid4(), uuid4()
    book_.register_entry(entry, ProtectionPlan(D("90")))
    book_.on_fill(entry, "buy", D("1"), 10)
    book_.exit_placed(book_.evaluate(11, D("80"), D("80"), position=D("1")), exit_id)
    with pytest.raises(PaperStateError, match="more than its protected"):
        book_.on_fill(exit_id, "sell", D("1.1"), 12)


def test_book_strategy_sell_reduces_oldest_protection_first():
    book_ = ProtectionBook(D("0.0001"))
    first, second = uuid4(), uuid4()
    for entry in (first, second):
        book_.register_entry(entry, ProtectionPlan(D("90")))
        book_.on_fill(entry, "buy", D("1"), 10)
        book_.on_terminal(entry, 10)
    book_.on_fill(uuid4(), "sell", D("1.5"), 20)
    assert [row["open"] for row in book_.snapshot()] == ["0.5"]


def test_scripted_resume_keeps_earlier_fills_in_the_accounting(journal):
    first = fx.session(journal, decisions=fx.scripted(fx.buy_decision(quantity="1")),
                       data_feed=fx.feed("2700.00"), simulator=ScriptedSimulationRunner())
    first.run()
    resumed = fx.session(journal, decisions=fx.scripted(fx.buy_decision(quantity="1"), fx.hold_decision()),
                         data_feed=fx.feed("2700.00", "2710.00"), simulator=ScriptedSimulationRunner())
    report = resumed.run(start_index=resumed.resume_index())
    assert report.positions["ETHUSDT"] == D("1")
    assert report.unrealized_pnl_quote == D("10.00")
