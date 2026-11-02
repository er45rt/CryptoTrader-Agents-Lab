"""Adversarial red test: never make a venue write; intercept the durable boundary."""
from dataclasses import replace
from decimal import Decimal
from datetime import timedelta

import pytest

from crypto_trader.production import ProductionStateError
from crypto_trader.execution.binance_testnet.market_data import BinanceTestnetTickerQuoteSource
from crypto_trader.tests.r9_crossfeed_fixtures import FakeSocket, ticker, prepare, NOW

from crypto_trader.production.runtime import DurableVenueWritePort


class NoPostIntercept(Exception):
    """Raised instead of any venue submission; no transport is ever called."""


def test_late_venue_price_jump_must_not_reach_durable_submission_boundary(tmp_path, monkeypatch):
    journal, runtime, admission, host, transport, sockets, bootstrap = prepare(
        tmp_path, monkeypatch,
    )
    entered_submission = []
    def intercept(_port, _request):
        entered_submission.append(True)
        raise NoPostIntercept("intercepted BEFORE any send")
    monkeypatch.setattr(DurableVenueWritePort, "submit_durably", staticmethod(intercept))
    try:
        receipt = host.run().receipt
        intent = receipt.intent
        assert admission.offer(intent) == (True, "accepted")
        preparation = runtime.prepare_intent(
            intent, _admission_token=admission._runtime_token,
        )
        assert preparation.approved
        # Newer authoritative market observation arrives after the successful
        # Validity / Hard Risk preparation, with the same still-valid deadline.
        bootstrap._observation = replace(
            bootstrap._observation,
            price=Decimal("32000"),
            market_reference="later-venue-price-32000",
            market_event_ts=NOW,
            local_ingest_ts=NOW,
        )
        assert runtime._market_state.latest("BTCUSDT").price == Decimal("32000")
        # Crucially, advance the verified venue ticker itself, not only a cache.
        # The third source frame is consumed at the final submission boundary.
        later = []
        def moved_reader():
            socket = FakeSocket(ticker(E=int(NOW.timestamp()*1000), b="31999", a="32001"))
            later.append(socket)
            return BinanceTestnetTickerQuoteSource(
                symbol="BTCUSDT", clock=lambda: NOW,
                max_event_age=timedelta(seconds=5), max_spread_bps=Decimal("100"),
                connect=lambda *_: socket,
            )
        monkeypatch.setattr(bootstrap, "_ticker_factory", moved_reader)
        assert not any(r.method == "POST" for r in transport.requests)
        with pytest.raises(ProductionStateError, match="strict submit price moved"):
            runtime.submit_prepared(
                preparation, _admission_token=admission._runtime_token,
            )
        assert len(later) == 1 and later[0].closed
        # The production invariant we want: refuse before the durable write
        # boundary when a market jump invalidates the approval.
        assert entered_submission == [], (
            "BUG: changed market state reached venue submission after earlier "
            "price/risk evidence was approved"
        )
        assert not any(r.method == "POST" for r in transport.requests)
    finally:
        journal.close()

def _wire_late_frame(monkeypatch, bootstrap, *, bid="26999", ask="27001", fail=False):
    late_sockets = []
    def read():
        socket = FakeSocket(ticker(E=int(NOW.timestamp() * 1000), b=bid, a=ask), fails=fail)
        late_sockets.append(socket)
        return BinanceTestnetTickerQuoteSource(
            symbol="BTCUSDT", clock=lambda: NOW,
            max_event_age=timedelta(seconds=5), max_spread_bps=Decimal("100"),
            connect=lambda *_: socket,
        )
    monkeypatch.setattr(bootstrap, "_ticker_factory", read)
    return late_sockets


def test_fresh_unchanged_venue_quote_reaches_intercepted_boundary_only(tmp_path, monkeypatch):
    journal, runtime, admission, host, transport, sockets, bootstrap = prepare(
        tmp_path, monkeypatch,
    )
    calls = []
    def intercept(_port, request):
        calls.append(request)
        raise NoPostIntercept("blocked before any real venue send")
    monkeypatch.setattr(DurableVenueWritePort, "submit_durably", staticmethod(intercept))
    try:
        intent = host.run().receipt.intent
        assert admission.offer(intent) == (True, "accepted")
        preparation = runtime.prepare_intent(intent, _admission_token=admission._runtime_token)
        late = _wire_late_frame(monkeypatch, bootstrap)
        with pytest.raises(NoPostIntercept):
            runtime.submit_prepared(preparation, _admission_token=admission._runtime_token)
        assert len(calls) == 1
        assert len(late) == 1 and late[0].closed and late[0].read_count == 1
        assert all(r.method == "GET" for r in transport.requests)
    finally:
        journal.close()


def test_broken_fresh_venue_quote_fails_closed_before_durable_boundary(tmp_path, monkeypatch):
    journal, runtime, admission, host, transport, sockets, bootstrap = prepare(
        tmp_path, monkeypatch,
    )
    calls = []
    monkeypatch.setattr(
        DurableVenueWritePort, "submit_durably",
        staticmethod(lambda *_: calls.append(True)),
    )
    try:
        intent = host.run().receipt.intent
        assert admission.offer(intent) == (True, "accepted")
        preparation = runtime.prepare_intent(intent, _admission_token=admission._runtime_token)
        late = _wire_late_frame(monkeypatch, bootstrap, fail=True)
        with pytest.raises(ProductionStateError, match="strict submit market refresh failed"):
            runtime.submit_prepared(preparation, _admission_token=admission._runtime_token)
        assert len(late) == 1 and late[0].closed
        assert calls == []
        assert all(r.method == "GET" for r in transport.requests)
        assert preparation.intent_id not in runtime._issued_preparations
    finally:
        journal.close()

def test_quote_websocket_read_occurs_outside_submission_ownership_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager
    journal, runtime, admission, host, transport, sockets, bootstrap = prepare(
        tmp_path, monkeypatch,
    )
    store = runtime._control.ownership_store
    actual_exclusive = store.exclusive
    held = [False]
    seen_connect = []

    @contextmanager
    def monitored_exclusive(*, purpose):
        with actual_exclusive(purpose=purpose):
            held[0] = True
            try:
                yield
            finally:
                held[0] = False

    def final_reader():
        sock = FakeSocket(ticker(E=int(NOW.timestamp() * 1000), b="26999", a="27001"))
        def connect(*_args):
            seen_connect.append(True)
            assert not held[0], "market network read happened while holding the write lock"
            return sock
        return BinanceTestnetTickerQuoteSource(
            symbol="BTCUSDT", clock=lambda: NOW,
            max_event_age=timedelta(seconds=5), max_spread_bps=Decimal("100"),
            connect=connect,
        )

    def sentinel(_port, _request):
        assert held[0], "durable boundary must remain fenced under ownership lock"
        raise NoPostIntercept("no real submission")

    try:
        intent = host.run().receipt.intent
        assert admission.offer(intent) == (True, "accepted")
        preparation = runtime.prepare_intent(intent, _admission_token=admission._runtime_token)
        monkeypatch.setattr(store, "exclusive", monitored_exclusive)
        monkeypatch.setattr(bootstrap, "_ticker_factory", final_reader)
        monkeypatch.setattr(DurableVenueWritePort, "submit_durably", staticmethod(sentinel))
        with pytest.raises(NoPostIntercept):
            runtime.submit_prepared(preparation, _admission_token=admission._runtime_token)
        assert seen_connect == [True]
        assert not held[0]
        assert all(r.method == "GET" for r in transport.requests)
    finally:
        journal.close()

def test_old_replayed_venue_event_is_rejected_at_last_gate(tmp_path, monkeypatch):
    journal, runtime, admission, host, transport, sockets, bootstrap = prepare(
        tmp_path, monkeypatch,
    )
    attempts = []
    monkeypatch.setattr(
        DurableVenueWritePort, "submit_durably",
        staticmethod(lambda *_: attempts.append(True)),
    )
    try:
        intent = host.run().receipt.intent
        assert admission.offer(intent) == (True, "accepted")
        preparation = runtime.prepare_intent(intent, _admission_token=admission._runtime_token)
        late = []
        def replayed_reader():
            sock = FakeSocket(ticker(
                E=int(NOW.timestamp() * 1000) - 1000, b="26999", a="27001",
            ))
            late.append(sock)
            return BinanceTestnetTickerQuoteSource(
                symbol="BTCUSDT", clock=lambda: NOW,
                max_event_age=timedelta(seconds=5), max_spread_bps=Decimal("100"),
                connect=lambda *_: sock,
            )
        monkeypatch.setattr(bootstrap, "_ticker_factory", replayed_reader)
        with pytest.raises(ProductionStateError, match="strict submit price event order"):
            runtime.submit_prepared(preparation, _admission_token=admission._runtime_token)
        assert attempts == [] and len(late) == 1 and late[0].closed
        assert all(r.method == "GET" for r in transport.requests)
    finally:
        journal.close()


def test_market_state_and_venue_quote_conflict_fails_closed(tmp_path, monkeypatch):
    journal, runtime, admission, host, transport, sockets, bootstrap = prepare(
        tmp_path, monkeypatch,
    )
    attempts = []
    monkeypatch.setattr(
        DurableVenueWritePort, "submit_durably",
        staticmethod(lambda *_: attempts.append(True)),
    )
    try:
        intent = host.run().receipt.intent
        assert admission.offer(intent) == (True, "accepted")
        preparation = runtime.prepare_intent(intent, _admission_token=admission._runtime_token)
        bootstrap._observation = replace(
            bootstrap._observation, price=Decimal("32000"),
            market_reference="conflicting-late-market-state",
            market_event_ts=NOW, local_ingest_ts=NOW,
        )
        late = _wire_late_frame(monkeypatch, bootstrap)  # Venue still quotes old price.
        with pytest.raises(ProductionStateError, match="strict submit live market-state moved"):
            runtime.submit_prepared(preparation, _admission_token=admission._runtime_token)
        assert attempts == [] and len(late) == 1 and late[0].closed
        assert all(r.method == "GET" for r in transport.requests)
    finally:
        journal.close()

def test_intent_expiring_during_new_ticker_read_refuses_before_send(tmp_path, monkeypatch):
    journal, runtime, admission, host, transport, sockets, bootstrap = prepare(
        tmp_path, monkeypatch,
    )
    calls = []
    monkeypatch.setattr(
        DurableVenueWritePort, "submit_durably",
        staticmethod(lambda *_: calls.append(True)),
    )
    try:
        intent = host.run().receipt.intent
        assert admission.offer(intent) == (True, "accepted")
        preparation = runtime.prepare_intent(
            intent, _admission_token=admission._runtime_token,
        )
        latest = []
        def reader():
            socket = FakeSocket(ticker(
                E=int(NOW.timestamp() * 1000), b="26999", a="27001",
            ))
            latest.append(socket)
            def connect(*_args):
                # Simulate waiting for venue data long enough to cross the
                # intent's effective deadline, without sleeping or sending.
                runtime._clock = lambda: NOW + timedelta(seconds=5)
                return socket
            return BinanceTestnetTickerQuoteSource(
                symbol="BTCUSDT", clock=lambda: NOW,
                max_event_age=timedelta(seconds=5),
                max_spread_bps=Decimal("100"), connect=connect,
            )
        monkeypatch.setattr(bootstrap, "_ticker_factory", reader)
        with pytest.raises(ProductionStateError, match="expired before submission"):
            runtime.submit_prepared(
                preparation, _admission_token=admission._runtime_token,
            )
        assert calls == [] and len(latest) == 1 and latest[0].closed
        assert all(r.method == "GET" for r in transport.requests)
    finally:
        journal.close()
