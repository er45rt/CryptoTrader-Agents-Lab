"""Independent AP2 P1 regressions. No network or venue access."""
import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import host_fixtures as hf
from crypto_trader.tests import test_crew_paper_bridge as b
from crypto_trader.tests import test_paper_protection as p
from crypto_trader.tests import paper_fixtures as fx
from crypto_trader.execution.nautilus import NautilusSimulationRunner
from crypto_trader.execution import ExecutionTimeInForce, MarketTrade
from crypto_trader.paper import ProtectionBook, ProtectionPlan, TakeProfitLevel
from crypto_trader.paper.config import SpotCashSettings
from crypto_trader.paper_host.host import PaperHost
from crypto_trader.paper_host.public_market import FeedUnavailable


def test_br1_subtick_target_is_refused_as_hold():
    raw = b.plan(take_profit_targets=[{"level": 0.001, "allocation_pct": 100}])
    result = b.to_decision(raw)
    assert result.action == "hold"
    assert "target_on_wrong_side" in result.rationale


def test_br1_targets_merged_by_the_tick_are_refused_as_hold():
    raw = b.plan(take_profit_targets=[{"level": 2800.001, "allocation_pct": 50},
                                      {"level": 2800.004, "allocation_pct": 50}])
    result = b.to_decision(raw)
    assert result.action == "hold"
    assert "target_on_wrong_side" in result.rationale


def test_br1_subtick_hold_is_sealed_and_provider_is_not_recalled(tmp_path):
    store = tmp_path / "decisions"
    store.mkdir()
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        plans = b.Plans(b.plan(take_profit_targets=[{"level": 0.001, "allocation_pct": 100}]))
        source = b.source_for(journal, store, plans)
        observed = b.observation()
        first = source.decide(observed)
        replayed = source.decide(observed)
        assert first.action == replayed.action == "hold"
        assert len(plans.contexts) == 1
        assert len(list(store.glob("crew_*.json"))) == 1


@pytest.fixture
def journal(tmp_path):
    with SQLiteEventJournal(tmp_path / 'journal.db') as instance:
        yield instance


def resting_session(journal, runner, *, limit="2700.00", quantity="1"):
    runner.configure_paper_cash(SpotCashSettings(initial_quote=p.D("100000")))
    decision = replace(p.protected(limit=limit, quantity=quantity), immediate=False)
    return fx.session(journal, decisions=fx.scripted(decision), data_feed=fx.feed("2700.00"), simulator=runner)


def market_trades(rows):
    return tuple(MarketTrade(price=p.D(price), size=p.D("100"), aggressor=side, ts_ns=p.BASE_NS + offset, trade_id=name)
                 for offset, price, side, name in rows)


def test_pr1_subsecond_stop_after_delayed_fill(journal):
    # The book move fills the resting entry; the stop prints and recovers inside one _STEP_NS.
    books = []
    books.extend(p.book(1, 0, "2700.00", "2701.00"))
    books.extend(p.book(2, 0, "2649.00", "2650.00", previous=("2700.00", "2701.00"), offset=200_000_000))
    books.extend(p.book(3, 0, "2700.00", "2701.00", previous=("2649.00", "2650.00"), offset=500_000_000))
    trades = market_trades((
        (10_000_000, "2701.00", "BUYER", "pre_fill_tick"),
        (100_000_000, "2700.00", "SELLER", "entry"),
        (210_000_000, "2649.00", "SELLER", "stop"),
        (510_000_000, "2701.00", "BUYER", "rebound"),
        (1_100_000_000, "2701.00", "BUYER", "horizon"),
    ))
    runner = NautilusSimulationRunner(instrument=p.INSTRUMENT, book_updates=tuple(books), trades=trades)
    report = resting_session(journal, runner).run()
    assert report.fills, "test must witness the entry fill"
    assert report.fills[0].price == p.D("2700.00"), "entry must fill after the initial non-filling tick"
    assert report.positions["ETHUSDT"] == 0, "a subsecond crossed stop must not stay silently open"


class CountingRunner(NautilusSimulationRunner):
    stops = 0

    def advance_until(self, timestamp_ns):
        type(self).stops += 1
        return super().advance_until(timestamp_ns)


def test_pr1_a_resting_entry_keeps_the_engine_stop_throttle(journal):
    # 3 s of 10 ms prints: stopping the engine on each one made a replay minutes long.
    CountingRunner.stops = 0
    trades = market_trades((10_000_000 * (index + 1), "2701.00", "BUYER", f"t{index}") for index in range(300))
    runner = CountingRunner(instrument=p.INSTRUMENT, book_updates=tuple(p.book(1, 0, "2700.00", "2701.00")),
                            trades=trades)
    resting_session(journal, runner, limit="2690.00").run()
    assert CountingRunner.stops <= 10


def test_pr1_book_counts_a_working_entry_only_for_a_print_through_its_stop():
    book = ProtectionBook(p.D("0.01"))
    entry = fx.SESSION_ID
    book.register_entry(entry, ProtectionPlan(p.D("2650"), (TakeProfitLevel(p.D("2800"), p.D("100")),)))
    assert book.crosses(p.D("2650"), p.D("2700"))
    assert not book.crosses(p.D("2651"), p.D("2800"))
    book.on_terminal(entry, 1)
    assert not book.crosses(p.D("2650"), p.D("2700")), "an ended unfilled entry has nothing to protect"


def test_pr1_a_later_partial_fill_does_not_disarm_earlier_exposure():
    book = ProtectionBook(p.D("0.01"))
    entry = fx.SESSION_ID
    book.register_entry(entry, ProtectionPlan(p.D("2650")))
    book.on_fill(entry, "buy", p.D("0.4"), 100)
    book.on_fill(entry, "buy", p.D("0.3"), 200)
    trigger = book.evaluate(200, p.D("2649"), p.D("2701"), position=p.D("0.7"))
    assert trigger is not None and trigger.leg == "stop_loss"


def test_pr1_a_stop_sharing_a_partial_fill_timestamp_still_exits(journal):
    books = []
    books.extend(p.book(1, 0, "2700.00", "2701.00"))
    books.extend(p.book(2, 1, "2699.00", "2699.50", size="0.4", previous=("2700.00", "2701.00")))
    books.extend(p.book(3, 1, "2700.00", "2701.00", previous=("2699.00", "2699.50"), offset=300_000_000))
    books.extend(p.book(4, 2, "2699.00", "2699.50", size="0.3", previous=("2700.00", "2701.00"), offset=100_000_000))
    books.extend(p.book(5, 2, "2700.00", "2701.00", previous=("2699.00", "2699.50"), offset=400_000_000))
    trades = market_trades((
        (10_000_000, "2701.00", "BUYER", "pre"),
        (1_100_000_000, "2701.00", "BUYER", "first_partial_seen"),
        (2_200_000_000, "2649.00", "SELLER", "stop"),
        (2_500_000_000, "2701.00", "BUYER", "rebound"),
        (4_000_000_000, "2701.00", "BUYER", "later"),
        (6_000_000_000, "2701.00", "BUYER", "horizon"),
    ))
    runner = NautilusSimulationRunner(instrument=p.INSTRUMENT, book_updates=tuple(books), trades=trades)
    session = resting_session(journal, runner)
    report = session.run()
    assert sum(fill.quantity for fill in report.fills) > p.D("1"), "test must witness an exit"
    assert report.positions["ETHUSDT"] == 0


def test_book_closes_a_target_share_that_rounds_to_nothing_and_the_last_level_takes_the_rest():
    book = ProtectionBook(p.D("0.01"))
    entry = fx.SESSION_ID
    levels = (TakeProfitLevel(p.D("2700"), p.D("33")), TakeProfitLevel(p.D("2800"), p.D("67")))
    book.register_entry(entry, ProtectionPlan(p.D("2600"), levels))
    book.on_fill(entry, "buy", p.D("0.01"), 1)
    assert book.evaluate(2, p.D("2690"), p.D("2701"), position=p.D("0.01")) is None
    assert book.snapshot()[0]["targets"][0]["closed"] is True
    last = book.evaluate(3, p.D("2790"), p.D("2801"), position=p.D("0.01"))
    assert (last.level, last.quantity) == (1, p.D("0.01"))


def test_host_entries_and_exits_are_all_sent_immediate(tmp_path, monkeypatch):
    original = NautilusSimulationRunner.submit_temporal
    sent = []

    def spy(self, request, **kwargs):
        sent.append(request)
        return original(self, request, **kwargs)

    monkeypatch.setattr(NautilusSimulationRunner, "submit_temporal", spy)
    directory = hf.new_session(tmp_path)
    market = hf.ScriptedMarket(hf.LIFECYCLE_PRICES)
    instance = PaperHost(directory, source=market, plan_provider=hf.Plans(*hf.LIFECYCLE_PLANS),
                         wall_clock_ns=market.wall_clock, sleep=lambda seconds: None)
    for _ in range(len(market.prices) + 2):
        instance.cycle()
    # A crash-only replay resubmits earlier orders, so count distinct intents.
    assert len({request.intent_id for request in sent}) == 4
    assert {request.time_in_force for request in sent} == {ExecutionTimeInForce.IOC}


def test_host_a_feed_outage_at_the_stamp_does_not_buy_a_second_kickoff(tmp_path):
    directory = hf.new_session(tmp_path)
    market = hf.ScriptedMarket(("2700",) * 12)
    plans = hf.Plans(hf.plan(**hf.NEUTRAL))
    failures = []

    def source(after):
        if failures:
            raise failures.pop(0)
        return market(after)

    def provider(inputs):
        failures.extend(FeedUnavailable("offline") for _ in range(5))
        return plans(inputs)

    instance = PaperHost(directory, source=source, plan_provider=provider,
                         wall_clock_ns=market.wall_clock, sleep=lambda seconds: None)
    assert instance.cycle() == {"feed": "down"}
    assert list((directory / "decisions").glob("crew_*.json")) == []
    assert instance.cycle()["feed"] == "up"
    assert len(plans.contexts) == 1
    assert len(list((directory / "decisions").glob("crew_*.json"))) == 1


_HC13_SNIPPET = """
import json, socket, sys
attempts = []
def blocked(*args, **kwargs):
    attempts.append(repr(args[1:3]))
    raise OSError('offline denied')
socket.socket.connect = socket.socket.connect_ex = blocked
socket.getaddrinfo = socket.create_connection = blocked
import crypto_trader.paper_host.host
loaded = sorted({name.split('.')[0] for name in sys.modules} & {'crewai', 'litellm', 'opentelemetry'})
print('HC13=' + json.dumps({'attempts': attempts, 'loaded': loaded}))
"""


def test_hc13_stub_host_import_stays_offline_with_the_default_environment(tmp_path):
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("CREWAI_", "OTEL_"))}
    env.update(USERPROFILE=str(tmp_path), HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", _HC13_SNIPPET], capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    line = next(row for row in result.stdout.splitlines() if row.startswith("HC13="))
    assert json.loads(line[5:]) == {"attempts": [], "loaded": []}


def test_hc13_factory_names_resolve_on_explicit_use():
    import crypto_trader.llm as llm
    from crypto_trader.llm import factory
    assert llm.get_llm is factory.get_llm
    with pytest.raises(AttributeError):
        llm.__getattr__("not_a_factory_name")


def test_hc13_a_real_crewai_task_still_accepts_the_guardrail():
    from crewai import Task
    from crypto_trader.llm.schemas import trading_plan_guardrail
    assert Task(description="d", expected_output="e", guardrail=trading_plan_guardrail) is not None
