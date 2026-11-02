"""R-9-only offline runtime fixture; NOT a Crew host/bootstrap E2E claim."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from crypto_trader.execution.binance_testnet import TestnetOrderRuntime
from crypto_trader.execution.binance_testnet.market_data import BinanceTestnetTickerQuoteSource
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production.risk_state import ProductionRiskStateSource
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    INSTRUMENT,
    bind_test_admission,
    build_runtime,
    control_for,
    intent,
    canonical_risk_refresh_responses,
    seed_canonical_equity_root,
    startup_responses,
)


NOW = datetime.fromtimestamp(fx.SERVER_TIME_MS / 1000, tz=timezone.utc)


class FakeSocket:
    def __init__(self, frame, *, fails=False):
        self.frame, self.fails = frame, fails
        self.closed, self.read_count = False, 0

    def recv(self):
        self.read_count += 1
        if self.fails:
            raise OSError("injected connection drop")
        return self.frame

    def close(self):
        self.closed = True


def ticker(**changes):
    payload = {
        "e": "24hrTicker",
        "E": int(NOW.timestamp() * 1000),
        "s": "BTCUSDT",
        "b": "26999.00",
        "B": "10",
        "a": "27001.00",
        "A": "11",
    }
    payload.update(changes)
    return json.dumps(payload)


class MutableTestMarket:
    def __init__(self, factory):
        self._ticker_factory = factory
        self._observation = None

    def latest(self, symbol):
        value = self._observation
        return value if value is not None and value.symbol == symbol else None

    def new_risk_reader(self):
        return self._ticker_factory()


def prepare(tmp_path, monkeypatch):
    sockets = []

    def factory():
        sock = FakeSocket(ticker())
        sockets.append(sock)
        return BinanceTestnetTickerQuoteSource(
            symbol=INSTRUMENT.symbol,
            clock=lambda: NOW,
            max_event_age=timedelta(seconds=5),
            max_spread_bps=Decimal("100"),
            connect=lambda *_: sock,
        )

    market = MutableTestMarket(factory)
    responses = [*startup_responses(), *canonical_risk_refresh_responses()]
    del responses[-2]  # strict source must not consume an untimed REST mark
    journal = SQLiteEventJournal(tmp_path / "r9-crossfeed.db", venue_writes=True)
    seed_canonical_equity_root(journal)
    transport = fx.ScriptedTransport(responses)
    client = fx.client(transport, retries=1)
    port = TestnetOrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    control = control_for(journal, tmp_path / "state", when=NOW)
    risk_source = ProductionRiskStateSource(
        journal=journal,
        control=control,
        client=client,
        instrument=INSTRUMENT,
        environment="binance-spot-testnet",
        venue_account_id="123456789",
        mark_source_factory=market.new_risk_reader,
        max_decision_mark_move_bps=Decimal("50"),
    )
    runtime = build_runtime(
        journal=journal,
        state_dir=tmp_path / "state",
        execution=port,
        control=control,
        risk_state_source=risk_source,
        market=market,
        when=NOW,
    )
    admission = bind_test_admission(runtime, when=NOW)
    runtime.start()
    quote, observation = factory().read_once_bound()
    market._observation = observation
    candidate_intent = intent(
        "r9-crossfeed",
        decision_started_at=NOW,
        decision_completed_at=NOW,
        market_event_ts=quote.event_ts,
        reference_price=quote.best_ask,
        limit_price=quote.best_ask,
        market_reference=quote.reference_id,
        valid_until=NOW + timedelta(seconds=3),
    )
    synthetic_input = SimpleNamespace(
        run=lambda: SimpleNamespace(receipt=SimpleNamespace(intent=candidate_intent))
    )
    return journal, runtime, admission, synthetic_input, transport, sockets, market
