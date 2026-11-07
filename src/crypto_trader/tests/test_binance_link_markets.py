"""Sol D44 review B4: syncing "the available trade history" must not stop at <asset>USDT for currently held assets. Every
market whose base is a known coin (any quote) is read; coins sold completely can be named by the owner and stay known
from earlier syncs. Trades against another quote are shown but never imported and leave both coins' cost unavailable."""
from __future__ import annotations

import http.client
import json
import threading

from crypto_trader.mainnet.permissions import parse_api_restrictions
from crypto_trader.strategies.ui import accounts, portfolio, server
from crypto_trader.tests.test_binance_link_accounts import KEY, READ_ONLY, SECRET, _vault
from crypto_trader.tests.test_binance_link_trades import T0

MARKETS = [("BTCUSDT", "BTC", "USDT"), ("BTCFDUSD", "BTC", "FDUSD"), ("ETHBTC", "ETH", "BTC"), ("ETHUSDT", "ETH", "USDT"),
           ("SOLUSDT", "SOL", "USDT"), ("DOGEUSDT", "DOGE", "USDT"), ("BNBBTC", "BNB", "BTC")]


def _t(i, buyer, qty, price, day=0):
    return {"id": i, "orderId": 900 + i, "price": price, "qty": qty, "quoteQty": "0", "commission": "0",
            "commissionAsset": "USDT", "time": T0 + day * 86_400_000, "isBuyer": buyer}


class Fake:
    def __init__(self, balances, trades, log):
        self.balances, self.trades, self.log = balances, trades, log

    def api_restrictions(self):
        return parse_api_restrictions(READ_ONLY)

    def verify_read_only_permissions(self):
        return self.api_restrictions()

    def account_snapshot(self):
        return {"balances": [{"asset": a, "free": q, "locked": "0"} for a, q in self.balances.items()]}

    def market_symbols(self):
        return tuple({"symbol": s, "base": b, "quote": q} for s, b, q in MARKETS)

    def my_trades(self, symbol, *, limit, from_id=None):
        self.log.append(symbol)
        return tuple(t for t in self.trades.get(symbol, []) if t["id"] >= (from_id or 0))[:limit]


def _link(tmp_path, balances, trades, log):
    link = accounts.BinanceLink(tmp_path, vault=_vault(tmp_path), prices=lambda a: {},
                                client_factory=lambda c: Fake(balances, trades, log))
    link.connect(KEY, SECRET, pin_set=True)
    return link


def test_every_market_of_a_coin_is_read_not_only_usdt(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "PAUSE_S", 0)
    log = []
    trades = {"BTCUSDT": [_t(1, True, "1", "100")], "BTCFDUSD": [_t(2, True, "1", "101")], "ETHBTC": [_t(3, True, "10", "0.05")]}
    link = _link(tmp_path, {"BTC": "1.5", "ETH": "10"}, trades, log)
    res = link.sync_trades(["BTC", "ETH"])
    assert sorted(log) == ["BTCFDUSD", "BTCUSDT", "ETHBTC", "ETHUSDT"] and res["markets"] == 4 and res["trades"] == 3
    lg = portfolio.Ledger(tmp_path)
    rows = {r["trade"]["symbol"]: r for r in link.preview(lg)["rows"]}
    assert rows["BTCUSDT"]["status"] == "ready"
    assert rows["BTCFDUSD"]["status"] == rows["ETHBTC"]["status"] == "blocked"
    assert rows["BTCFDUSD"]["reason"] == rows["ETHBTC"]["reason"] == "quote_not_usdt"
    assert rows["ETHBTC"]["trade"]["quote"] == "BTC"
    assert accounts._uncovered(link._staged()["trades"], []) >= {"BTC", "ETH", "FDUSD"}


def _post(port, path, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("POST", path, json.dumps(body), {"Host": "127.0.0.1", "X-CT-UI": "1", "Content-Type": "application/json"})
    r = c.getresponse()
    doc = json.loads(r.read() or b"{}")
    c.close()
    return r.status, doc


def test_a_coin_sold_completely_is_synced_when_named_and_stays_known(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "PAUSE_S", 0)
    log = []
    trades = {"SOLUSDT": [_t(1, True, "3", "100"), _t(2, False, "3", "150", 5)], "BTCUSDT": [_t(3, True, "1", "100")]}
    srv = server.make_server(tmp_path, 0)
    server.Handler.link = _link(tmp_path, {"BTC": "1"}, trades, log)      # SOL is gone: zero balance, not in the ledger
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        st, doc = _post(port, "/api/accounts/binance/sync", {})
        assert st == 200 and doc["assets"] == ["BTC"] and "SOLUSDT" not in log
        st, doc = _post(port, "/api/accounts/binance/sync", {"extra": ["sol", "x" * 40, 7]})
        assert st == 200 and doc["assets"] == ["BTC", "SOL"] and "SOLUSDT" in log
        log.clear()
        st, doc = _post(port, "/api/accounts/binance/sync", {})               # stays known from the earlier sync
        assert st == 200 and doc["assets"] == ["BTC", "SOL"] and "SOLUSDT" in log and doc["trades"] == 3
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_client_lists_markets_with_the_same_public_get():
    from crypto_trader.execution.binance_testnet import ScriptedTransport
    from crypto_trader.tests import mainnet_fixtures as mf

    body = {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT"},
                        {"symbol": "ETHBTC", "baseAsset": "ETH", "quoteAsset": "BTC"}, {"symbol": "bad"}, "junk"]}
    tr = ScriptedTransport(responses=[mf.response(200, body)])
    client = mf.client(tr)
    assert client.market_symbols() == ({"symbol": "BTCUSDT", "base": "BTC", "quote": "USDT"},
                                       {"symbol": "ETHBTC", "base": "ETH", "quote": "BTC"})
    (req,) = tr.requests
    assert req.method == "GET" and req.url.endswith("/api/v3/exchangeInfo") and not req.is_write and req.query == ""


def test_the_page_lets_the_owner_name_coins_sold_completely():
    from pathlib import Path

    js = (Path(server.__file__).parent / "static" / "accounts.js").read_text(encoding="utf-8")
    assert 'id="tr-extra"' in js and '"/api/accounts/binance/sync", { extra:' in js and "quote_not_usdt" in js
    assert 't.quote && t.quote !== "USDT"' in js                     # a non-USDT price is never shown as if in USDT
    assert "600000); accBalances(); }" in js                          # balances re-read after a sync (history states change)
