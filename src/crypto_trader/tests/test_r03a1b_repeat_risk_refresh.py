"""Isolated no-submit regression for repeated canonical risk refreshes."""
from datetime import timedelta
from decimal import Decimal

import pytest

from crypto_trader.execution.binance_testnet.market_data import BinanceTestnetTickerQuoteSource
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStateError
from crypto_trader.production.risk_state import ProductionRiskStateSource
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.test_production_risk_state_source import (
    ACCOUNT, ENV, INSTRUMENT, NOW, seed_baseline,
)
from crypto_trader.tests.test_r03a1b_testnet_ticker_quote import FakeSocket, ticker


def refresh_without_untimed_price():
    return [
        fx.time_response(fx.SERVER_TIME_MS + 1),
        fx.open_orders_response(), fx.account_response(), fx.open_orders_response(),
    ]


def _source(journal, control, factory, *, cycles=2, mark_source=None):
    transport = fx.ScriptedTransport(refresh_without_untimed_price() * cycles)
    client = fx.client(transport)
    return ProductionRiskStateSource(
        journal=journal, control=control, client=client,
        instrument=INSTRUMENT, environment=ENV, venue_account_id=ACCOUNT,
        max_account_age_seconds=5, max_mark_age_seconds=5,
        mark_source_factory=factory, mark_source=mark_source,
    ), transport


def _factory(sockets):
    def fresh():
        sock = FakeSocket(ticker(E=int(NOW.timestamp() * 1000), a='27001.00'))
        sockets.append(sock)
        return BinanceTestnetTickerQuoteSource(
            symbol=INSTRUMENT.symbol, clock=lambda: NOW,
            max_event_age=timedelta(seconds=5), max_spread_bps=Decimal('100'),
            connect=lambda _url, _timeout: sock,
        )
    return fresh


def test_two_sequential_risk_refreshes_each_get_new_verified_venue_mark(tmp_path):
    (tmp_path / 'state').mkdir()
    with SQLiteEventJournal(tmp_path / 'orders.db') as journal:
        control, report = seed_baseline(journal, tmp_path / 'state')
        guard = control.claim_normal_writer(owner_id='normal:runtime', reason_code='test_runtime')
        sockets = []
        source, transport = _source(journal, control, _factory(sockets))
        first, first_at = source(NOW, guard=guard)
        second, second_at = source(NOW, guard=guard)
        assert first.equity_quote == report.equity_quote == second.equity_quote
        assert first_at == second_at == NOW + timedelta(milliseconds=1)
        assert len(sockets) == 2
        assert all(sock.closed and sock.read_count == 1 for sock in sockets)
        assert len(transport.requests) == 8
        assert all(req.method == 'GET' for req in transport.requests)
        assert journal.count() == 1


def test_reusing_one_shot_factory_source_refuses_second_refresh_without_rest_fallback(tmp_path):
    (tmp_path / 'state').mkdir()
    with SQLiteEventJournal(tmp_path / 'orders.db') as journal:
        control, _ = seed_baseline(journal, tmp_path / 'state')
        guard = control.claim_normal_writer(owner_id='normal:runtime', reason_code='test_runtime')
        sockets = []
        old_source = _factory(sockets)()
        source, transport = _source(journal, control, lambda: old_source)
        source(NOW, guard=guard)
        with pytest.raises(ProductionStateError, match='venue risk evidence refresh failed'):
            source(NOW, guard=guard)
        assert len(sockets) == 1 and sockets[0].read_count == 1
        assert len(transport.requests) == 7
        assert journal.count() == 1


def test_factory_rejects_wrong_symbol_and_untrusted_type_without_a_post(tmp_path):
    (tmp_path / 'state').mkdir()
    with SQLiteEventJournal(tmp_path / 'orders.db') as journal:
        control, _ = seed_baseline(journal, tmp_path / 'state')
        guard = control.claim_normal_writer(owner_id='normal:runtime', reason_code='test_runtime')
        for factory in (lambda: object(), lambda: BinanceTestnetTickerQuoteSource(
            symbol='ETHUSDT', clock=lambda: NOW,
            max_event_age=timedelta(seconds=5), max_spread_bps=Decimal('100'),
            connect=lambda _url, _timeout: FakeSocket(ticker()),
        )):
            source, transport = _source(journal, control, factory, cycles=1)
            with pytest.raises(ProductionStateError, match='venue risk evidence refresh failed'):
                source(NOW, guard=guard)
            assert len(transport.requests) == 3
            assert all(req.method == 'GET' for req in transport.requests)
        assert journal.count() == 1


def test_rejects_factory_and_legacy_one_shot_mark_at_same_time(tmp_path):
    (tmp_path / 'state').mkdir()
    with SQLiteEventJournal(tmp_path / 'orders.db') as journal:
        control, _ = seed_baseline(journal, tmp_path / 'state')
        sockets = []
        with pytest.raises(ProductionStateError, match='cannot share a one-shot source'):
            _source(journal, control, _factory(sockets), mark_source=_factory(sockets)())