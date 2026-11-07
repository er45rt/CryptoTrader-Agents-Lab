"""Sol D44 review B2: a matching quantity alone must not make cost and profit "available". Full history buy 1@100,
buy 1@200, sell 1@300 and a ledger holding only buy 1@200 both leave 1 - the cost would be 200 instead of the real
basis. Cost appears only when the latest sync covered the asset and every synced Binance trade of it is in the ledger
(imported, or one-to-one with a manual row)."""
from __future__ import annotations

from decimal import Decimal

from crypto_trader.mainnet.permissions import parse_api_restrictions
from crypto_trader.strategies.ui import accounts, portfolio
from crypto_trader.tests.test_binance_link_accounts import KEY, READ_ONLY, SECRET, _vault
from crypto_trader.tests.test_binance_link_trades import T0

DAY = 86_400_000


def _t(i, buyer, qty, price, day, *, fee="0", fee_asset="USDT", order=None):
    return {"id": i, "orderId": order or 900 + i, "price": price, "qty": qty, "quoteQty": "0", "commission": fee,
            "commissionAsset": fee_asset, "time": T0 + day * DAY, "isBuyer": buyer}


class Fake:
    def __init__(self, balances, trades):
        self.balances, self.trades = balances, trades

    def api_restrictions(self):
        return parse_api_restrictions(READ_ONLY)

    def verify_read_only_permissions(self):
        return self.api_restrictions()

    def account_snapshot(self):
        return {"balances": [{"asset": a, "free": q, "locked": "0"} for a, q in self.balances.items()]}

    def my_trades(self, symbol, *, limit, from_id=None):
        if symbol not in self.trades:
            raise accounts.MainnetError("HTTP 400, code -1121: Invalid symbol.")
        return tuple(t for t in self.trades[symbol] if t["id"] >= (from_id or 0))[:limit]


def _setup(tmp_path, balances, trades):
    prices = lambda a: {k: {"price": "300", "at": "t"} for k in a}  # noqa: E731
    link = accounts.BinanceLink(tmp_path, vault=_vault(tmp_path), prices=prices,
                                client_factory=lambda c: Fake(balances, trades))
    link.connect(KEY, SECRET, pin_set=True)
    return link, portfolio.Ledger(tmp_path)


def _row(link, lg, asset):
    return next(r for r in link.balances(lg)["balances"] if r["asset"] == asset)


def test_a_partial_history_with_the_same_quantity_never_shows_a_cost(tmp_path):
    full = [_t(1, True, "1", "100", 0), _t(2, True, "1", "200", 3), _t(3, False, "1", "300", 6)]
    link, lg = _setup(tmp_path, {"BTC": "1"}, {"BTCUSDT": full})
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-04T00:00", "qty": "1", "price": "200", "wallet": "Binance"})
    assert _row(link, lg, "BTC")["history"] == "unsynced"
    link.sync_trades(["BTC"])
    r = _row(link, lg, "BTC")
    assert r["history"] == "incomplete" and r["cost"] is None and r["unrealized"] is None and r["avg_cost"] is None
    # once the two missing trades are imported the full history is in the ledger and the real basis appears
    ready = [x["trade"]["ext_id"] for x in link.preview(lg)["rows"] if x["status"] == "ready"]
    assert ready == ["binance:BTCUSDT:1", "binance:BTCUSDT:3"]
    link.import_selected(lg, ready)
    r = _row(link, lg, "BTC")
    basis = portfolio.compute(lg.transactions())["BTC"]["basis"]
    assert r["history"] == "complete" and Decimal(r["cost"]) == basis.quantize(Decimal("0.01")) and r["cost"] != "200.00"


def test_one_manual_row_cannot_cover_two_binance_trades(tmp_path):
    trades = [_t(1, True, "1", "200", 0), _t(2, True, "1", "200", 0)]
    link, lg = _setup(tmp_path, {"BTC": "1"}, {"BTCUSDT": trades})
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01T00:00", "qty": "1", "price": "200", "wallet": "Binance"})
    link.sync_trades(["BTC"])
    assert _row(link, lg, "BTC")["history"] == "incomplete"       # quantity 1 == 1, but one Binance trade is unrecorded


def test_a_whole_order_typed_once_covers_its_parts(tmp_path):
    trades = [_t(1, True, "0.4", "200", 0, order=5), _t(2, True, "0.6", "200", 0, order=5)]
    link, lg = _setup(tmp_path, {"BTC": "1"}, {"BTCUSDT": trades})
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01T00:00", "qty": "1", "price": "200", "wallet": "Binance"})
    link.sync_trades(["BTC"])
    r = _row(link, lg, "BTC")
    assert r["history"] == "complete" and r["cost"] == "200.00"


def test_a_fee_in_a_third_asset_leaves_both_assets_unavailable(tmp_path):
    trades = [_t(1, True, "1", "200", 0, fee="0.01", fee_asset="BNB")]
    link, lg = _setup(tmp_path, {"BTC": "1", "BNB": "1"}, {"BTCUSDT": trades, "BNBUSDT": []})
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01T00:00", "qty": "1", "price": "200", "wallet": "Binance"})
    lg.add({"type": "buy", "asset": "BNB", "at": "2025-12-01T00:00", "qty": "1", "price": "600", "wallet": "Binance"})
    link.sync_trades(["BTC", "BNB"])
    assert _row(link, lg, "BTC")["history"] == "incomplete" and _row(link, lg, "BNB")["history"] == "incomplete"


def test_only_the_assets_of_the_latest_sync_count(tmp_path):
    link, lg = _setup(tmp_path, {"BTC": "1", "ETH": "2"}, {"BTCUSDT": [], "ETHUSDT": []})
    lg.add({"type": "buy", "asset": "ETH", "at": "2026-01-01T00:00", "qty": "2", "price": "100", "wallet": "Binance"})
    link.sync_trades(["BTC", "ETH"])
    assert _row(link, lg, "ETH")["history"] == "complete"
    link.sync_trades(["BTC"])
    assert _row(link, lg, "ETH")["history"] == "unsynced" and _row(link, lg, "ETH")["cost"] is None
