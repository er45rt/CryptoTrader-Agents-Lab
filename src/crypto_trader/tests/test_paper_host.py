"""Paper host: lifecycle, feedback, restart under real process death, feed faults. Offline.

The plans here are local fixtures, so every result proves wiring and recovery only. Nothing in
this file says anything about the quality of a model or a strategy.
"""
import ast
import json
import os
import socket
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import crypto_trader.paper_host.host as host_module
from crypto_trader.paper import PaperStateError
from crypto_trader.paper_host import public_market
from crypto_trader.paper_host.host import PaperHost, SessionLock, load_manifest, read_status
from crypto_trader.paper_host.public_market import BinancePublicReader, FeedUnavailable, MarketBatch
from crypto_trader.paper_host.tape import MarketTape, PublicBook, PublicTrade
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import host_fixtures as hf

D = Decimal


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("network refused by offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)


def host(directory, market, plans, **kwargs):
    return PaperHost(directory, source=market, plan_provider=plans, wall_clock_ns=market.wall_clock,
                     sleep=lambda seconds: None, **kwargs)


def run_all(directory, market, plans, **kwargs):
    instance = host(directory, market, plans, **kwargs)
    for _ in range(len(market.prices) + 2):
        instance.cycle()
    return instance


def journal_rows(directory, kind):
    with SQLiteEventJournal(Path(directory) / "journal.db") as journal:
        return [event for event in journal.read_range() if event.event_type == kind]


def rationale_kinds(directory):
    return [event.payload.rationale.split(":")[1] if event.payload.rationale.startswith("protective")
            else "entry" for event in journal_rows(directory, "trade.intent.created")]


def lifecycle(tmp_path):
    directory = hf.new_session(tmp_path)
    plans = hf.Plans(*hf.LIFECYCLE_PLANS)
    market = hf.ScriptedMarket(hf.LIFECYCLE_PRICES)
    return run_all(directory, market, plans), directory, plans


def test_full_cycle_buy_stop_feedback_reentry_target_flat(tmp_path):
    instance, directory, plans = lifecycle(tmp_path)
    assert rationale_kinds(directory) == ["entry", "stop_loss", "entry", "take_profit"]
    fills = journal_rows(directory, "execution.fill.recorded")
    assert len(fills) == 4
    assert fills[1].payload.price < D("2650")  # The stop fills at the book, below its level.
    status = read_status(directory)["recovered"]
    assert D(status["position_quantity"]) == 0 and status["open_orders"] == []
    assert status["stop_exits"] == 1 and status["target_exits"] == 1

    # The realized stop-out reached the next prompt, and so did the smaller equity.
    assert "stop_exits=1" in plans.contexts[2] and "realized_pnl_quote=-" in plans.contexts[2]
    assert "stop_exits=0" in plans.contexts[1]
    assert fills[2].payload.quantity < fills[0].payload.quantity

    state = json.loads((directory / "state.json").read_bytes())
    assert state["provider_kind"] == "stub"
    assert D(state["quote_total"]) == D(status["quote_free"])
    assert D(state["fees_quote"]) > 0


def test_every_fill_descends_from_an_allowed_risk_decision(tmp_path):
    _, directory, _ = lifecycle(tmp_path)
    allowed = {event.payload.intent_id for event in journal_rows(directory, "risk.evaluation.recorded")
               if event.payload.decision == "allowed"}
    intents = {event.event_id for event in journal_rows(directory, "trade.intent.created")}
    fills = journal_rows(directory, "execution.fill.recorded")
    assert len(allowed) == len(intents) == 4
    assert {event.causation_id for event in fills} <= intents


def test_a_provider_failure_is_a_recorded_hold_and_the_session_keeps_running(tmp_path):
    directory = hf.new_session(tmp_path)
    plans = hf.Plans(RuntimeError("provider down"), hf.plan(**hf.NEUTRAL))
    run_all(directory, hf.ScriptedMarket(("2700",) * 12), plans)
    assert journal_rows(directory, "trade.intent.created") == []
    decisions = journal_rows(directory, "strategy.decision.recorded")
    assert "crew_unavailable:RuntimeError" in decisions[0].payload.rationale


def test_decision_records_carry_provenance_and_stub_label(tmp_path):
    _, directory, _ = lifecycle(tmp_path)
    records = [json.loads(path.read_bytes()) for path in (directory / "decisions").iterdir()]
    assert {record["provenance"]["provider_kind"] for record in records} == {"stub"}
    protected = [record for record in records if record["decision"].get("protection")]
    assert protected[0]["decision"]["valid_until"] > protected[0]["decision"]["decided_at"]


# --- restart under real process death ---

_CHILD = """
import json, os, sys
from pathlib import Path
from crypto_trader.paper_host.host import PaperHost
from crypto_trader.tests import host_fixtures as hf
directory, point, count = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
seen = {"n": 0}
def crash(name):
    if name == point:
        seen["n"] += 1
        if seen["n"] == count:
            os._exit(9)
class Dying:
    def __init__(self, journal):
        self.__dict__["_inner"] = journal
    def __getattr__(self, name):
        return getattr(self._inner, name)
    def append(self, event):
        written = self._inner.append(event)
        if point == "mid_journal" and written:
            seen["n"] += 1
            if seen["n"] == count:
                os._exit(9)
        return written
market = hf.ScriptedMarket(hf.LIFECYCLE_PRICES)
instance = PaperHost(directory, source=market, plan_provider=hf.Plans(*hf.LIFECYCLE_PLANS, log=directory / "calls.log"),
                     wall_clock_ns=market.wall_clock, sleep=lambda seconds: None, crash=crash,
                     journal_wrapper=Dying)
for _ in range(len(market.prices) + 2):
    instance.cycle()
"""


def killed_then_resumed(tmp_path, point, count):
    directory = hf.new_session(tmp_path / f"{point}{count}".replace("_", ""))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)}
    child = subprocess.run([sys.executable, "-s", "-c", _CHILD, str(directory), point, str(count)],
                           env=env, capture_output=True, timeout=180)
    assert child.returncode == 9, child.stderr[-3000:]
    log = directory / "calls.log"
    run_all(directory, hf.ScriptedMarket(hf.LIFECYCLE_PRICES), hf.Plans(*hf.LIFECYCLE_PLANS, log=log))
    return directory


def assert_no_double_effect(directory):
    fills = journal_rows(directory, "execution.fill.recorded")
    assert len({event.event_id for event in fills}) == len(fills)
    status = read_status(directory)["recovered"]
    assert D(status["position_quantity"]) == 0
    decisions = journal_rows(directory, "strategy.decision.recorded")
    assert len({event.event_id for event in decisions}) == len(decisions)
    references = [path.name for path in (directory / "decisions").iterdir()]
    assert len(references) == len(set(references)) >= 4
    assert rationale_kinds(directory).count("stop_loss") == 1
    assert rationale_kinds(directory).count("take_profit") == 1


@pytest.mark.parametrize("point, count", [
    ("before_tape_commit", 3), ("after_tape_commit", 7), ("after_observation", 2),
    ("before_decision", 2), ("after_decision", 1), ("after_decision", 3), ("after_replay", 6),
    ("before_state", 9), ("mid_journal", 3), ("mid_journal", 12), ("mid_journal", 25),
])
def test_process_killed_at_each_stage_recovers_without_a_double_effect(tmp_path, point, count):
    assert_no_double_effect(killed_then_resumed(tmp_path, point, count))


def test_kill_after_a_sealed_decision_never_calls_the_model_again(tmp_path):
    directory = killed_then_resumed(tmp_path, "after_decision", 2)
    calls = (directory / "calls.log").read_text(encoding="utf-8").split()
    assert len(calls) == len(set(calls))


@pytest.mark.parametrize("point, count", [("after_decision", 3), ("after_replay", 6), ("mid_journal", 12)])
def test_kill_after_durable_inputs_converges_on_the_uninterrupted_fills(tmp_path, point, count):
    _, control, _ = lifecycle(tmp_path)
    directory = killed_then_resumed(tmp_path, point, count)
    def facts(path):
        return [(e.event_id, e.payload.quantity, e.payload.price)
                for e in journal_rows(path, "execution.fill.recorded")]
    assert facts(directory) == facts(control)


def test_resume_restores_an_open_position_and_its_armed_stop(tmp_path):
    directory = hf.new_session(tmp_path)
    prices = ("2700",) * 4
    market = hf.ScriptedMarket(prices)
    run_all(directory, market, hf.Plans(hf.plan()))
    held = read_status(directory)["recovered"]
    assert D(held["position_quantity"]) > 0

    # A new process over more market: the stop armed before the restart still fires, exactly once.
    extended = hf.ScriptedMarket(prices + ("2640",) * 5)
    resumed = hf.Plans(hf.plan(), *[hf.plan(**hf.NEUTRAL)] * 5)
    run_all(directory, extended, resumed)
    status = read_status(directory)["recovered"]
    assert D(status["position_quantity"]) == 0 and status["stop_exits"] == 1
    assert rationale_kinds(directory).count("entry") == 1


def test_a_host_session_leaves_nothing_resting_after_a_partial_entry(tmp_path):
    directory = hf.new_session(tmp_path)
    run_all(directory, hf.ScriptedMarket(("2700",) * 4, size="0.1"), hf.Plans(hf.plan()))
    recovered = read_status(directory)["recovered"]
    assert D("0") < D(recovered["position_quantity"]) <= D("0.1")
    assert recovered["open_orders"] == []
    assert json.loads((directory / "state.json").read_bytes())["orders"] == []


def test_torn_tape_tail_is_cut_and_earlier_damage_stops_the_session(tmp_path):
    directory = hf.new_session(tmp_path)
    run_all(directory, hf.ScriptedMarket(("2700",) * 3), hf.Plans(hf.plan(**hf.NEUTRAL)))
    path = directory / "tape.jsonl"
    whole = path.read_bytes()
    path.write_bytes(whole + b'{"partial')
    reopened = MarketTape(path, "ETHUSDT")
    assert reopened.torn_tail_bytes == 9 and path.read_bytes() == whole
    path.write_bytes(whole.replace(b'"size":"200"', b'"size":"999"', 1))
    with pytest.raises(PaperStateError, match="damaged"):
        MarketTape(path, "ETHUSDT")


def test_tampered_manifest_is_refused(tmp_path):
    directory = hf.new_session(tmp_path)
    path = directory / "manifest.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"3000"', '"300000"'), encoding="utf-8")
    with pytest.raises(PaperStateError, match="seal"):
        load_manifest(directory)


def test_a_second_host_is_refused_while_the_first_owns_the_session(tmp_path):
    directory = hf.new_session(tmp_path)
    with SessionLock(directory):
        with pytest.raises(PaperStateError, match="owns this session"):
            with SessionLock(directory):
                pass


def test_stop_request_ends_the_run_and_resume_continues(tmp_path):
    directory = hf.new_session(tmp_path)
    market = hf.ScriptedMarket(("2700",) * 6)
    (directory / "stop.request").touch()
    instance = host(directory, market, hf.Plans(hf.plan(**hf.NEUTRAL)))
    assert instance.run(poll_seconds=0) == "stopped"
    assert not (directory / "stop.request").exists()
    assert instance.run(poll_seconds=0, max_cycles=2) == "cycle_limit"


def test_config_that_cannot_exit_a_full_position_is_refused():
    with pytest.raises(PaperStateError, match="exiting a full position"):
        hf.config(limits={**hf.fx.LIMIT_CONFIG, "max_gross_exposure_quote": "1000"})


# --- feed faults ---


def trade(identity, *, ms=0, price="2700"):
    return PublicTrade(identity, D(price), D("1"), 1_800_000_000_000_000_000 + ms * 1_000_000, False)


def test_tape_drops_duplicates_restamps_late_trades_and_counts_gaps(tmp_path):
    tape = MarketTape(tmp_path / "t.jsonl", "ETHUSDT")
    tape.ingest_book(PublicBook(D("2700"), D("1"), D("2701"), D("1"), 1_800_000_000_000_000_000))
    tape.ingest_trades([trade(1, ms=10), trade(2, ms=20)])
    tape.ingest_trades([trade(2, ms=20), trade(1, ms=10)])
    tape.ingest_trades([trade(3, ms=5), trade(9, ms=30)])
    tape.commit()
    assert tape.counters == {"duplicate_trades": 2, "late_trades": 1, "trade_id_gaps": 1}
    times = [row["ts_ns"] for row in tape.rows]
    assert times == sorted(set(times)) and all(ts % 1000 == 0 for ts in times)


def test_padded_venue_prices_are_normalized_and_sub_tick_prices_stop_the_session(tmp_path):
    tape = MarketTape(tmp_path / "t.jsonl", "ETHUSDT")
    tape.ingest_book(PublicBook(D("2259.41000000"), D("1"), D("2259.42000000"), D("1"), 1_800_000_000_000_000_000))
    assert tape.market_events()[0][0].levels[0].price.as_tuple().exponent == -2
    with pytest.raises(PaperStateError, match="finer than the session tick"):
        tape.ingest_trades([trade(1, price="2259.415")])


def test_crossed_or_empty_books_are_refused(tmp_path):
    tape = MarketTape(tmp_path / "t.jsonl", "ETHUSDT")
    assert tape.ingest_book(PublicBook(D("2701"), D("1"), D("2700"), D("1"), 1)) is None
    assert tape.ingest_book(PublicBook(D("2700"), D("0"), D("2701"), D("1"), 1)) is None
    assert tape.counters == {"invalid_books": 2}


def test_outage_backfill_quotes_around_prints_so_an_exit_cannot_use_a_stale_book(tmp_path):
    tape = MarketTape(tmp_path / "t.jsonl", "ETHUSDT", book_max_age_ns=1_000_000_000)
    tape.ingest_book(PublicBook(D("2700"), D("1"), D("2701"), D("1"), 1_800_000_000_000_000_000))
    tape.ingest_trades([trade(1, ms=60_000, price="2600")])
    books, trades = tape.market_events()
    assert tape.counters == {"synthetic_books": 1}
    assert books[-1].levels[0].price == D("2600") and books[-1].ts_ns < trades[0].ts_ns


def test_feed_outage_backs_off_keeps_state_and_recovers(tmp_path):
    directory = hf.new_session(tmp_path)
    market = hf.ScriptedMarket(("2700",) * 4)
    delays = []

    failures = []

    def flaky(after):
        if failures:
            raise failures.pop(0)
        return market(after)

    instance = PaperHost(directory, source=flaky, plan_provider=hf.Plans(hf.plan(**hf.NEUTRAL)),
                         wall_clock_ns=market.wall_clock, sleep=delays.append)
    assert instance.cycle()["feed"] == "up"
    failures += [FeedUnavailable("offline"), FeedUnavailable("offline", retry_after=30)]
    assert instance.cycle() == {"feed": "down"}
    assert json.loads((directory / "state.json").read_bytes())["feed_failures"] == 1
    assert instance.cycle() == {"feed": "down"}
    assert delays == [1.0, 30]
    assert instance.cycle()["feed"] == "up"


def test_no_observation_is_made_from_a_stale_feed(tmp_path):
    directory = hf.new_session(tmp_path, max_feed_age_seconds=1)
    market = hf.ScriptedMarket(("2700",) * 2)
    plans = hf.Plans(hf.plan())
    PaperHost(directory, source=market, plan_provider=plans,
              wall_clock_ns=lambda: market.wall_clock() + 10**12, sleep=lambda seconds: None).cycle()
    assert plans.contexts == []


# --- public reader boundary ---


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.replies.pop(0)


BOOK = {"symbol": "ETHUSDT", "bidPrice": "2700.00", "bidQty": "1.5", "askPrice": "2700.01", "askQty": "2.5"}
ROW = {"a": 7, "p": "2700.00", "q": "0.5", "T": 1_800_000_000_000, "m": True}


def test_reader_uses_only_approved_get_paths_with_no_redirects():
    session = FakeSession(FakeResponse(body=[ROW]), FakeResponse(body=BOOK))
    reader = BinancePublicReader("ETHUSDT", session=session, clock_ns=lambda: 5)
    batch = reader.poll(after_trade_id=6)
    assert isinstance(batch, MarketBatch)
    assert batch.trades[0].trade_id == 7 and batch.book.ask == D("2700.01")
    urls = [url for url, _ in session.calls]
    assert urls == ["https://api.binance.com/api/v3/aggTrades", "https://api.binance.com/api/v3/ticker/bookTicker"]
    assert all(call["allow_redirects"] is False for _, call in session.calls)
    assert session.calls[0][1]["params"]["fromId"] == 7
    with pytest.raises(FeedUnavailable, match="unapproved"):
        reader._get("/api/v3/order", {})


@pytest.mark.parametrize("reply, retry", [
    (FakeResponse(429, headers={"Retry-After": "7"}), 7.0),
    (FakeResponse(418), None), (FakeResponse(503), None), (FakeResponse(200), None),
    (FakeResponse(200, body={"unexpected": 1}), None),
])
def test_reader_fails_closed_on_rate_limits_and_bad_payloads(reply, retry):
    reader = BinancePublicReader("ETHUSDT", session=FakeSession(reply), clock_ns=lambda: 5)
    with pytest.raises(FeedUnavailable) as caught:
        reader.poll(after_trade_id=1)
    assert caught.value.retry_after == retry


def test_new_host_modules_have_no_write_credential_or_venue_path():
    root = Path(host_module.__file__).parent
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in ("api_key", "secret", "signature", "X-MBX", ".post(", ".put(", ".delete(",
                      "/api/v3/order", "fapi", "testnet", "mainnet"):
            assert token.lower() not in source.lower(), (path.name, token)
        tree = ast.parse(source)
        names = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        names |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                  for alias in node.names}
        assert not [name for name in names if any(
            word in name for word in ("binance_testnet", "mainnet", "production", "breakglass", "crew_decision_gateway"))]
    assert public_market.BASE_URL == "https://api.binance.com"


# --- CLI and real Crew wiring ---


def test_cli_start_status_stop_resume_with_a_labeled_stub(tmp_path, monkeypatch, capsys):
    from crypto_trader.paper_host import __main__ as cli

    market = hf.ScriptedMarket(("2700",) * 12)
    monkeypatch.setattr(BinancePublicReader, "poll", lambda self, *, after_trade_id: market(after_trade_id))
    monkeypatch.setattr(host_module.time, "time_ns", market.wall_clock)
    values = {name: value for name, value in hf.config().__dict__.items() if name not in {"session_id", "provider_kind"}}
    config = tmp_path / "config.json"
    config.write_text(json.dumps(values), encoding="utf-8")
    plans = tmp_path / "plans.json"
    plans.write_text(json.dumps([hf.plan(**hf.NEUTRAL)]), encoding="utf-8")
    directory = tmp_path / "s"
    common = ["--dir", str(directory), "--provider", "stub", "--plans", str(plans), "--poll-seconds", "0"]
    assert cli.main(["start", "--config", str(config), *common, "--max-cycles", "3"]) == 0
    assert cli.main(["resume", *common, "--max-cycles", "2"]) == 0
    assert cli.main(["status", "--dir", str(directory)]) == 0
    assert cli.main(["stop", "--dir", str(directory)]) == 0
    assert (directory / "stop.request").exists()
    assert cli.main(["resume", *common]) == 0
    with pytest.raises(SystemExit, match="manifest"):
        cli.main(["resume", "--dir", str(directory), "--provider", "crew"])
    assert '"provider_kind": "stub"' in capsys.readouterr().out


def test_host_drives_a_real_crew_kickoff_with_a_stub_llm(tmp_path, monkeypatch):
    from crewai.llms.base_llm import BaseLLM

    from crypto_trader import crew as crew_module
    from crypto_trader.crew_paper_bridge import crew_plan_provider

    prompts = []

    class StubLLM(BaseLLM):
        def __init__(self):
            super().__init__(model="offline-stub", stop=[])

        def call(self, messages, tools=None, callbacks=None, available_functions=None,
                 from_task=None, from_agent=None):
            text = messages if isinstance(messages, str) else "\n".join(item["content"] for item in messages)
            if "MACHINE-READABLE SUMMARY" not in text:
                return "Thought: done\nFinal Answer: offline context"
            prompts.append(text)
            return "Thought: done\nFinal Answer: " + hf.plan()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(crew_module, "get_llm", lambda profile: StubLLM())
    directory = hf.new_session(tmp_path)
    run_all(directory, hf.ScriptedMarket(("2700",) * 6), crew_plan_provider)
    assert prompts and all("CURRENT SIMULATED PORTFOLIO" in prompt for prompt in prompts)
    assert len(journal_rows(directory, "execution.fill.recorded")) >= 1
    assert not list(tmp_path.glob("**/trading_plan_*"))
