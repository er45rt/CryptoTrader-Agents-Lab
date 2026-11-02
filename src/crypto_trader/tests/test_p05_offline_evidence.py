"""P-05 offline ETHUSDT: pinned fixture, real Nautilus engine, independent reconciliation.

All observations/trades are frozen synthetic public-shaped inputs. No network, keys,
broker adapters, OMS writes or live/testnet orders. Paper research evidence only.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from crypto_trader.events import EXECUTION_FILL_EVENT_TYPE, STRATEGY_DECISION_EVENT_TYPE
from crypto_trader.execution import BookLevel, InstrumentSpec, MarketBookUpdate, MarketTrade
from crypto_trader.execution.nautilus import NautilusSimulationRunner
from crypto_trader.paper import (
    FrozenMarketDataFeed, MarketObservation, ScriptedDecisionSource,
    report_from_journal,
)
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "p05_ethusdt_offline_v1.json"
FIXTURE_SHA256 = "773f353ccd8410a9a45731585a381a264f761da36699c62bf15cbc7285aa9526"
NSEC = 1_000_000_000


def _fixture() -> dict:
    blob = FIXTURE_PATH.read_bytes()
    assert hashlib.sha256(blob).hexdigest() == FIXTURE_SHA256, "offline fixture changed"
    data = json.loads(blob)
    assert data["engine_version"] == "1.231.0"
    assert data["book_type"] == "L2_MBP"
    assert data["symbol"] == "ETHUSDT"
    return data


def _run_once(tmp_path: Path, name: str):
    data = _fixture()
    observed = [
        MarketObservation(
            symbol=data["symbol"],
            price=Decimal(item["mark"]),
            observed_at=datetime.fromisoformat(item["observed_at"]),
            market_reference=item["market_reference"],
            volume=Decimal(item["volume"]),
        )
        for item in data["observations"]
    ]
    feed = FrozenMarketDataFeed(entries=tuple(observed), feed_id=data["fixture_id"])
    decisions = ScriptedDecisionSource(script={
        observed[0].market_reference: fx.buy_decision(
            quantity=data["observations"][0]["quantity"],
            limit_price=data["observations"][0]["mark"],
        ),
        observed[1].market_reference: fx.hold_decision(),
    })
    book = data["l2_book"]
    at_ns = int(observed[0].observed_at.timestamp() * NSEC)
    l2 = MarketBookUpdate(
        levels=[
            BookLevel(side="BUY", price=Decimal(book["bid_price"]),
                      size=Decimal(book["bid_size"]), order_id=1),
            BookLevel(side="SELL", price=Decimal(book["ask_price"]),
                      size=Decimal(book["ask_size"]), order_id=2),
        ],
        ts_ns=at_ns, sequence=1,
    )
    trades = tuple(
        MarketTrade(
            price=Decimal(item["price"]),
            size=Decimal(item["size"]),
            aggressor=item["aggressor"],
            ts_ns=at_ns + int(item["offset_ms"]) * 1_000_000,
            trade_id=item["trade_id"],
        )
        for item in data["trades"]
    )
    simulator = NautilusSimulationRunner(
        instrument=InstrumentSpec(symbol=data["symbol"]),
        book_updates=(l2,),
        trades=trades,
    )

    with SQLiteEventJournal(tmp_path / f"{name}.db") as journal:
        session = fx.session(journal, decisions=decisions, data_feed=feed,
                             simulator=simulator)
        report = session.run()
        events = tuple(journal.read_by_correlation(fx.SESSION_ID))
        journal_replay = report_from_journal(
            journal.read_range(),
            session_id=fx.SESSION_ID, mode="paper", symbol=data["symbol"],
            strategy_id="strategy-paper-1",
        )
        native_fills = [
            event for event in events if event.event_type == EXECUTION_FILL_EVENT_TYPE
        ]
        decision_events = [
            event for event in events if event.event_type == STRATEGY_DECISION_EVENT_TYPE
        ]
        return data, report, journal_replay, events, native_fills, decision_events


def test_p05_independent_native_fill_fee_inventory_and_equity_reconcile(tmp_path: Path):
    data, report, replay, events, native_fills, decisions = _run_once(tmp_path, "first")
    assert report.engine_version == "1.231.0"
    assert report.book_type == "L2_MBP" and report.queue_aware is True
    assert len(decisions) == 2
    assert len(native_fills) == 1
    assert len(report.orders) == 1 and report.orders[0].state == "filled"
    assert len(report.fills) == 1
    assert len(events) == len({event.event_id for event in events})

    # Calculate from the persisted native Phase-3 journal ONLY, not RunReport.
    qty = sum((event.payload.quantity for event in native_fills), Decimal("0"))
    amount = sum((event.payload.quantity * event.payload.price for event in native_fills), Decimal("0"))
    fee = sum((event.payload.fee or Decimal("0") for event in native_fills), Decimal("0"))
    avg = amount / qty
    last_mark = Decimal(data["observations"][-1]["mark"])
    starting = Decimal(data["starting_equity_quote"])
    expected_unrealized = (last_mark - avg) * qty
    expected_equity = starting - fee + expected_unrealized

    assert qty == Decimal("1.00000000")
    assert avg == Decimal("2700.00")
    assert all(event.occurred_at > decisions[0].occurred_at for event in native_fills)
    assert report.positions == {"ETHUSDT": qty}
    assert report.fills[0].quantity == qty
    assert report.realized_pnl_quote == -fee
    assert report.unrealized_pnl_quote == expected_unrealized
    assert report.equity_quote == expected_equity
    assert replay.observations_processed == 2
    assert len(replay.fills) == 1
    assert replay.fills[0].quantity == qty and replay.fills[0].price == avg
    assert replay.orders[0].state == report.orders[0].state


def test_p05_frozen_fixture_repeats_semantic_hash_and_native_event_identifiers(tmp_path: Path):
    first = _run_once(tmp_path, "first")
    second = _run_once(tmp_path, "second")
    _, report1, replay1, events1, fills1, decisions1 = first
    _, report2, replay2, events2, fills2, decisions2 = second

    assert report1.semantic_hash() == report2.semantic_hash()
    assert report1.to_json() == report2.to_json()
    assert {event.event_id for event in events1} == {event.event_id for event in events2}
    assert {event.event_id for event in fills1} == {event.event_id for event in fills2}
    assert replay1.validity_counts == replay2.validity_counts
    assert replay1.risk_counts == replay2.risk_counts
    assert [event.payload.action for event in decisions1] == [
        event.payload.action for event in decisions2
    ]
