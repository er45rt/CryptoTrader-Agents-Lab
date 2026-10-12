"""P-04: persisted-journal restart must fail closed on interrupted observations.

Offline sqlite only; no venue, network, credentials or external order transport.
"""
from pathlib import Path

import pytest

from crypto_trader.events import STRATEGY_DECISION_EVENT_TYPE
from crypto_trader.paper import PaperStateError, ScriptedSimulationRunner
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import paper_fixtures as fx


@pytest.mark.parametrize("crash_boundary", [
    "after_decision_before_intent",
    "after_intent_before_validity",
    "after_validity_before_risk",
])
def test_p04_partial_journal_does_not_advance_resume_cursor(tmp_path: Path, crash_boundary):
    path = tmp_path / "durable-crash.db"
    with SQLiteEventJournal(path) as journal:
        session = fx.session(
            journal,
            decisions=fx.scripted(fx.buy_decision()),
            data_feed=fx.feed("2700.00"),
            simulator=ScriptedSimulationRunner(),
        )
        if crash_boundary == "after_decision_before_intent":
            def crash(*args, **kwargs):
                raise OSError("injected interruption before intent")
            session._record_intent = crash
        elif crash_boundary == "after_intent_before_validity":
            original = session._record_intent
            def crash(*args, **kwargs):
                original(*args, **kwargs)
                raise OSError("injected interruption before validity")
            session._record_intent = crash
        else:
            def crash(*args, **kwargs):
                raise OSError("injected interruption before risk")
            session._record_risk = crash

        with pytest.raises(OSError, match="injected interruption"):
            session.run()
        before = tuple(event.event_id for event in journal.read_by_correlation(fx.SESSION_ID))
        assert before
        assert sum(1 for e in journal.read_by_correlation(fx.SESSION_ID)
                   if e.event_type == STRATEGY_DECISION_EVENT_TYPE) == 1

    with SQLiteEventJournal(path) as reopened:
        restarted = fx.session(
            reopened,
            decisions=fx.scripted(fx.buy_decision()),
            data_feed=fx.feed("2700.00"),
            simulator=ScriptedSimulationRunner(),
        )
        with pytest.raises(PaperStateError, match="incomplete"):
            restarted.resume_index()
        after = tuple(event.event_id for event in reopened.read_by_correlation(fx.SESSION_ID))
        assert before == after, "failing closed must not rewrite or duplicate the durable journal"


def test_p04_reopened_complete_journal_has_stable_cursor_and_no_duplicate_ids(tmp_path: Path):
    path = tmp_path / "durable-success.db"
    with SQLiteEventJournal(path) as journal:
        session = fx.session(
            journal,
            decisions=fx.scripted(fx.buy_decision(), fx.hold_decision()),
            data_feed=fx.feed("2700.00", "2701.00"),
            simulator=ScriptedSimulationRunner(),
        )
        session.run()
        original = tuple(e.event_id for e in journal.read_by_correlation(fx.SESSION_ID))
        assert len(original) == len(set(original))
    with SQLiteEventJournal(path) as reopened:
        restarted = fx.session(
            reopened,
            decisions=fx.scripted(fx.buy_decision(), fx.hold_decision()),
            data_feed=fx.feed("2700.00", "2701.00"),
            simulator=ScriptedSimulationRunner(),
        )
        assert restarted.resume_index() == 2
        assert tuple(e.event_id for e in reopened.read_by_correlation(fx.SESSION_ID)) == original
