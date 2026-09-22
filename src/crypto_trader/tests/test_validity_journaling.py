"""Phase 6: every validity decision is journaled and replays deterministically."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from crypto_trader.events import ValidityDispositionRecorded, decode_event
from crypto_trader.persistence import IdempotentConsumer, SQLiteEventJournal, rebuild_state
from crypto_trader.validity import (
    DecisionValidityGate,
    Disposition,
    ReplayMarketStateProvider,
    ValidityPolicy,
    ValidityRequest,
    record_validity_decision,
)
from crypto_trader.validity.market_state import MarketObservation

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
POLICY = ValidityPolicy(policy_id="strat-1-v3", max_age_seconds=5.0, max_market_move_bps=Decimal("25"))


def observation(offset_s: float, price: str) -> MarketObservation:
    return MarketObservation(
        symbol="ETHUSDT",
        price=Decimal(price),
        market_event_ts=BASE + timedelta(seconds=offset_s),
        local_ingest_ts=BASE + timedelta(seconds=offset_s),
        market_reference=f"obs-{offset_s}",
    )


def request(*, completed_offset_s: float = 1.0, market_offset_s: float = 0.0) -> ValidityRequest:
    return ValidityRequest(
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        decision_started_at=BASE + timedelta(seconds=market_offset_s),
        decision_completed_at=BASE + timedelta(seconds=completed_offset_s),
        market_event_ts=BASE + timedelta(seconds=market_offset_s),
        reference_price=Decimal("2700.00"),
        market_reference=f"obs-{market_offset_s}",
    )


class DispositionConsumer(IdempotentConsumer):
    def __init__(self) -> None:
        super().__init__()
        self.dispositions: list[str] = []

    def apply(self, event) -> None:
        if isinstance(event, ValidityDispositionRecorded):
            self.dispositions.append(event.payload.disposition)


def test_every_decision_is_journaled_as_the_additive_contract(tmp_path: Path):
    gate = DecisionValidityGate(POLICY)
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        accepted = gate.evaluate(
            request(completed_offset_s=1.0),
            market_state=ReplayMarketStateProvider([observation(1.0, "2700.50")]),
            now=BASE + timedelta(seconds=2),
        )
        stale = gate.evaluate(
            request(completed_offset_s=9.0, market_offset_s=0.0),
            market_state=ReplayMarketStateProvider([observation(9.0, "2700.50")]),
            now=BASE + timedelta(seconds=9),
        )

        record_validity_decision(journal, accepted)
        record_validity_decision(journal, stale)

        events = [event for event in journal.replay()]

    assert [event.payload.disposition for event in events] == ["accepted", "stale"]
    assert all(isinstance(event, ValidityDispositionRecorded) for event in events)
    assert events[0].payload.policy_id == "strat-1-v3"
    assert events[0].payload.intent_id == accepted.intent_id
    assert events[1].payload.decision_age_seconds == pytest.approx(9.0)
    assert events[0].event_type == "validity.disposition.recorded"


def test_journaled_decisions_replay_deterministically(tmp_path: Path):
    gate = DecisionValidityGate(POLICY)
    # 2700.00 -> 2710.00 is ~37 bps, above the 25 bps tolerance: both decisions must be
    # MARKET_MOVED, deterministically.
    history = [observation(0.0, "2700.00"), observation(1.0, "2710.00")]
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        for index, provider in enumerate(
            [ReplayMarketStateProvider(history), ReplayMarketStateProvider(list(history))]
        ):
            provider.advance("ETHUSDT")  # move both replays to the same second observation
            decision = gate.evaluate(
                request(completed_offset_s=1.0 + index, market_offset_s=0.0),
                market_state=provider,
                now=BASE + timedelta(seconds=2 + index),
            )
            record_validity_decision(journal, decision)

        first = DispositionConsumer()
        rebuild_state(journal, first)
        second = DispositionConsumer()
        rebuild_state(journal, second)

    assert first.dispositions == second.dispositions == ["market_moved", "market_moved"]


def test_journaled_decision_round_trips_through_the_registry(tmp_path: Path):
    gate = DecisionValidityGate(POLICY)
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        decision = gate.evaluate(
            request(completed_offset_s=1.0),
            market_state=ReplayMarketStateProvider([observation(1.0, "2700.00")]),
            now=BASE + timedelta(seconds=2),
        )
        record_validity_decision(journal, decision)
        raw = journal._conn.execute("SELECT payload_json FROM events").fetchone()["payload_json"]
        decoded = decode_event(raw)

    assert isinstance(decoded, ValidityDispositionRecorded)
    assert decoded.payload.disposition == Disposition.ACCEPTED.value
    assert decoded.payload.market_event_ts == BASE
    assert decoded.payload.decision_completed_at == BASE + timedelta(seconds=1)


def test_redelivered_decision_does_not_duplicate_journal_effects(tmp_path: Path):
    gate = DecisionValidityGate(POLICY)
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        decision = gate.evaluate(
            request(completed_offset_s=1.0),
            market_state=ReplayMarketStateProvider([observation(1.0, "2700.00")]),
            now=BASE + timedelta(seconds=2),
        )
        event_id = record_validity_decision(journal, decision)
        # Re-delivery of the SAME logical decision reuses its event identity.
        record_validity_decision(journal, decision, event_id=event_id)

        assert journal.count() == 1
        assert journal.read_by_id(event_id) is not None
