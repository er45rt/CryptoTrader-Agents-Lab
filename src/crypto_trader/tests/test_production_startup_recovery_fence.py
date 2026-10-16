"""Startup recovery is journaled only while the runtime still holds its writer fence."""
import re

import pytest

from crypto_trader.breakglass.errors import StaleWriterFenceError
from crypto_trader.breakglass.ownership import OwnerKind, OwnershipStore
from crypto_trader.execution.binance_testnet.startup import BinanceStartupCollector
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import BASE, startup_responses
from crypto_trader.tests.test_production_testnet_integration import _wired

RECOVERY_EVENT_TYPE = "recovery.state.changed"


def test_takeover_before_startup_recovery_writes_no_recovery_events(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with SQLiteEventJournal(tmp_path / "journal.db", venue_writes=True) as journal:
        transport = fx.ScriptedTransport(startup_responses())
        runtime, _port, _client = _wired(journal, state_dir, transport)
        before = {event.event_id for event in journal.replay()}
        validate = BinanceStartupCollector.validate
        taken = []

        def validate_then_take_over(self, *args, **kwargs):
            source = validate(self, *args, **kwargs)
            if not taken:  # after the last pre-recovery fence check, outside the lock
                taken.append(OwnershipStore(state_dir / "ownership.json").takeover(
                    owner_id="emergency:test", owner_kind=OwnerKind.EMERGENCY,
                    acquired_at=BASE, reason_code="offline-regression",
                ).term)
            return source

        monkeypatch.setattr(BinanceStartupCollector, "validate", validate_then_take_over)
        with pytest.raises(StaleWriterFenceError, match=re.escape(
            "writer fence term 2 owned by 'normal:production-runtime' (normal) is stale: the "
            "trading-state lease is held by term 3 owned by 'emergency:test' (emergency)."
        )):
            runtime.start()

        written = [event for event in journal.replay() if event.event_id not in before]
        assert taken, "the takeover must land inside the startup window"
        assert runtime.status()["last_refusal"] == "recovery:StaleWriterFenceError"
        assert [event.event_type for event in written if event.event_type == RECOVERY_EVENT_TYPE] == []
