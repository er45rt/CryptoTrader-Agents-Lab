"""Sol D44 review B3: an operation the owner already recorded by hand must not be imported again from Binance. The manual
form keeps only minutes (and may be in local time) and one manual row may stand for an order Binance filled in parts,
so an exact-timestamp match is not enough."""
from __future__ import annotations

import pytest

from crypto_trader.strategies.api import StrategyError
from crypto_trader.strategies.ui import accounts, portfolio
from crypto_trader.tests.test_binance_link_trades import T0, _link


def _fill(i, qty, price, *, order=None, ms=0, buyer=True):
    return {"id": i, "orderId": order or 900 + i, "price": price, "qty": qty, "quoteQty": "0", "commission": "0",
            "commissionAsset": "USDT", "time": T0 + 12 * 3_600_000 + ms, "isBuyer": buyer}


def _status(link, lg):
    return {r["trade"]["ext_id"]: r["status"] for r in link.preview(lg)["rows"]}


def test_a_manual_row_entered_to_the_minute_is_not_imported_again(tmp_path):
    link = _link(tmp_path, {"BTCUSDT": [_fill(1, "1", "200", ms=25_000)]}, [])        # Binance: 12:00:25
    lg = portfolio.Ledger(tmp_path)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01T12:00", "qty": "1", "price": "200"})   # form: 12:00
    link.sync_trades(["BTC"])
    assert _status(link, lg) == {"binance:BTCUSDT:1": "possible_duplicate"}
    with pytest.raises(StrategyError):
        link.import_selected(lg, ["binance:BTCUSDT:1"])
    assert portfolio.compute(lg.transactions())["BTC"]["qty"] == 1                 # never doubled to 2


def test_one_manual_row_for_an_order_filled_in_parts_holds_back_every_part(tmp_path):
    fills = [_fill(1, "0.4", "100", order=77), _fill(2, "0.6", "100", order=77, ms=900)]
    link = _link(tmp_path, {"BTCUSDT": fills}, [])
    lg = portfolio.Ledger(tmp_path)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01T15:00:00+03:00", "qty": "1", "price": "100"})   # local time
    link.sync_trades(["BTC"])
    assert set(_status(link, lg).values()) == {"possible_duplicate"}


def test_a_rounded_quantity_still_counts_but_other_days_and_types_do_not(tmp_path):
    fills = [_fill(1, "0.01998", "90000"), _fill(2, "0.02", "90000", ms=3 * 86_400_000), _fill(3, "0.02", "90000", buyer=False)]
    link = _link(tmp_path, {"BTCUSDT": fills}, [])
    lg = portfolio.Ledger(tmp_path)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01T12:00", "qty": "0.02", "price": "90000"})
    link.sync_trades(["BTC"])
    assert _status(link, lg) == {"binance:BTCUSDT:1": "possible_duplicate", "binance:BTCUSDT:2": "ready",
                                 "binance:BTCUSDT:3": "ready"}


def test_a_manual_row_added_after_the_preview_stops_the_import(tmp_path):
    link = _link(tmp_path, {"BTCUSDT": [_fill(1, "1", "200", ms=25_000)]}, [])
    lg = portfolio.Ledger(tmp_path)
    link.sync_trades(["BTC"])
    assert _status(link, lg) == {"binance:BTCUSDT:1": "ready"}
    rows = link.preview(lg)                                          # the preview the owner looked at was clean ...
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01T12:00", "qty": "1", "price": "200"})
    link.preview = lambda _ledger: rows                              # ... then the trade was typed by hand: the lock re-check stops it
    with pytest.raises(StrategyError):
        link.import_selected(lg, ["binance:BTCUSDT:1"])
    assert len(lg.transactions()) == 1


def test_the_window_and_tolerance_are_the_documented_ones():
    assert accounts.DUP_WINDOW_S == 24 * 3600 and str(accounts.DUP_QTY_TOL) == "0.01"
