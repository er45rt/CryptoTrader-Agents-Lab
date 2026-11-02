"""Independent late-fence cross-feed disagreement tests; zero real venue sends."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from crypto_trader.execution import DurableVenueWritePort
from crypto_trader.execution.binance_testnet.market_data import BinanceTestnetTickerQuoteSource
from crypto_trader.production.errors import ProductionStateError
from crypto_trader.tests.test_adversarial_submit_price_drift_red import (
    NoPostIntercept, _wire_late_frame,
)
from crypto_trader.tests.r9_crossfeed_fixtures import prepare, NOW, FakeSocket, ticker


def _prepared(tmp_path, monkeypatch):
    journal, runtime, admission, host, transport, sockets, bootstrap = prepare(
        tmp_path, monkeypatch,
    )
    intent = host.run().receipt.intent
    assert admission.offer(intent) == (True, "accepted")
    preparation = runtime.prepare_intent(intent, _admission_token=admission._runtime_token)
    assert preparation.approved
    intercepts = []
    def intercepted(_port, _request):
        intercepts.append(True)
        raise NoPostIntercept("intercepted; never POST")
    monkeypatch.setattr(DurableVenueWritePort, "submit_durably", staticmethod(intercepted))
    return journal, runtime, admission, preparation, transport, bootstrap, intercepts


def test_local_and_venue_prices_diverge_beyond_cap_even_if_both_near_decision(
    tmp_path, monkeypatch,
):
    journal, runtime, admission, prep, transport, bootstrap, intercepts = _prepared(
        tmp_path, monkeypatch,
    )
    try:
        assert prep.reference_price == Decimal("27001.00")
        # Both prices are ~44bps from the decision, on opposite sides.
        bootstrap._observation = replace(
            bootstrap._observation,
            price=Decimal("27120.00"), market_event_ts=NOW,
            local_ingest_ts=NOW, market_reference="local-moved-up",
        )
        late = _wire_late_frame(monkeypatch, bootstrap, bid="26879", ask="26881")
        with pytest.raises(ProductionStateError, match="diverg|conflict|mismatch"):
            runtime.submit_prepared(prep, _admission_token=admission._runtime_token)
        assert intercepts == []
        assert len(late) == 1 and late[0].closed
        assert all(request.method == "GET" for request in transport.requests)
    finally:
        journal.close()


def test_newer_local_market_event_cannot_be_approved_using_older_venue_quote(
    tmp_path, monkeypatch,
):
    journal, runtime, admission, prep, transport, bootstrap, intercepts = _prepared(
        tmp_path, monkeypatch,
    )
    try:
        bootstrap._observation = replace(
            bootstrap._observation,
            price=Decimal("27000"), market_event_ts=NOW + timedelta(milliseconds=300),
            local_ingest_ts=NOW + timedelta(milliseconds=500), market_reference="local-newer-than-final-mark",
        )
        sock_list = []
        def final_reader():
            sock = FakeSocket(ticker(
                E=int(NOW.timestamp()*1000)+100, b="26999", a="27001",
            ))
            sock_list.append(sock)
            return BinanceTestnetTickerQuoteSource(
                symbol="BTCUSDT", clock=lambda: NOW + timedelta(milliseconds=500),
                max_event_age=timedelta(seconds=5),
                max_spread_bps=Decimal("100"),
                connect=lambda *_: sock,
            )
        monkeypatch.setattr(bootstrap, "_ticker_factory", final_reader)
        runtime._clock = lambda: NOW + timedelta(milliseconds=500)
        with pytest.raises(ProductionStateError, match="newer.*venue|market-state.*order"):
            runtime.submit_prepared(prep, _admission_token=admission._runtime_token)
        assert intercepts == [] and len(sock_list) == 1 and sock_list[0].closed
        assert all(request.method == "GET" for request in transport.requests)
    finally:
        journal.close()
