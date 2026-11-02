"""Crew advisory -> Paper bridge: every stage offline against the real session and simulator.

No provider, venue or order endpoint is reachable: sockets are refused and the strategist LLM
is a deterministic stub. A stub proves the wiring, never the quality of any model's decision.
"""

import ast
import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from types import SimpleNamespace
import crypto_trader.paper.session as paper_session_module
from crypto_trader.paper import PaperStateError, PositionLedger
from crypto_trader.paper.accounting import require_unambiguous_fill_order
import pytest
from crewai.llms.base_llm import BaseLLM

from crypto_trader import crew as crew_module
from crypto_trader import crew_paper_bridge as bridge
from crypto_trader.crew_paper_bridge import (
    CrewPaperBridgeConfig,
    CrewPaperDecisionSource,
    PortfolioSnapshot,
    plan_to_decision,
    portfolio_snapshot,
)
from crypto_trader.execution import BookLevel, InstrumentSpec, MarketBookUpdate, MarketTrade
from crypto_trader.execution.nautilus import NautilusSimulationRunner
from crypto_trader.paper import PaperStateError, TradingMode
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.safety import SafetyController, SafetyMode
from crypto_trader.tests import paper_fixtures as fx

INSTRUMENT = InstrumentSpec(symbol="ETHUSDT")
MS = 1_000_000
BASE_NS = int(fx.BASE.timestamp() * 1_000_000_000)
ONE = timedelta(seconds=1)


def plan(**overrides) -> str:
    body = {
        "symbol": "ETHUSDT", "bias": "bullish", "confidence": "high",
        "human_readable_plan": "offline plan", "position_size_pct": 2,
        "entry_low": 2690, "entry_high": 2730, "stop_loss": 2650,
        "take_profit_targets": [{"level": 2800, "allocation_pct": 100}],
        "max_loss_pct": 1, "risk_reward_ratio": 2, "plan_validity": "1h",
        "invalidation_conditions": ["trend breaks"],
    }
    body.update(overrides)
    return "Plan\n```json\n" + json.dumps(body) + "\n```"


BEARISH = dict(bias="bearish", entry_low=2670, entry_high=2730, stop_loss=2760)
NEUTRAL = dict(bias="neutral", confidence="medium")


def bridge_config(**overrides) -> CrewPaperBridgeConfig:
    values = dict(
        session_id=fx.SESSION_ID, symbol="ETHUSDT", starting_equity_quote=Decimal("100000"),
        max_notional_quote=Decimal("3000"), tick_size=Decimal("0.01"),
        quantity_step=Decimal("0.0001"),
    )
    values.update(overrides)
    return CrewPaperBridgeConfig(**values)


FLAT = PortfolioSnapshot(
    position_quantity=Decimal("0"), average_cost=Decimal("0"), realized_pnl_quote=Decimal("0"),
    equity_quote=Decimal("100000"), open_sell_quantity=Decimal("0"),
)


def observation(price="2700.00", index=0):
    return fx.feed(*["2700.00"] * index, price).observations()[index]


def to_decision(raw, *, price="2700.00", portfolio=FLAT, **config):
    seen = observation(price)
    return plan_to_decision(raw, observation=seen, portfolio=portfolio,
                            config=bridge_config(**config), decided_at=seen.observed_at)


class Plans:
    """Host-injected plan source that records each portfolio context it was shown."""

    def __init__(self, *plans):
        self.plans = list(plans)
        self.contexts = []

    def __call__(self, inputs):
        self.contexts.append(inputs["portfolio_context"])
        reply = self.plans.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def tape(*steps):
    """One book plus trigger/clear/fill trades per (second, bid, ask, aggressor) step."""
    books, trades = [], []
    for sequence, (second, bid, ask, aggressor) in enumerate(steps, start=1):
        start = BASE_NS + second * 1_000_000_000
        books.append(MarketBookUpdate(
            levels=[
                BookLevel(side="BUY", price=Decimal(bid), size=Decimal("100"), order_id=2 * sequence - 1),
                BookLevel(side="SELL", price=Decimal(ask), size=Decimal("100"), order_id=2 * sequence),
            ],
            ts_ns=start, sequence=sequence,
        ))
        price = Decimal(bid if aggressor == "SELLER" else ask)
        for offset, size in ((10, "100"), (20, "100"), (30, "5")):
            trades.append(MarketTrade(price=price, size=Decimal(size), aggressor=aggressor,
                                      ts_ns=start + offset * MS, trade_id=f"T{second}-{offset}"))
    return NautilusSimulationRunner(instrument=INSTRUMENT, book_updates=tuple(books), trades=tuple(trades))


ROUND_TRIP_PRICES = ("2700.00", "2705.00", "2720.00", "2720.00")
ROUND_TRIP_TAPE = ((0, "2700.00", "2701.00", "SELLER"), (2, "2719.00", "2720.00", "BUYER"),
                   (3, "2720.00", "2721.00", "SELLER"))


def round_trip_plans():
    return (plan(), plan(**NEUTRAL), plan(**BEARISH), plan())


def source_for(journal, store, plans, **config):
    return CrewPaperDecisionSource(
        config=bridge_config(**config), journal=journal, store_dir=store,
        clock=lambda seen: seen.observed_at, plan_provider=plans,
    )


def run_session(journal, store, plans, *, prices=ROUND_TRIP_PRICES, steps=ROUND_TRIP_TAPE,
                limits=None, safety=None, **config):
    source = source_for(journal, store, plans, **config)
    session = fx.session(journal, decisions=source, data_feed=fx.feed(*prices), simulator=tape(*steps),
                         limits_override=limits, safety=safety)
    return session.run(), source


def events_of(journal, event_type):
    return [event for event in journal.read_range() if event.event_type == event_type]


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as instance:
        yield instance


@pytest.fixture
def store(tmp_path: Path):
    path = tmp_path / "decisions"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args[:2])
        raise OSError("network refused by offline test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield attempts
    assert attempts == []


# --- structured plan -> StrategyDecision ---


def test_bullish_plan_becomes_a_sized_limit_buy_on_the_observed_price():
    decision = to_decision(plan(), price="2700.005")
    assert (decision.action, decision.order_type, decision.confidence) == ("buy", "limit", "high")
    assert decision.limit_price == Decimal("2700.00")  # BUY never rounds upward
    assert decision.quantity == Decimal("0.7407")  # 2% of 100000 at 2700, floored to the step
    assert "sha256:" in decision.rationale


@pytest.mark.parametrize("raw, reason", [
    ("free text with no machine block", "plan_unstructured"),
    (plan() + "\n" + plan(), "plan_unstructured"),
    (plan(position_size_pct=101), "plan_invalid"),
    (plan(**NEUTRAL), "plan_neutral"),
    (plan(confidence="low"), "plan_low_confidence"),
    (plan(symbol="BTCUSDT"), "plan_wrong_symbol"),
    (plan(entry_low=2750, entry_high=2760, stop_loss=2700), "price_outside_entry_zone"),
    (plan(stop_loss=2800), "stop_on_wrong_side"),
    (plan(position_size_pct=0.0000001), "quantity_below_step"),
])
def test_unusable_plans_fail_closed_to_a_recorded_hold(raw, reason):
    decision = to_decision(raw)
    assert decision.action == "hold"
    assert reason in decision.rationale


def test_model_size_cannot_exceed_the_host_budget():
    assert to_decision(plan(position_size_pct=100)).quantity == Decimal("1.1111")  # 3000 / 2700


def test_bearish_plan_only_reduces_a_long_and_never_opens_a_short():
    assert "no_long_position" in to_decision(plan(**BEARISH)).rationale
    held = replace(FLAT, position_quantity=Decimal("0.5"))
    decision = to_decision(plan(**BEARISH), portfolio=held)
    assert (decision.action, decision.quantity) == ("sell", Decimal("0.5"))
    pending = replace(held, open_sell_quantity=Decimal("0.5"))
    assert "no_long_position" in to_decision(plan(**BEARISH), portfolio=pending).rationale


def test_buy_carries_host_rounded_protection_and_an_expiry():
    decision = to_decision(plan(plan_validity="30m", take_profit_targets=[
        {"level": 2800.004, "allocation_pct": 40}, {"level": 2850, "allocation_pct": 60},
    ], stop_loss=2649.996))
    assert decision.protection.stop_price == Decimal("2650.00")  # Rounded toward the entry.
    assert [level.price for level in decision.protection.take_profits] == [Decimal("2800.00"), Decimal("2850.00")]
    assert decision.valid_until - fx.BASE == timedelta(minutes=30)


@pytest.mark.parametrize("text, expected", [
    ("15m", timedelta(minutes=15)), ("2 hours", timedelta(hours=1)), ("until the trend breaks", timedelta(hours=1)),
])
def test_plan_validity_can_only_shorten_the_host_cap(text, expected):
    assert bridge.plan_ttl(text, timedelta(hours=1)) == expected


def test_a_plan_finished_after_its_expiry_is_a_hold():
    seen = observation()
    decision = plan_to_decision(plan(plan_validity="5m"), observation=seen, portfolio=FLAT,
                                config=bridge_config(), decided_at=seen.observed_at + timedelta(minutes=6))
    assert "plan_expired" in decision.rationale


def test_buy_is_capped_by_free_cash_and_by_the_plan_stop_distance():
    poor = replace(FLAT, quote_free=Decimal("270.00"))
    assert to_decision(plan(), portfolio=poor).quantity == Decimal("0.0999")  # Fee reserve kept.
    capped = to_decision(plan(position_size_pct=100, max_loss_pct=0.1))
    assert capped.quantity == Decimal("1.1111")  # 3000 budget; the 100 loss cap allows 2.
    tight = to_decision(plan(position_size_pct=100, max_loss_pct=0.02))
    assert tight.quantity == Decimal("0.4")  # 20 quote loss over a 50 stop distance.


def test_a_target_at_or_below_the_entry_is_a_hold():
    decision = to_decision(plan(entry_low=2690, entry_high=2720, take_profit_targets=[
        {"level": 2700, "allocation_pct": 100}]))
    assert decision.action == "hold"


def test_a_provider_failure_is_a_recorded_hold_and_never_a_trade(journal, store):
    source = source_for(journal, store, Plans(RuntimeError("provider down")))
    decision = source.decide(fx.feed("2700.00").observations()[0])
    assert decision.action == "hold" and "crew_unavailable:RuntimeError" in decision.rationale


# --- full lifecycle ---


def test_decision_reaches_risk_simulated_fill_portfolio_and_shapes_the_next_decision(journal, store):
    plans = Plans(*round_trip_plans())
    report, _ = run_session(journal, store, plans)

    assert [record.submission for record in report.decisions] == [
        "submitted_to_simulator", "hold", "submitted_to_simulator", "submitted_to_simulator",
    ]
    assert [record.action for record in report.decisions] == ["buy", "hold", "sell", "buy"]
    assert report.risk_counts == {"allowed": 3}
    # 0.7354 is 2% of the post-profit equity; a stale 100000 equity would size 0.7352.
    assert [fill.quantity for fill in report.fills] == [Decimal("0.7407"), Decimal("0.7407"), Decimal("0.7354")]

    # Later decisions were shown, and sized from, the post-fill journal state.
    assert plans.contexts[0].startswith("position_quantity=0;")
    assert "position_quantity=0.7407" in plans.contexts[1]
    assert "realized_pnl_quote=14.814" in plans.contexts[3]
    assert report.decisions[3].intent_id is not None
    assert report.positions == {"ETHUSDT": Decimal("0.7354")}
    assert report.realized_pnl_quote == Decimal("14.8140")


def test_every_fill_descends_from_an_allowed_risk_decision(journal, store):
    run_session(journal, store, Plans(*round_trip_plans()))
    approved = {event.payload.intent_id for event in events_of(journal, "risk.evaluation.recorded")
                if event.payload.decision == "allowed"}
    intents = {event.event_id for event in events_of(journal, "trade.intent.created")}
    fills = events_of(journal, "execution.fill.recorded")
    assert len(fills) == 3 and len(approved) == 3
    assert {event.causation_id for event in fills} <= intents
    assert all(event.producer != "crew" for event in fills)


def test_portfolio_rebuilt_from_journal_matches_the_session_ledger(journal, store):
    report, _ = run_session(journal, store, Plans(*round_trip_plans()))
    rebuilt = portfolio_snapshot(
        journal, config=bridge_config(), mark=Decimal("2720.00"), as_of=fx.BASE + timedelta(hours=1),
    )
    assert rebuilt.position_quantity == report.positions["ETHUSDT"]
    assert rebuilt.realized_pnl_quote == report.realized_pnl_quote
    assert rebuilt.equity_quote == report.equity_quote


def test_oversized_plan_cannot_pass_the_hard_risk_engine(journal, store):
    plans = Plans(plan(position_size_pct=10))
    report, _ = run_session(journal, store, plans, prices=("2700.00",), steps=ROUND_TRIP_TAPE[:1],
                            limits=fx.limits(max_order_notional_quote="1000"))
    assert report.decisions[0].submission == "stopped_by_risk"
    assert report.fills == ()
    assert events_of(journal, "order.state.changed") == []


def test_loss_from_a_simulated_fill_stops_the_next_buy_at_risk(journal, store):
    prices = ("2700.00", "2690.00", "2690.00")
    steps = ((0, "2700.00", "2701.00", "SELLER"), (1, "2689.00", "2690.00", "BUYER"))
    plans = Plans(plan(), plan(**BEARISH), plan())
    report, _ = run_session(journal, store, plans, prices=prices, steps=steps,
                            limits=fx.limits(loss_limit_quote="5"))
    assert [record.submission for record in report.decisions] == [
        "submitted_to_simulator", "submitted_to_simulator", "stopped_by_risk",
    ]
    assert "realized_pnl_quote=-7.407" in plans.contexts[2]
    assert report.risk_counts == {"allowed": 2, "rejected": 1}


def test_safety_halt_blocks_an_ai_decision_before_the_simulator(journal, store):
    safety = SafetyController(correlation_id=fx.SESSION_ID)
    safety.set_mode(SafetyMode.HALT_NEW_RISK, reason_code="test", reason="test", trigger="operator")
    report, _ = run_session(journal, store, Plans(plan()), prices=("2700.00",),
                            steps=ROUND_TRIP_TAPE[:1], safety=safety)
    assert report.decisions[0].submission == "blocked_by_safety"
    assert report.fills == ()


# --- continuity ---


def test_replayed_observation_reuses_the_durable_decision_without_a_new_kickoff(journal, store):
    plans = Plans(plan())
    seen = fx.feed("2700.00").observations()[0]
    first = source_for(journal, store, plans).decide(seen)
    restarted = source_for(journal, store, Plans())  # An empty source fails on any new kickoff.
    assert restarted.decide(seen) == first


def test_tampered_or_mismatched_decision_records_are_refused(journal, store):
    seen = fx.feed("2700.00").observations()[0]
    source_for(journal, store, Plans(plan())).decide(seen)
    record = next(store.iterdir())
    record.write_text(record.read_text(encoding="utf-8").replace("0.7407", "9.9999"), encoding="utf-8")
    with pytest.raises(PaperStateError, match="decision record"):
        source_for(journal, store, Plans()).decide(seen)
    record.unlink()
    source_for(journal, store, Plans(plan())).decide(seen)
    moved = replace(seen, price=Decimal("2701.00"))
    with pytest.raises(PaperStateError, match="decision record"):
        source_for(journal, store, Plans()).decide(moved)


def test_restart_replays_the_session_without_duplicate_decisions_or_events(tmp_path, journal, store):
    plans = Plans(plan(), plan(**NEUTRAL), KeyboardInterrupt())
    source = source_for(journal, store, plans)
    session = fx.session(journal, decisions=source, data_feed=fx.feed(*ROUND_TRIP_PRICES),
                         simulator=tape(*ROUND_TRIP_TAPE))
    with pytest.raises(KeyboardInterrupt):
        session.run()
    assert len(plans.contexts) == 3
    partial = {event.event_id for event in journal.read_range()}
    assert partial

    remaining = Plans(*round_trip_plans()[2:])
    report, _ = run_session(journal, store, remaining)
    assert len(remaining.contexts) == 2  # Observations 0 and 1 never reach the model again.
    assert "position_quantity=0.7407" in remaining.contexts[0]

    control_store = tmp_path / "control"
    control_store.mkdir()
    with SQLiteEventJournal(tmp_path / "control.db") as control:
        expected, _ = run_session(control, control_store, Plans(*round_trip_plans()))
        assert {e.event_id for e in journal.read_range()} == {e.event_id for e in control.read_range()}
    assert journal.count() == len({e.event_id for e in journal.read_range()})
    assert report.semantic_hash() == expected.semantic_hash()


_CRASH_CHILD = """
import json, os, sys
from pathlib import Path
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import test_crew_paper_bridge as t
root = Path(sys.argv[1])
replies = iter(t.round_trip_plans())
def provider(inputs):
    with (root / "calls.log").open("a", encoding="utf-8") as stream:
        stream.write(inputs["market_reference"] + "\\n")
    if inputs["market_reference"].endswith("#2"):
        os._exit(9)
    return next(replies)
with SQLiteEventJournal(root / "journal.db") as journal:
    t.run_session(journal, root / "decisions", provider)
"""


def test_killed_process_resumes_in_a_new_process_without_duplicating_a_decision(tmp_path):
    (tmp_path / "decisions").mkdir()
    child = subprocess.run(
        [sys.executable, "-s", "-c", _CRASH_CHILD, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)},
        capture_output=True, timeout=120,
    )
    assert child.returncode == 9, child.stderr[-2000:]
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").split()
    assert calls == [f"public.kline.1m#{index}" for index in range(3)]

    resumed = Plans(*round_trip_plans()[2:])
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        report, _ = run_session(journal, tmp_path / "decisions", resumed)
        decisions = events_of(journal, "strategy.decision.recorded")
    assert len(resumed.contexts) == 2
    assert len(decisions) == 4 and len({event.event_id for event in decisions}) == 4
    assert [record.action for record in report.decisions] == ["buy", "hold", "sell", "buy"]


def test_resume_from_the_middle_stays_refused_by_the_temporal_session(journal, store):
    run_session(journal, store, Plans(plan()), prices=("2700.00",), steps=ROUND_TRIP_TAPE[:1])
    session = fx.session(journal, decisions=source_for(journal, store, Plans()),
                         data_feed=fx.feed(*ROUND_TRIP_PRICES), simulator=tape(*ROUND_TRIP_TAPE))
    assert session.resume_index() == 1
    with pytest.raises(PaperStateError, match="temporal execution resume"):
        session.run(start_index=1)


# --- real CrewAI wiring and boundaries ---


class StateReadingLLM(BaseLLM):
    """Stub strategist that decides from the portfolio line in its actual prompt."""

    def __init__(self, prompts):
        super().__init__(model="offline-stub", stop=[])
        self.prompts = prompts

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None):
        text = messages if isinstance(messages, str) else "\n".join(item["content"] for item in messages)
        if "MACHINE-READABLE SUMMARY" not in text:
            return "Thought: done\nFinal Answer: offline context"
        self.prompts.append(text)
        reply = plan() if "position_quantity=0;" in text else plan(**BEARISH)
        return "Thought: done\nFinal Answer: " + reply


def test_real_crew_kickoff_drives_the_session_and_reads_portfolio_state(journal, store, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # The strategist task writes its plan file relative to the cwd.
    prompts = []
    monkeypatch.setattr(crew_module, "get_llm", lambda profile: StateReadingLLM(prompts))
    prices = ("2700.00", "2720.00", "2720.00")
    steps = ((0, "2700.00", "2701.00", "SELLER"), (1, "2719.00", "2720.00", "BUYER"),
             (2, "2720.00", "2721.00", "SELLER"))
    report, _ = run_session(journal, store, bridge.crew_plan_provider, prices=prices, steps=steps)
    assert len(prompts) == 3
    assert [record.action for record in report.decisions] == ["buy", "sell", "buy"]
    assert all("CURRENT SIMULATED PORTFOLIO" in prompt for prompt in prompts)
    assert "{portfolio_context}" not in "".join(prompts)
    assert len(report.fills) == 3


def test_shadow_mode_decides_and_risks_without_any_simulated_order(journal, store):
    source = source_for(journal, store, Plans(plan()))
    report = fx.session(journal, mode=TradingMode.SHADOW, decisions=source,
                        data_feed=fx.feed("2700.00")).run()
    assert report.decisions[0].submission == "would_submit"
    assert events_of(journal, "order.state.changed") == []


def test_bridge_module_has_no_venue_or_gateway_dependency():
    tree = ast.parse(Path(bridge.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden = ("binance", "mainnet", "production", "breakglass", "crew_decision_gateway",
                 "requests", "httpx", "execution")
    assert not [name for name in imported if any(token in name for token in forbidden)]



class _RetimedJournal:
    """Read-only view that moves one persisted fill onto another fill's timestamp."""

    def __init__(self, journal, moved, to):
        self._journal, self._moved, self._to = journal, moved, to

    def read_by_correlation(self, correlation_id):
        return [event.model_copy(update={"occurred_at": self._to}) if event.event_id == self._moved
                else event for event in self._journal.read_by_correlation(correlation_id)]


def test_equal_timestamp_buy_and_sell_fills_refuse_ambiguous_accounting(journal, store):
    run_session(journal, store, Plans(*round_trip_plans()))
    sides = {e.event_id: e.payload.side for e in events_of(journal, "trade.intent.created")}
    fills = events_of(journal, "execution.fill.recorded")
    buy = next(f for f in fills if sides[f.causation_id] == "buy")
    sell = next(f for f in fills if sides[f.causation_id] == "sell")
    view = _RetimedJournal(journal, sell.event_id, buy.occurred_at)
    with pytest.raises(PaperStateError, match="equal-timestamp buy and sell"):
        portfolio_snapshot(view, config=bridge_config(), mark=Decimal("2720.00"),
                           as_of=fx.BASE + timedelta(hours=1))
    session_cls = next(c for c in vars(paper_session_module).values()
                       if isinstance(c, type) and "_recover_prior_fills" in vars(c))
    stub = SimpleNamespace(journal=view, config=SimpleNamespace(
        session_id=bridge_config().session_id, symbol="ETHUSDT"))
    with pytest.raises(PaperStateError, match="equal-timestamp buy and sell"):
        session_cls._recover_prior_fills(stub, PositionLedger(Decimal("100000")))


def test_equal_timestamp_same_side_fills_still_replay():
    stamp = fx.BASE
    require_unambiguous_fill_order([(stamp, "sell"), (stamp, "sell"), (stamp + timedelta(seconds=1), "buy")])
    with pytest.raises(PaperStateError):
        require_unambiguous_fill_order([(stamp, "sell"), (stamp, "buy")])
