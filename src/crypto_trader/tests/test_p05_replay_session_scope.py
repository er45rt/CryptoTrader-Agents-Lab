"""P-05 cross-session replay isolation regression (offline only)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace as Record
from uuid import UUID

import pytest

from crypto_trader.events import (
    STRATEGY_DECISION_EVENT_TYPE, RISK_DECISION_EVENT_TYPE,
    ORDER_STATE_EVENT_TYPE, EXECUTION_FILL_EVENT_TYPE,
)
from crypto_trader.paper import PaperStateError, report_from_journal

OWN = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def event(owner, kind, at, **payload):
    return Record(
        correlation_id=owner, event_type=kind,
        occurred_at=BASE + timedelta(seconds=at),
        payload=Record(**payload), order_id=payload.get("order_id"),
        symbol="ETHUSDT",
    )


def replay(events):
    return report_from_journal(
        events, session_id=OWN, mode="paper",
        symbol="ETHUSDT", strategy_id="synthetic",
    )


def test_p05_replay_excludes_other_sessions_and_timestamps():
    order = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    other_order = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    events = [
        event(OTHER, STRATEGY_DECISION_EVENT_TYPE, 0, action="sell", confidence="low"),
        event(OTHER, RISK_DECISION_EVENT_TYPE, 1, approved=False),
        event(OTHER, ORDER_STATE_EVENT_TYPE, 2, order_id=other_order, state="filled"),
        event(OTHER, EXECUTION_FILL_EVENT_TYPE, 3, order_id=other_order,
              quantity=Decimal("9"), price=Decimal("999"), remaining_quantity=Decimal("0")),
        event(OWN, STRATEGY_DECISION_EVENT_TYPE, 10, action="buy", confidence="medium"),
        event(OWN, RISK_DECISION_EVENT_TYPE, 12, approved=True),
        event(OWN, ORDER_STATE_EVENT_TYPE, 13, order_id=order, state="filled"),
        event(OWN, EXECUTION_FILL_EVENT_TYPE, 14, order_id=order,
              quantity=Decimal("1"), price=Decimal("2700"), remaining_quantity=Decimal("0")),
        event(OTHER, STRATEGY_DECISION_EVENT_TYPE, 100, action="sell", confidence="high"),
    ]
    report = replay(events)
    assert report.observations_processed == 1
    assert report.risk_counts == {"allowed": 1}
    assert report.started_at == BASE + timedelta(seconds=10)
    assert report.ended_at == BASE + timedelta(seconds=14)
    assert len(report.orders) == 1
    assert report.orders[0].order_reference == str(order)
    assert len(report.fills) == 1
    assert report.fills[0].quantity == Decimal("1")
    assert report.fills[0].price == Decimal("2700")
    assert report.fills[0].correlation_id == OWN


def test_p05_other_sessions_do_not_satisfy_required_decision():
    with pytest.raises(PaperStateError, match="no recorded decisions"):
        replay([event(OTHER, STRATEGY_DECISION_EVENT_TYPE, 0,
                      action="buy", confidence="medium")])
