"""The whole AI-to-Paper path on one synthetic tape, across two real process deaths.

Decision -> validity -> risk -> simulated fill -> stop -> feedback -> re-entry -> target, then an
audit of the durable evidence alone. Plans are fixtures: this proves the system's mechanics and
its recovery, and says nothing about any model's or strategy's quality.
"""
import json
import os
import socket
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_trader.events import VALIDITY_EVENT_TYPE
from crypto_trader.paper import report_from_journal
from crypto_trader.paper_host.host import read_status
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import host_fixtures as hf

D = Decimal

_CHILD = """
import os, sys
from pathlib import Path
from crypto_trader.paper_host.host import PaperHost
from crypto_trader.tests import host_fixtures as hf
directory, cycles = Path(sys.argv[1]), int(sys.argv[2])
market = hf.ScriptedMarket(hf.LIFECYCLE_PRICES)
plans = hf.Plans(*hf.LIFECYCLE_PLANS, log=directory / "calls.log")
instance = PaperHost(directory, source=market, plan_provider=plans, wall_clock_ns=market.wall_clock,
                     sleep=lambda seconds: None)
for _ in range(cycles):
    instance.cycle()
sys.stdout.flush()
os._exit(9)
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("network refused by offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)


def die_after(directory: Path, cycles: int) -> None:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)}
    child = subprocess.run([sys.executable, "-s", "-c", _CHILD, str(directory), str(cycles)],
                           env=env, capture_output=True, timeout=180)
    assert child.returncode == 9, child.stderr[-3000:]


def test_full_lifecycle_across_two_process_deaths_from_durable_evidence_alone(tmp_path):
    directory = hf.new_session(tmp_path)
    die_after(directory, 3)    # Position open, stop armed, nothing has fired.
    held = read_status(directory)["recovered"]
    assert D(held["position_quantity"]) > 0 and held["fills"] == 1

    die_after(directory, 4)    # The stop fired in this process.
    stopped = read_status(directory)["recovered"]
    assert stopped["stop_exits"] == 1 and D(stopped["position_quantity"]) == 0

    die_after(directory, 30)   # The re-entry and its target, then the market runs out.
    status = read_status(directory)
    recovered = status["recovered"]
    assert status["provider_kind"] == "stub"

    with SQLiteEventJournal(directory / "journal.db") as journal:
        events = journal.read_by_correlation(hf.fx.SESSION_ID)
        rebuilt = report_from_journal(events, session_id=hf.fx.SESSION_ID, mode="paper",
                                      symbol="ETHUSDT", strategy_id="crew-paper-host",
                                      strict_native_provenance=True)

    def of(kind):
        return [event for event in events if event.event_type == kind]

    decisions = of("strategy.decision.recorded")
    intents = of("trade.intent.created")
    risks = of("risk.evaluation.recorded")
    validity = of(VALIDITY_EVENT_TYPE)
    fills = of("execution.fill.recorded")

    # Decision validated and accepted or held, risk before execution.
    assert len(decisions) == len({event.event_id for event in decisions}) >= 5
    assert {event.payload.action for event in decisions} == {"buy", "hold"}
    assert len(validity) == 2  # Only strategy entries; exits are not subject to staleness.
    assert {event.payload.decision for event in risks} == {"allowed"}
    assert len(risks) == len(intents) == 4

    # Open, stop, re-open, target: each exactly once, from allowed intents only.
    assert [text.payload.rationale.split(":")[1] if text.payload.rationale.startswith("protective")
            else "entry" for text in sorted(intents, key=lambda e: e.occurred_at)] == [
        "entry", "stop_loss", "entry", "take_profit"]
    assert len(fills) == len({event.event_id for event in fills}) == 4
    assert {event.causation_id for event in fills} <= {event.event_id for event in intents}

    # Portfolio and balance: bought and sold quantities net to flat, and cash carries the P&L.
    assert rebuilt.fills and D(recovered["position_quantity"]) == 0
    sides = {event.event_id: event.payload.side for event in intents}
    net = sum(event.payload.quantity if sides[event.causation_id] == "buy" else -event.payload.quantity
              for event in fills)
    cash = D("100000") + sum(
        (-1 if sides[event.causation_id] == "buy" else 1) * event.payload.quantity * event.payload.price
        - event.payload.fee for event in fills)
    assert net == 0
    assert cash == D(recovered["quote_free"])
    assert D(recovered["realized_pnl_quote"]) == cash - D("100000")
    assert recovered["open_orders"] == []

    # The outcome changed the next decision's input and its size.
    records = {json.loads(path.read_bytes())["market_reference"]: json.loads(path.read_bytes())
               for path in (directory / "decisions").iterdir()}
    assert D(records["public.book#2"]["decision"]["quantity"]) < D(records["public.book#0"]["decision"]["quantity"])

    # No plan was requested twice for one observation across both restarts.
    calls = (directory / "calls.log").read_text(encoding="utf-8").split()
    assert len(calls) == len(set(calls))
    assert (directory / "tape.jsonl").stat().st_size > 0
