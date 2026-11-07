"""Owner decision D44, K4: Binance trade history goes to a review file first; nothing reaches the ledger until the owner
imports selected trades marked ready; a trade can never be imported twice; duplicates of manual rows and fees in a
third asset are held back."""
from __future__ import annotations

import pytest

from crypto_trader.mainnet.errors import MainnetEndpointError
from crypto_trader.mainnet.permissions import parse_api_restrictions
from crypto_trader.strategies.api import StrategyError
from crypto_trader.strategies.ui import accounts, portfolio, vault
from crypto_trader.tests.test_binance_link_accounts import KEY, READ_ONLY, SECRET, _vault

T0 = 1_767_225_600_000          # 2026-01-01T00:00:00Z in ms


def _t(i, buyer, qty, price, fee, fee_asset, dt=0):
    return {"id": i, "orderId": 900 + i, "price": price, "qty": qty, "quoteQty": str(float(qty) * float(price)),
            "commission": fee, "commissionAsset": fee_asset, "time": T0 + dt * 86_400_000, "isBuyer": buyer}


class Client:
    def __init__(self, trades, log):
        self.trades, self.log = trades, log

    def api_restrictions(self):
        self.log.append("restrictions")
        return parse_api_restrictions(READ_ONLY)

    def verify_read_only_permissions(self):
        return self.api_restrictions()

    def my_trades(self, symbol, *, limit, from_id=None):
        self.log.append((symbol, from_id))
        if symbol not in self.trades:
            raise MainnetEndpointError("the mainnet read-only request was rejected (HTTP 400, code -1121): Invalid symbol.")
        rows = [t for t in self.trades[symbol] if t["id"] >= (from_id or 0)]
        return tuple(rows[:limit])


def _link(tmp_path, trades, log):
    link = accounts.BinanceLink(tmp_path, vault=_vault(tmp_path), client_factory=lambda creds: Client(trades, log))
    link.connect(KEY, SECRET, pin_set=True)
    return link


def test_sync_pages_the_whole_history_into_the_review_file_only(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "PAGE", 2)
    log = []
    trades = {"BTCUSDT": [_t(1, True, "0.01", "90000", "0.9", "USDT"), _t(2, True, "0.01", "91000", "0.00001", "BTC", 1),
                          _t(3, False, "0.005", "95000", "0.47", "USDT", 2)]}
    link = _link(tmp_path, trades, log)
    lg = portfolio.Ledger(tmp_path)
    log.clear()
    res = link.sync_trades(["BTC", "NOPAIR", "USDT"])
    assert res["trades"] == 3 and res["assets"] == ["BTC", "NOPAIR"]
    assert log[0] == "restrictions" and ("BTCUSDT", 0) in log and ("BTCUSDT", 3) in log      # next page starts after the last id (1, 2 -> 3)
    assert lg.transactions() == []                                  # nothing reached the ledger
    again = link.sync_trades(["BTC"])
    assert again["trades"] == 3                                     # merged by trade id, no growth


def test_preview_classifies_and_import_takes_only_selected_ready_trades_once(tmp_path):
    log = []
    trades = {"BTCUSDT": [_t(1, True, "0.01", "90000", "0.9", "USDT"), _t(2, True, "0.02", "91000", "0.00002", "BTC", 1),
                          _t(3, True, "0.01", "92000", "0.0005", "BNB", 2), _t(4, False, "0.01", "95000", "0.95", "USDT", 3)]}
    link = _link(tmp_path, trades, log)
    lg = portfolio.Ledger(tmp_path)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01T00:00:00+00:00", "qty": "0.010", "price": "90000",
            "fee": "0.9"})                                          # the owner typed trade 1 by hand already
    link.sync_trades(["BTC"])
    p = link.preview(lg)
    status = {r["trade"]["ext_id"]: r["status"] for r in p["rows"]}
    assert status == {"binance:BTCUSDT:1": "possible_duplicate", "binance:BTCUSDT:2": "ready",
                      "binance:BTCUSDT:3": "blocked", "binance:BTCUSDT:4": "ready"}
    assert p["counts"] == {"ready": 2, "imported": 0, "possible_duplicate": 1, "blocked": 1} and lg.transactions()[0]["source"] == "manual"
    for bad in (["binance:BTCUSDT:1"], ["binance:BTCUSDT:3"], ["binance:BTCUSDT:9"], []):
        with pytest.raises(StrategyError):
            link.import_selected(lg, bad)
    assert len(lg.transactions()) == 1                              # refused selections wrote nothing
    assert link.import_selected(lg, ["binance:BTCUSDT:2", "binance:BTCUSDT:4"]) == {"imported": 2}
    rows = {t.get("ext_id"): t for t in lg.transactions()}
    assert rows["binance:BTCUSDT:2"]["source"] == "binance" and rows["binance:BTCUSDT:2"]["fee_asset"] == "BTC"
    assert {r["trade"]["ext_id"]: r["status"] for r in link.preview(lg)["rows"]}["binance:BTCUSDT:2"] == "imported"
    with pytest.raises(StrategyError):
        link.import_selected(lg, ["binance:BTCUSDT:2"])               # never twice
    st = portfolio.compute(lg.transactions())["BTC"]
    assert str(st["qty"]) == "0.01998"                              # 0.01 + (0.02 - 0.00002 fee) - 0.01


def test_an_import_that_would_oversell_is_refused_whole(tmp_path):
    log = []
    trades = {"ETHUSDT": [_t(7, False, "1", "3000", "3", "USDT")]}     # a sale with no recorded purchase
    link = _link(tmp_path, trades, log)
    lg = portfolio.Ledger(tmp_path)
    link.sync_trades(["ETH"])
    assert link.preview(lg)["replay_error"]
    with pytest.raises(StrategyError):
        link.import_selected(lg, ["binance:ETHUSDT:7"])
    assert lg.transactions() == []


def test_ext_ids_are_strict_and_only_for_venue_trades():
    with pytest.raises(StrategyError):
        portfolio.parse_tx({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "1", "price": "1", "ext_id": "x:1"})
    with pytest.raises(StrategyError):
        portfolio.parse_tx({"type": "fee", "asset": "BTC", "at": "2026-01-01", "fee": "1", "ext_id": "binance:BTCUSDT:1"})
    assert portfolio.parse_tx({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "1", "price": "1",
                               "ext_id": "binance:BTCUSDT:1"})["ext_id"] == "binance:BTCUSDT:1"


def test_the_read_only_client_pages_my_trades_by_from_id_on_the_same_get_path():
    from crypto_trader.execution.binance_testnet import ScriptedTransport
    from crypto_trader.mainnet.errors import MainnetError
    from crypto_trader.tests import mainnet_fixtures as mf

    tr = ScriptedTransport(responses=[mf.response(200, [{"id": 5}]), mf.response(200, [])])
    client = mf.client(tr)
    assert client.my_trades("BTCUSDT", limit=1000, from_id=5) == ({"id": 5},)
    assert client.my_trades("BTCUSDT", limit=10) == ()
    first, second = tr.requests
    assert first.method == second.method == "GET" and first.url.endswith("/api/v3/myTrades") and not first.is_write
    assert "fromId=5" in first.query and "limit=1000" in first.query and "fromId" not in second.query
    for bad in (-1, True, "5"):
        with pytest.raises(MainnetError):
            client.my_trades("BTCUSDT", from_id=bad)
