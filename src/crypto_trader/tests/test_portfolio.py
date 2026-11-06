"""D40 phase 1, item 1: the owner's portfolio ledger and Weighted Average Cost accounting."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
from decimal import Decimal

import pytest

from crypto_trader.strategies import StrategyError
from crypto_trader.strategies.ui import portfolio
from crypto_trader.tests.test_strategies_ui import live, ws  # noqa: F401  (server fixtures)


def _ledger(tmp_path, prices=None):
    return portfolio.Ledger(tmp_path, prices=prices)


def _row(summary, asset):
    return next(a for a in summary["assets"] if a["asset"] == asset)


def test_wac_buy_buy_sell_keeps_the_average_and_books_realized_profit(tmp_path):
    lg = _ledger(tmp_path)
    lg.add({"type": "buy", "asset": "btc", "at": "2026-01-05", "qty": "0.1", "price": "60000", "fee": "6"})
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-02-05", "qty": "0.1", "price": "70000", "fee": "7"})
    # basis = 6006 + 7007 = 13013 for 0.2 -> avg 65065
    s = compute_one(lg, "BTC")
    assert (s["qty"], s["basis"], s["avg_cost"]) == (Decimal("0.2"), Decimal("13013"), Decimal("65065"))
    lg.add({"type": "sell", "asset": "BTC", "at": "2026-03-05", "qty": "0.05", "price": "80000", "fee": "4"})
    s = compute_one(lg, "BTC")
    # realized = 0.05*80000 - 4 - 0.05*65065 = 4000 - 4 - 3253.25 = 742.75 ; avg unchanged
    assert s["realized"] == Decimal("742.75") and s["avg_cost"] == Decimal("65065")
    assert s["qty"] == Decimal("0.15") and s["basis"] == Decimal("9759.75")
    assert s["invested"] == Decimal("13013") and s["fees_quote"] == Decimal("17")


def compute_one(lg, asset):
    return portfolio.compute(lg._load())[asset]


def test_fee_in_the_asset_on_a_buy_is_capitalised_into_the_average(tmp_path):
    lg = _ledger(tmp_path)
    lg.add({"type": "buy", "asset": "ETH", "at": "2026-01-01", "qty": "2", "price": "2500", "fee": "0.002",
            "fee_asset": "ETH"})
    s = compute_one(lg, "ETH")
    assert s["qty"] == Decimal("1.998") and s["basis"] == Decimal("5000")
    q = Decimal("1e-18")                              # compute() runs at 80 digits; compare at 18 places
    assert s["avg_cost"].quantize(q) == (Decimal("5000") / Decimal("1.998")).quantize(q) and s["realized"] == 0


@pytest.mark.parametrize("fee", ["1", "1.5"])
def test_an_asset_fee_that_eats_the_whole_purchase_is_refused(tmp_path, fee):
    # Sol D40 blocker 3: buy 1 BTC @ 100 with a 1 BTC fee left qty 0, basis 0, realized 0 - the 100 USDT vanished
    lg = _ledger(tmp_path)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "5", "price": "100"})   # other holdings exist
    with pytest.raises(StrategyError, match="أقل من الكمية المشتراة"):
        lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-02", "qty": "1", "price": "100", "fee": fee,
                "fee_asset": "BTC"})
    assert len(lg.transactions()) == 1


def test_more_precision_than_the_ledger_holds_is_refused(tmp_path):
    # adversarial check of Sol D40 blocker 3: qty 1.00000000000000000000000000001 was rounded to 1 when added, so
    # "fee 1 < qty" passed on the exact input while the held quantity became 0 and the 100 USDT cost vanished
    lg = _ledger(tmp_path)
    for body in ({"qty": "1.00000000000000000000000000001", "fee": "1", "fee_asset": "BTC"},
                 {"qty": "1", "price": "1.0000000000000000001"}, {"qty": "1000000000000000"}):
        with pytest.raises(StrategyError, match="دقة الرقم"):
            lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01", "price": "100", **body})
    assert lg.transactions() == []


def test_the_smallest_allowed_residual_keeps_the_whole_cost(tmp_path):
    lg = _ledger(tmp_path)
    lg.add({"type": "buy", "asset": "ETH", "at": "2026-01-01", "qty": "1.000000000000000001", "price": "100",
            "fee": "1", "fee_asset": "ETH"})
    s = compute_one(lg, "ETH")
    assert s["qty"] == Decimal("0.000000000000000001") and s["basis"] == Decimal("100.0000000000000001")


def test_large_and_tiny_quantities_add_up_exactly(tmp_path):
    lg = _ledger(tmp_path)
    lg.add({"type": "buy", "asset": "SHIB", "at": "2026-01-01", "qty": "999999999999999", "price": "0.00001"})
    lg.add({"type": "buy", "asset": "SHIB", "at": "2026-01-02", "qty": "0.000000000000000001", "price": "0.00001"})
    assert compute_one(lg, "SHIB")["qty"] == Decimal("999999999999999.000000000000000001")
    lg.add({"type": "sell", "asset": "SHIB", "at": "2026-01-03", "qty": "999999999999999.000000000000000001",
            "price": "0.00002"})                                  # the exact held quantity can be sold
    assert compute_one(lg, "SHIB")["qty"] == 0


def test_negative_zero_is_stored_as_zero(tmp_path):
    tx = _ledger(tmp_path).add({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "1", "price": "1",
                                "fee": "-0.0"})
    assert tx["fee"] == "0"


def test_transfer_between_own_wallets_is_never_a_gain_and_its_network_fee_is_a_cost(tmp_path):
    lg = _ledger(tmp_path)
    lg.add({"type": "buy", "asset": "ETH", "at": "2026-01-01", "qty": "2", "price": "2500"})
    lg.add({"type": "transfer", "asset": "ETH", "at": "2026-01-02", "qty": "2", "fee": "0", "wallet": "ledger"})
    s = compute_one(lg, "ETH")
    assert (s["qty"], s["basis"], s["realized"]) == (Decimal(2), Decimal(5000), 0)
    lg.add({"type": "transfer", "asset": "ETH", "at": "2026-01-03", "qty": "1", "fee": "0.01", "network": "Arbitrum"})
    s = compute_one(lg, "ETH")
    # fee 0.01 ETH at avg 2500 = 25 USDT cost; avg unchanged
    assert s["qty"] == Decimal("1.99") and s["realized"] == Decimal("-25") and s["avg_cost"] == Decimal(2500)
    assert s["fees_asset_cost"] == Decimal(25) and s["networks"] == ["Arbitrum"]


def test_selling_more_than_held_is_refused_and_nothing_is_written(tmp_path):
    lg = _ledger(tmp_path)
    lg.add({"type": "buy", "asset": "SOL", "at": "2026-01-01", "qty": "10", "price": "100"})
    before = lg.path.read_bytes()
    with pytest.raises(StrategyError, match="أكبر من الرصيد"):
        lg.add({"type": "sell", "asset": "SOL", "at": "2026-01-02", "qty": "11", "price": "120"})
    assert lg.path.read_bytes() == before


def test_two_ledgers_on_one_workspace_never_lose_a_write(tmp_path):
    # Sol D40 blocker 6: each instance had its own thread lock, so two servers could load the same snapshot and
    # the later save dropped the other's transaction. A: load inside its lock; B: add from another thread; A: save.
    import threading
    import time

    a, b = _ledger(tmp_path), _ledger(tmp_path)
    done = threading.Event()
    with a._lock:
        snap = a._load()
        t = threading.Thread(target=lambda: (b.add({"type": "buy", "asset": "ETH", "at": "2026-01-02", "qty": "1",
                                                    "price": "2000"}), done.set()))
        t.start()
        time.sleep(0.5)
        assert not done.is_set()                      # B waits for A's workspace lock
        a._save(snap + [{**portfolio.parse_tx({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "1",
                                               "price": "50000"}), "id": "a1", "seq": 1, "source": "t"}])
    t.join(20)
    assert done.is_set() and sorted(x["asset"] for x in a.transactions()) == ["BTC", "ETH"]


def test_readers_never_break_concurrent_writes(tmp_path):
    # adversarial check of D40: DCA status and CSV preview read ledger.json / dca.json without the workspace lock; on
    # Windows an open reader handle makes the writer's os.replace fail (PermissionError) and valid adds were refused
    import threading

    from crypto_trader.strategies.ui import dca, portfolio_csv

    lg = _ledger(tmp_path)
    plans = dca.Plans(lg)
    plan = plans.create({"asset": "BTC", "amount": "10", "day": "1", "start": "2026-01"})
    errors, stop = [], threading.Event()
    csv_text = "date,type,asset,quantity,price\n2026-01-01,buy,ETH,1,100\n"

    def reader():
        while not stop.is_set():
            try:
                plans.status()
                portfolio_csv.preview(csv_text, lg)
            except Exception as exc:              # noqa: BLE001 - any failure is the defect
                errors.append("read " + repr(exc))

    def writer(k):
        for i in range(40):
            try:
                lg.add({"type": "buy", "asset": "BTC", "at": f"2026-01-{1 + i % 28:02d}", "qty": "0.001",
                        "price": "100", "note": f"w{k}-{i}", "dca_plan": plan["id"]})
                plans.set_active(plan["id"], True)
            except Exception as exc:              # noqa: BLE001
                errors.append("write " + repr(exc))

    rs = [threading.Thread(target=reader) for _ in range(3)]
    ws_ = [threading.Thread(target=writer, args=(k,)) for k in range(3)]
    [t.start() for t in rs + ws_]
    [t.join(120) for t in ws_]
    stop.set()
    [t.join(30) for t in rs]
    assert errors == [] and len(lg.transactions()) == 120


def test_a_second_process_waits_for_the_workspace_lock(tmp_path):
    import subprocess
    import sys

    import time

    # the child prints "ready" right before add() and the wall-clock time add() returned; the parent holds the
    # workspace lock until well after "ready", so the write can only finish after the parent released the lock
    code = ("import sys, time; sys.path.insert(0, sys.argv[2]);"
            "from crypto_trader.strategies.ui import portfolio;"
            "lg = portfolio.Ledger(sys.argv[1]); print('ready', flush=True);"
            "lg.add({'type':'buy','asset':'SOL','at':'2026-01-03','qty':'1','price':'100'});"
            "print(time.time(), flush=True)")
    lg = _ledger(tmp_path)
    src = str(Path(portfolio.__file__).resolve().parents[3])
    with lg._lock:
        p = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), src], stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "ready"
        time.sleep(1.0)
        assert p.poll() is None and lg._load() == []  # blocked on the lock file, nothing written
        released = time.time()
    out, _ = p.communicate(timeout=60)
    assert p.returncode == 0 and float(out.strip()) >= released
    assert [x["asset"] for x in lg.transactions()] == ["SOL"]


def test_a_sale_dated_before_its_buy_is_refused(tmp_path):
    lg = _ledger(tmp_path)
    lg.add({"type": "buy", "asset": "SOL", "at": "2026-02-01", "qty": "10", "price": "100"})
    with pytest.raises(StrategyError):
        lg.add({"type": "sell", "asset": "SOL", "at": "2026-01-15", "qty": "1", "price": "120"})


def test_deleting_a_buy_that_a_later_sale_needs_is_refused(tmp_path):
    lg = _ledger(tmp_path)
    buy = lg.add({"type": "buy", "asset": "SOL", "at": "2026-01-01", "qty": "10", "price": "100"})
    lg.add({"type": "sell", "asset": "SOL", "at": "2026-01-02", "qty": "5", "price": "120"})
    with pytest.raises(StrategyError):
        lg.delete(buy["id"])
    assert len(lg.transactions()) == 2


def test_add_many_is_all_or_nothing(tmp_path):
    lg = _ledger(tmp_path)
    rows = [{"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "1", "price": "50000"},
            {"type": "sell", "asset": "BTC", "at": "2026-01-02", "qty": "2", "price": "50000"}]
    with pytest.raises(StrategyError):
        lg.add_many(rows, source="test")
    assert lg.transactions() == [] and not lg.path.exists()


@pytest.mark.parametrize("body, match", [
    ({"type": "buy", "asset": "USDT", "at": "2026-01-01", "qty": "1", "price": "1"}, "عملة التسعير"),
    ({"type": "buy", "asset": "BTC", "at": "2999-01-01", "qty": "1", "price": "1"}, "المستقبل"),
    ({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "-1", "price": "1"}, "أكبر من صفر"),
    ({"type": "swap", "asset": "BTC", "at": "2026-01-01", "qty": "1", "price": "1"}, "نوع العملية"),
    ({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "1", "price": "1", "fee_asset": "ETH"}, "عملة الرسوم"),
    ({"type": "sell", "asset": "BTC", "at": "2026-01-01", "qty": "1", "price": "1", "dca_plan": "p1"}, "للمشتريات فقط"),
])
def test_invalid_transactions_are_refused_in_arabic(tmp_path, body, match):
    with pytest.raises(StrategyError, match=match):
        _ledger(tmp_path).add(body)


def test_summary_values_priced_assets_and_marks_unpriced_ones_instead_of_inventing_a_number(tmp_path):
    def prices(assets):
        assert assets == ["BTC", "ETH", "XYZ"]
        return {"BTC": {"price": "70000", "source": "test", "at": "t"}, "ETH": {"price": "2500", "source": "test"},
                "XYZ": {"price": None, "reason": "no market"}}

    lg = _ledger(tmp_path, prices)
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "0.1", "price": "60000"})
    lg.add({"type": "buy", "asset": "ETH", "at": "2026-01-01", "qty": "2", "price": "2400"})
    lg.add({"type": "buy", "asset": "XYZ", "at": "2026-01-01", "qty": "100", "price": "1"})
    s = lg.summary()
    t = s["totals"]
    assert t["value"] == "12000" and t["priced_basis"] == "10800" and t["unrealized"] == "1200"
    assert t["unrealized_pct"] == "11.11" and t["value_complete"] is False and s["unpriced"] == ["XYZ"]
    assert t["basis"] == "10900"
    xyz = _row(s, "XYZ")
    assert xyz["value"] is None and xyz["price_missing"] == "no market" and xyz["allocation_pct"] is None
    assert _row(s, "BTC")["allocation_pct"] == "58.33" and _row(s, "ETH")["unrealized_pct"] == "4.17"


def test_closed_positions_keep_their_realized_result_and_need_no_price(tmp_path):
    lg = _ledger(tmp_path, prices=lambda assets: pytest.fail("no price lookup for closed positions"))
    lg.add({"type": "buy", "asset": "ADA", "at": "2026-01-01", "qty": "3", "price": "0.1"})
    lg.add({"type": "sell", "asset": "ADA", "at": "2026-01-02", "qty": "3", "price": "0.4"})
    row = _row(lg.summary(), "ADA")
    assert row["closed"] and row["qty"] == "0" and row["realized"] == "0.9" and row["value"] is None


def _post(port, path, body, header=True):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Host": "127.0.0.1", "Content-Type": "application/json"} | ({"X-CT-UI": "1"} if header else {})
    conn.request("POST", path, body=json.dumps(body), headers=h)
    resp = conn.getresponse()
    out = (resp.status, json.loads(resp.read()))
    conn.close()
    return out


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", path, headers={"Host": "127.0.0.1"})
    resp = conn.getresponse()
    out = (resp.status, json.loads(resp.read()))
    conn.close()
    return out


def test_portfolio_api_adds_lists_and_deletes_with_the_ui_guard(live, ws, monkeypatch):
    from crypto_trader.strategies.ui import server

    # offline: the server's price lookup is replaced; prices have their own tests (test_portfolio_prices.py)
    monkeypatch.setattr(server.Handler, "ledger", portfolio.Ledger(ws.root, prices=lambda assets: {}))
    buy = {"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "0.5", "price": "40000"}
    assert _post(live, "/api/portfolio/add", buy, header=False)[0] == 403
    status, res = _post(live, "/api/portfolio/add", buy)
    assert status == 200 and res["transaction"]["asset"] == "BTC"
    status, res = _post(live, "/api/portfolio/add", {**buy, "type": "sell", "qty": "1"})
    assert status == 400 and "أكبر من الرصيد" in res["error"]
    status, doc = _get(live, "/api/portfolio")
    assert status == 200 and doc["n_transactions"] == 1 and doc["totals"]["basis"] == "20000"
    assert (ws.root / "portfolio" / "ledger.json").exists()
    assert _post(live, "/api/portfolio/delete", {"id": doc["transactions"][0]["id"]})[0] == 200
    assert _get(live, "/api/portfolio")[1]["n_transactions"] == 0


def test_a_saved_number_beyond_the_bounds_is_refused_on_replay_never_rounded(tmp_path):
    # Sol D40 recheck, blocker 3: a quantity saved before the precision bounds rounded to 0 in the 80-digit replay and
    # the 100 USDT cost vanished (qty 0E-79, basis 0, realized 0). Replay now applies the same bounds and refuses it.
    legacy = {"type": "buy", "asset": "BTC", "at": "2026-01-01T00:00:00+00:00", "qty": "1." + "0" * 80 + "1",
              "price": "100", "fee": "1", "fee_asset": "BTC"}
    with pytest.raises(StrategyError, match="خارج حدود الدقة"):
        portfolio.compute([legacy])
    lg = _ledger(tmp_path)
    lg.add({"type": "buy", "asset": "ETH", "at": "2026-01-02", "qty": "1", "price": "2000"})
    txs = lg._load()
    txs.append({**legacy, "id": "legacy1", "seq": 99})
    lg._save(txs)
    with pytest.raises(StrategyError, match="خارج حدود الدقة"):
        lg.summary()
    lg.delete("legacy1")                      # the owner can remove it and enter it again
    assert [a["asset"] for a in lg.summary()["assets"]] == ["ETH"]
