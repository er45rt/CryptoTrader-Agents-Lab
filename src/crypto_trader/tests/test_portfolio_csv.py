"""D40 phase 1, item 3: CSV import for My Portfolio - preview never writes, confirm is all-or-nothing, undo per import."""
from __future__ import annotations

import pytest

from crypto_trader.strategies import StrategyError
from crypto_trader.strategies.ui import portfolio, portfolio_csv as pcsv

GOOD = ("date,type,asset,quantity,price,fee,fee_asset,network,wallet,note\n"
        "2026-01-05,buy,BTC,0.01,65000,0.65,USDT,Bitcoin,Binance,\"monthly, DCA\"\n"
        "2026-02-05,buy,BTC,0.01,70000,0.7,USDT,Bitcoin,Binance,\n"
        "2026-02-10,sell,BTC,0.005,80000,,,Bitcoin,Binance,\n")


def _lg(tmp_path):
    return portfolio.Ledger(tmp_path)


def test_preview_parses_every_row_and_writes_nothing(tmp_path):
    lg = _lg(tmp_path)
    p = pcsv.preview(GOOD, lg)
    assert p["can_import"] and (p["n_rows"], p["n_ok"], p["n_errors"], p["n_duplicates"]) == (3, 3, 0, 0)
    assert p["rows"][0]["tx"]["note"] == "monthly, DCA" and p["rows"][2]["tx"]["fee"] == "0"
    assert not lg.path.exists()


def test_confirm_imports_all_rows_with_one_batch_and_undo_removes_exactly_them(tmp_path):
    lg = _lg(tmp_path)
    lg.add({"type": "buy", "asset": "ETH", "at": "2026-01-01", "qty": "1", "price": "3000"})
    p = pcsv.preview(GOOD, lg)
    res = pcsv.confirm(GOOD, p["sha256"], lg, filename="binance.csv")
    assert res == {"batch": p["batch"], "imported": 3, "skipped_duplicates": 0}
    txs = lg.transactions()
    assert sum(t.get("batch") == p["batch"] for t in txs) == 3
    assert {t["source"] for t in txs if t.get("batch")} == {"csv:binance.csv"}
    with pytest.raises(StrategyError):                       # same file twice
        pcsv.confirm(GOOD, p["sha256"], lg)
    assert pcsv.preview(GOOD, lg)["already_imported"] is True
    assert lg.delete_batch(p["batch"])["removed"] == 3
    assert [t["asset"] for t in lg.transactions()] == ["ETH"]


def test_one_bad_row_blocks_the_whole_import_and_is_reported_by_line(tmp_path):
    bad = GOOD + "2026-02-11,buy,BTC,abc,1,,,,,\n2026-02-12,swap,BTC,1,1,,,,,\n"
    lg = _lg(tmp_path)
    p = pcsv.preview(bad, lg)
    assert not p["can_import"] and [r["line"] for r in p["rows"] if "error" in r] == [5, 6]
    with pytest.raises(StrategyError, match="صحّح"):
        pcsv.confirm(bad, p["sha256"], lg)
    assert not lg.path.exists()


def test_a_sale_the_ledger_can_not_cover_blocks_the_import(tmp_path):
    text = "date,type,asset,quantity,price\n2026-01-01,sell,SOL,1,100\n"
    p = pcsv.preview(text, _lg(tmp_path))
    assert p["replay_error"] and "أكبر من الرصيد" in p["replay_error"] and not p["can_import"]


def test_duplicates_block_unless_the_owner_chooses_to_skip_them(tmp_path):
    lg = _lg(tmp_path)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-05", "qty": "0.01", "price": "65000", "fee": "0.65"})
    p = pcsv.preview(GOOD, lg)
    assert p["n_duplicates"] == 1 and p["rows"][0]["duplicate"] == "existing" and not p["can_import"]
    p2 = pcsv.preview(GOOD, lg, skip_duplicates=True)
    assert p2["can_import"] and p2["n_ok"] == 2
    res = pcsv.confirm(GOOD, p2["sha256"], lg, skip_duplicates=True)
    assert res["imported"] == 2 and res["skipped_duplicates"] == 1 and len(lg.transactions()) == 3


def test_a_file_changed_after_the_preview_is_refused(tmp_path):
    lg = _lg(tmp_path)
    p = pcsv.preview(GOOD, lg)
    with pytest.raises(StrategyError, match="تغيّر"):
        pcsv.confirm(GOOD.replace("65000", "65001"), p["sha256"], lg)


@pytest.mark.parametrize("text", ["", "when,what\n1,2\n", "date,type\n", "date,type,asset,asset\n"])
def test_a_wrong_header_is_refused(tmp_path, text):
    with pytest.raises(StrategyError, match="عنوان الملف"):
        pcsv.preview(text, _lg(tmp_path))


def test_excel_bom_quotes_and_blank_lines_are_handled(tmp_path):
    text = "﻿Date,Type,Asset,Quantity,Price\r\n\r\n2026-01-01,buy,ETH,\"1.5\",\"2,000\"\r\n"
    p = pcsv.preview(text, _lg(tmp_path))
    assert p["n_rows"] == 1 and "error" in p["rows"][0]           # "2,000" is not a number: reported, not guessed
    p = pcsv.preview(text.replace('"2,000"', "2000"), _lg(tmp_path))
    assert p["can_import"] and p["rows"][0]["tx"]["qty"] == "1.5"


def test_import_api_preview_confirm_undo(tmp_path, monkeypatch):
    import http.client
    import json
    import threading

    from crypto_trader.strategies.ui import server

    srv = server.make_server(tmp_path, 0)
    monkeypatch.setattr(server.Handler, "ledger", portfolio.Ledger(tmp_path, prices=lambda a: {}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def post(path, body):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request("POST", path, json.dumps(body), {"Host": "127.0.0.1", "X-CT-UI": "1", "Content-Type": "application/json"})
        r = c.getresponse()
        out = (r.status, json.loads(r.read()))
        c.close()
        return out

    try:
        st, p = post("/api/portfolio/import/preview", {"text": GOOD})
        assert st == 200 and p["can_import"] and not (tmp_path / "portfolio" / "ledger.json").exists()
        st, res = post("/api/portfolio/import/confirm", {"text": GOOD, "sha256": p["sha256"], "filename": "a.csv"})
        assert st == 200 and res["imported"] == 3
        st, res = post("/api/portfolio/import/confirm", {"text": GOOD, "sha256": p["sha256"]})
        assert st == 400
        st, res = post("/api/portfolio/import/undo", {"batch": p["batch"]})
        assert st == 200 and res["removed"] == 3
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_duplicate_written_with_different_decimals_is_still_a_duplicate(tmp_path):
    # Sol D40 blocker 2: "1.0 @ 100.0" is the same transaction as "1 @ 100"
    lg = _lg(tmp_path)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "1", "price": "100"})
    p = pcsv.preview("date,type,asset,quantity,price,fee\n2026-01-01,buy,BTC,1.0,100.00,0.0\n", lg)
    assert p["n_duplicates"] == 1 and p["rows"][0]["duplicate"] == "existing" and not p["can_import"]


@pytest.mark.parametrize("manual_fee, manual_fee_asset, csv_fee, csv_fee_asset", [
    ("0", None, "-0", "USDT"), ("", None, "-0.0", ""), ("-0", None, "", ""),          # negative zero
    ("0", "BTC", "", ""), ("", None, "0", "BTC"),                                      # zero fee, other currency
])
def test_zero_fees_match_whatever_their_sign_or_currency(tmp_path, manual_fee, manual_fee_asset, csv_fee, csv_fee_asset):
    # independent adversarial check of Sol D40 blocker 2: these doubled holdings after the first fix
    lg = _lg(tmp_path)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "1", "price": "100", "fee": manual_fee,
            **({"fee_asset": manual_fee_asset} if manual_fee_asset else {})})
    text = f"date,type,asset,quantity,price,fee,fee_asset\n2026-01-01,buy,BTC,1,100,{csv_fee},{csv_fee_asset}\n"
    p = pcsv.preview(text, lg)
    assert p["n_duplicates"] == 1 and not p["can_import"], p["rows"]


def test_a_non_zero_fee_in_another_currency_is_a_different_transaction(tmp_path):
    lg = _lg(tmp_path)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "1", "price": "100", "fee": "0.001",
            "fee_asset": "BTC"})
    p = pcsv.preview("date,type,asset,quantity,price,fee,fee_asset\n2026-01-01,buy,BTC,1,100,0.001,USDT\n", lg)
    assert p["n_duplicates"] == 0


def test_two_concurrent_confirms_of_the_same_file_import_it_once(tmp_path, monkeypatch):
    # Sol D40 blocker 1: both confirms pass their preview, then race into add_many; the guard runs under the lock
    import threading

    text = "date,type,asset,quantity,price\n2026-01-01,buy,BTC,1,100\n"
    lg = _lg(tmp_path)
    sha = pcsv.preview(text, lg)["sha256"]
    barrier, orig, results = threading.Barrier(2), lg.add_many, []

    def synced(*a, **k):
        barrier.wait(timeout=10)
        return orig(*a, **k)

    monkeypatch.setattr(lg, "add_many", synced)

    def run():
        try:
            results.append(pcsv.confirm(text, sha, lg)["imported"])
        except StrategyError as exc:
            results.append(str(exc))

    th = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in th]
    [t.join(20) for t in th]
    assert sorted(map(str, results)) == ["1", "هذا الملف مستورد من قبل."]
    assert str(portfolio.compute(lg._load())["BTC"]["qty"]) == "1"


def test_distinct_numbers_beyond_28_digits_are_never_merged_as_duplicates(tmp_path):
    # Sol D40 recheck, blocker 2: normalize() rounded both quantities to one key and skip_duplicates dropped a real buy
    text = ("date,type,asset,quantity,price\n"
            "2026-01-01,buy,BTC,123456789012345.123456789012345678,1\n"
            "2026-01-01,buy,BTC,123456789012345.123456789012345679,1\n")
    lg = _lg(tmp_path)
    p = pcsv.preview(text, lg, skip_duplicates=True)
    assert p["n_duplicates"] == 0 and p["n_ok"] == 2
    assert pcsv.confirm(text, p["sha256"], lg, skip_duplicates=True) == {"batch": p["batch"], "imported": 2,
                                                                         "skipped_duplicates": 0}
    assert pcsv._n("1.10") == pcsv._n("1.1") == pcsv._n("11e-1") == "1.1" and pcsv._n("1E+2") == "100"


def test_too_many_columns_in_a_row_is_an_error_not_a_silent_shift(tmp_path):
    text = "date,type,asset,quantity,price\n2026-01-01,buy,ETH,1,2000,extra\n"
    p = pcsv.preview(text, _lg(tmp_path))
    assert p["n_errors"] == 1 and "زائدة" in p["rows"][0]["error"]
