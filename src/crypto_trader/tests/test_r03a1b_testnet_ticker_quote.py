"""Offline, no-submit acceptance probes for the public Spot Testnet ticker source."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from crypto_trader.execution.binance_testnet.market_data import (
    BinanceTestnetTickerQuoteSource, TestnetTickerQuoteError as TickerError,
    filters_from_testnet_rules,
)
from crypto_trader.execution.binance_testnet.models import TestnetSymbolRules as VenueSymbolRules
from crypto_trader.validity.market_state import ReplayMarketStateProvider
from crypto_trader.tests.production_fixtures import BASE

NOW = BASE + timedelta(seconds=2)


class FakeSocket:
    def __init__(self, frame, *, fails=False):
        self.frame = frame
        self.fails = fails
        self.closed = False
        self.read_count = 0

    def recv(self):
        self.read_count += 1
        if self.fails:
            raise OSError("injected connection drop")
        return self.frame

    def close(self):
        self.closed = True


def ticker(**changes):
    payload = {
        "e": "24hrTicker", "E": int(NOW.timestamp() * 1000),
        "s": "BTCUSDT", "b": "26999.00", "B": "10",
        "a": "27000.00", "A": "11",
    }
    payload.update(changes)
    return json.dumps(payload)


def source(frame, *, now=NOW, connector=None, max_age=timedelta(seconds=5)):
    socket = FakeSocket(frame)
    calls = []

    def connect(url, timeout):
        calls.append((url, timeout))
        return socket

    result = BinanceTestnetTickerQuoteSource(
        symbol="BTCUSDT", clock=lambda: now, max_event_age=max_age,
        max_spread_bps=Decimal("100"), connect=connector or connect,
    )
    return result, socket, calls


def test_ticker_uses_actual_venue_E_and_TLS_pinned_testnet_stream():
    receiver, sock, calls = source(ticker())
    quote = receiver.read_once()
    assert calls == [("wss://stream.testnet.binance.vision/ws/btcusdt@ticker", 5.0)]
    assert quote.venue == "binance-spot-testnet"
    assert quote.symbol == "BTCUSDT"
    assert quote.event_ts == NOW
    assert quote.received_at == NOW
    assert (quote.best_bid, quote.best_ask) == (Decimal("26999.00"), Decimal("27000.00"))
    assert quote.reference_id.startswith("binance-spot-testnet:ticker:BTCUSDT:")
    assert sock.read_count == 1 and sock.closed


def test_one_ticker_frame_binds_crew_quote_to_runtime_market_observation():
    receiver, socket, calls = source(ticker())
    quote, observation = receiver.read_once_bound()
    validity_feed = ReplayMarketStateProvider([observation])

    assert validity_feed.latest("BTCUSDT") is observation
    assert validity_feed.latest("ETHUSDT") is None
    assert observation.symbol == quote.symbol
    assert observation.market_event_ts == quote.event_ts
    assert observation.local_ingest_ts == quote.received_at
    assert observation.market_reference == quote.reference_id
    assert observation.price == (quote.best_bid + quote.best_ask) / Decimal("2")
    assert observation.price == Decimal("26999.50")
    assert socket.closed and socket.read_count == 1 and len(calls) == 1
    with pytest.raises(TickerError, match="one-shot"):
        receiver.read_once_bound()
    with pytest.raises(TickerError, match="one-shot"):
        receiver.read_once()


def test_bound_ticker_refuses_mismatched_symbol_without_validity_fallback():
    receiver, socket, _calls = source(ticker(s="ETHUSDT"))
    with pytest.raises(TickerError, match="symbol"):
        receiver.read_once_bound()
    assert socket.closed and socket.read_count == 1
    with pytest.raises(TickerError, match="one-shot"):
        receiver.read_once_bound()


@pytest.mark.parametrize("frame, match", [
    (ticker(e="bookTicker"), "24hrTicker"),
    (ticker(E=None), "timestamp"),
    (ticker(E=True), "timestamp"),
    (ticker(s="ETHUSDT"), "symbol"),
    (ticker(b="0"), "best bid"),
    (ticker(a="-4"), "best ask"),
    (ticker(B="0"), "bid size"),
    (ticker(A="1e9"), "ask size"),
    (ticker(b="28000"), "spread"),
    (ticker(a="29000"), "spread"),
    ("not json", "failed closed"),
    ('{"e":"24hrTicker","e":"24hrTicker"}', "duplicate"),
    pytest.param(" " * 16385, "bounded", id="oversize"),
])
def test_ticker_invalid_frames_fail_closed_and_close_socket(frame, match):
    receiver, sock, _ = source(frame)
    with pytest.raises(TickerError, match=match):
        receiver.read_once()
    assert sock.closed


@pytest.mark.parametrize("delta_ms", [-6000, 1, 20000])
def test_ticker_stale_and_future_events_refused(delta_ms):
    event = int(NOW.timestamp() * 1000) + delta_ms
    receiver, sock, _ = source(ticker(E=event))
    with pytest.raises(TickerError, match="future-dated or stale"):
        receiver.read_once()
    assert sock.closed


def test_risk_mark_keeps_venue_event_time_and_frame_identity():
    receiver, socket, calls = source(ticker(), now=NOW + timedelta(milliseconds=500))
    mark = receiver.read_once_risk_mark()
    assert mark.symbol == "BTCUSDT"
    assert mark.price == Decimal("26999.50")
    assert mark.observed_at == NOW  # Venue E, never the 500ms-later receive timestamp.
    assert mark.reference.startswith("binance-spot-testnet:ticker:BTCUSDT:")
    assert socket.closed and socket.read_count == 1 and len(calls) == 1
    with pytest.raises(TickerError, match="one-shot"):
        receiver.read_once_risk_mark()


def test_ticker_one_shot_prevents_ambiguous_retry_or_replay():
    receiver, sock, calls = source(ticker())
    receiver.read_once()
    with pytest.raises(TickerError, match="one-shot"):
        receiver.read_once()
    assert len(calls) == 1 and sock.read_count == 1


def test_ticker_transport_errors_fail_closed_without_reconnect():
    socket = FakeSocket(ticker(), fails=True)
    calls = []

    def connect(url, timeout):
        calls.append((url, timeout))
        return socket

    receiver, _ignored, _calls = source(ticker(), connector=connect)
    with pytest.raises(TickerError, match="read failed closed"):
        receiver.read_once()
    assert socket.closed
    assert len(calls) == 1


def test_ticker_refuses_unbounded_timeout_age_or_spread():
    for invalid in (
        dict(timeout_seconds=60),
        dict(max_event_age=timedelta(minutes=2)),
        dict(max_spread_bps=Decimal("NaN")),
    ):
        values = dict(symbol="BTCUSDT", clock=lambda: NOW,
                      max_event_age=timedelta(seconds=5),
                      max_spread_bps=Decimal("100"), connect=lambda *_: FakeSocket(ticker()))
        values.update(invalid)
        with pytest.raises(TickerError):
            BinanceTestnetTickerQuoteSource(**values)


def test_host_filters_map_from_strict_exchange_info_without_llm_values():
    rules = VenueSymbolRules(
        symbol="BTCUSDT", status="TRADING",
        base_asset="BTC", quote_asset="USDT",
        tick_size=Decimal("0.01"), step_size=Decimal("0.0001"),
        min_quantity=Decimal("0.0001"), max_quantity=Decimal("100"),
        min_notional=Decimal("5"),
    )
    mapped = filters_from_testnet_rules(rules)
    assert mapped.venue == "binance-spot-testnet"
    assert mapped.symbol == "BTCUSDT"
    assert mapped.tick_size == rules.tick_size
    assert mapped.quantity_step == rules.step_size
    assert mapped.minimum_notional_quote == rules.min_notional
    with pytest.raises(TickerError, match="active"):
        filters_from_testnet_rules(object())


# Item 6: formerly deferred host-dependent venue-E policy regression
from crypto_trader.crew_intent_policy import ApprovedIntentConfig, HostDecisionTimes, make_policy
from crypto_trader.llm.schemas import TradingPlan
from crypto_trader.tests.test_r03a1b_crew_gateway import plan
from crypto_trader.tests.test_r03a1b_no_submit_e2e import host


def test_real_host_policy_accepts_quote_reference_without_any_venue_post(host):
    receiver, socket, calls = source(ticker())
    quote = receiver.read_once()
    rules = VenueSymbolRules(
        symbol="BTCUSDT", status="TRADING",
        base_asset="BTC", quote_asset="USDT",
        tick_size=Decimal("0.01"), step_size=Decimal("0.0001"),
        min_quantity=Decimal("0.0001"), max_quantity=Decimal("100"),
        min_notional=Decimal("5"),
    )
    limits = host[2]._limits
    policy = make_policy(
        config=ApprovedIntentConfig(
            venue="binance-spot-testnet", symbol="BTCUSDT", strategy_id="offline.e2e",
            approved_notional_quote=Decimal("270"),
            max_snapshot_age=timedelta(seconds=5),
            intent_valid_for=timedelta(seconds=3),
        ),
        market_quote=quote,
        host_times=HostDecisionTimes(started_at=NOW, completed_at=NOW),
        instrument_filters=filters_from_testnet_rules(rules),
        risk_limits=limits,
    )
    raw_plan = plan()
    parsed_plan = json.loads(raw_plan[raw_plan.index("{"):raw_plan.rindex("}") + 1])
    intent = policy(TradingPlan.model_validate(parsed_plan))
    assert intent.market_reference == quote.reference_id
    assert intent.market_event_ts == quote.event_ts
    assert intent.limit_price == Decimal("27000.00")
    assert socket.closed and len(calls) == 1
    assert host[3].depth() == 0
    assert not any(req.method == "POST" for req in host[4].requests)
