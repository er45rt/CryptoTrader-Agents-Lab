"""D40 phase 1, item 2: portfolio prices from Binance PUBLIC klines (read-only), never a guessed number. Offline:
the klines fetcher is replaced by a recorder, so no test touches the network."""
from __future__ import annotations

import socket
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from crypto_trader.strategies.api import Bar, StrategyError
from crypto_trader.strategies.ohlcv import KLINES_URL, fetch_public_klines
from crypto_trader.strategies.ui import portfolio, prices, server

NOW = datetime(2026, 3, 10, 12, 0, 30, tzinfo=timezone.utc)


def _bar(t, close, tf):
    c = Decimal(close)
    return Bar(open_time=t, open=c, high=c, low=c, close=c, volume=Decimal(1), timeframe=tf)


class Recorder:
    def __init__(self, table=None, fail=None):
        self.calls, self.table, self.fail = [], table or {}, fail or {}

    def __call__(self, symbol, tf, *, start, now, timeout, end=None):
        self.calls.append((symbol, tf, start, now))
        if symbol in self.fail:
            raise self.fail[symbol]
        if tf == "1m":
            return [_bar(NOW.replace(second=0) - timedelta(minutes=1), self.table[symbol], "1m")]
        days = []
        d = start
        while d.date() < now.date():
            days.append(_bar(d, self.table[symbol], "1d"))
            d += timedelta(days=1)
        return days


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network used in an offline test")
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_latest_price_is_the_last_closed_minute_with_source_and_time(tmp_path):
    rec = Recorder({"BTCUSDT": "70123.5"})
    p = prices.BinancePrices(tmp_path, fetch=rec, now=lambda: NOW)
    res = p.latest(["BTC"])["BTC"]
    assert res == {"price": "70123.5", "source": prices.SOURCE, "at": "2026-03-10T12:00:00+00:00", "url": KLINES_URL}
    assert rec.calls == [("BTCUSDT", "1m", NOW - timedelta(minutes=5), NOW)]
    p.latest(["BTC"])                                   # cached for LATEST_TTL_S
    assert len(rec.calls) == 1


def test_a_missing_pair_or_network_error_is_reported_never_guessed(tmp_path):
    rec = Recorder({}, fail={"XYZUSDT": RuntimeError("HTTP Error 400: Bad Request"),
                             "ABCUSDT": OSError("unreachable")})
    res = prices.BinancePrices(tmp_path, fetch=rec, now=lambda: NOW).latest(["XYZ", "ABC"])
    assert res["XYZ"] == {"price": None, "reason": "no Binance USDT pair", "source": prices.SOURCE}
    assert res["ABC"]["price"] is None and res["ABC"]["reason"] == "price unavailable: OSError"


def test_daily_closes_are_cached_and_only_the_missing_tail_is_fetched(tmp_path):
    rec = Recorder({"ETHUSDT": "2500"})
    clock = {"now": NOW}
    p = prices.BinancePrices(tmp_path, fetch=rec, now=lambda: clock["now"])
    res = p.daily("ETH", date(2026, 3, 1))
    assert sorted(res["closes"]) == [f"2026-03-{d:02d}" for d in range(1, 10)] and res["reason"] is None
    clock["now"] = NOW + timedelta(days=2)
    res = p.daily("ETH", date(2026, 3, 1))
    assert rec.calls[-1][2] == datetime(2026, 3, 10, tzinfo=timezone.utc)      # tail only
    assert max(res["closes"]) == "2026-03-11" and (tmp_path / "daily_ETHUSDT.json").exists()


def test_summary_uses_the_price_service_and_marks_unpriced_assets(tmp_path):
    rec = Recorder({"BTCUSDT": "70000"}, fail={"XYZUSDT": RuntimeError("HTTP Error 400")})
    p = prices.BinancePrices(tmp_path / "px", fetch=rec, now=lambda: NOW)
    lg = portfolio.Ledger(tmp_path, prices=p.latest)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "0.1", "price": "60000"})
    lg.add({"type": "buy", "asset": "XYZ", "at": "2026-01-01", "qty": "5", "price": "2"})
    s = lg.summary()
    btc = next(a for a in s["assets"] if a["asset"] == "BTC")
    xyz = next(a for a in s["assets"] if a["asset"] == "XYZ")
    assert btc["value"] == "7000" and btc["price_source"] == prices.SOURCE and btc["price_at"]
    assert xyz["value"] is None and xyz["price_missing"] == "no Binance USDT pair"
    assert s["totals"]["value"] == "7000" and s["totals"]["value_complete"] is False


def test_history_values_each_closed_day_and_flags_days_without_a_close(tmp_path):
    lg = portfolio.Ledger(tmp_path)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-03-01T10:00:00+00:00", "qty": "1", "price": "60000"})
    lg.add({"type": "buy", "asset": "XYZ", "at": "2026-03-03T10:00:00+00:00", "qty": "1", "price": "1"})

    def daily(asset, since):
        if asset == "XYZ":
            return {"closes": {}, "reason": "no Binance USDT pair"}
        d = datetime.now(timezone.utc).date() - timedelta(days=1)
        return {"closes": {(since + timedelta(days=i)).isoformat(): "61000" for i in range((d - since).days + 1)},
                "reason": None}

    h = lg.history(daily)
    first, third = h["points"][0], h["points"][2]
    assert first == {"d": "2026-03-01", "value": "61000", "basis": "60000", "complete": True}
    assert third["d"] == "2026-03-03" and third["value"] == "61000" and third["basis"] == "60001"
    assert third["complete"] is False and h["missing"] == {"XYZ": "no Binance USDT pair"}


def test_the_server_prices_come_only_from_the_public_klines_fetcher(tmp_path):
    srv = server.make_server(tmp_path, 0)
    try:
        assert server.Handler.prices._fetch is fetch_public_klines
        assert server.Handler.ledger._prices == server.Handler.prices.latest
        assert KLINES_URL == "https://api.binance.com/api/v3/klines"
    finally:
        srv.server_close()


def test_bad_asset_symbols_never_reach_the_price_url(tmp_path):
    with pytest.raises(StrategyError):
        portfolio.parse_tx({"type": "buy", "asset": "BTC&x=1", "at": "2026-01-01", "qty": "1", "price": "1"})
    with pytest.raises(StrategyError):
        portfolio.Ledger(tmp_path).asset_view("BTC&symbol=ETH", lambda a, s: pytest.fail("fetched"), prices.SOURCE)


def test_asset_view_has_its_closes_and_only_its_transactions(tmp_path):
    lg = portfolio.Ledger(tmp_path)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-03-01", "qty": "1", "price": "60000"})
    lg.add({"type": "buy", "asset": "ETH", "at": "2026-03-02", "qty": "1", "price": "2000"})
    seen = []
    v = lg.asset_view("btc", lambda a, since: seen.append((a, since)) or {"closes": {"2026-03-01": "61000"}, "reason": None},
                      prices.SOURCE)
    assert seen == [("BTC", date(2026, 1, 30))] and v["closes"] == {"2026-03-01": "61000"}
    assert [t["asset"] for t in v["transactions"]] == ["BTC"] and v["source"] == prices.SOURCE
