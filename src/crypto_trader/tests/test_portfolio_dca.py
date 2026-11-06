"""D40 phase 1, item 4: monthly DCA plans - tracking only, executed buys are the portfolio ledger's own records."""
from __future__ import annotations

from datetime import date

import pytest

from crypto_trader.strategies import StrategyError
from crypto_trader.strategies.ui import dca, portfolio

TODAY = date(2026, 4, 5)


def _setup(tmp_path, prices=None):
    lg = portfolio.Ledger(tmp_path)
    return lg, dca.Plans(lg, prices=prices, today=lambda: TODAY)


def test_months_are_executed_missed_due_or_upcoming_from_the_ledger(tmp_path):
    lg, plans = _setup(tmp_path, prices=lambda a: {"BTC": {"price": "80000", "source": "test", "at": "t"}})
    p = plans.create({"asset": "btc", "amount": "100", "day": "5", "start": "2026-01"})
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-05", "qty": "0.002", "price": "50000", "fee": "0.1",
            "dca_plan": p["id"]})
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-03-06", "qty": "0.0016", "price": "62500", "dca_plan": p["id"]})
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-03-20", "qty": "1", "price": "60000"})   # not part of the plan
    s = plans.status()["plans"][0]
    assert [(m["month"], m["state"]) for m in s["months"]] == [
        ("2026-01", "executed"), ("2026-02", "missed"), ("2026-03", "executed"), ("2026-04", "due")]
    assert s["qty"] == "0.0036" and s["paid"] == "200.1" and s["avg_cost"] == "55583.33333333"
    assert s["value"] == "288" and s["value_minus_paid"] == "87.9" and s["n_missed"] == 1
    assert s["next_purchase"] == "2026-04-05"
    rem = plans.status()["reminders"]
    assert [(r["month"], r["state"]) for r in rem] == [("2026-02", "missed"), ("2026-04", "due")]


def test_dca_buys_are_ordinary_ledger_transactions_not_a_second_copy(tmp_path):
    lg, plans = _setup(tmp_path)
    p = plans.create({"asset": "ETH", "amount": "50", "day": "1", "start": "2026-04"})
    tx = lg.add({"type": "buy", "asset": "ETH", "at": "2026-04-01", "qty": "0.02", "price": "2500", "dca_plan": p["id"]})
    assert portfolio.compute(lg._load())["ETH"]["qty"] == portfolio.Decimal("0.02")
    assert plans.status()["plans"][0]["months"][0]["tx_ids"] == [tx["id"]]
    assert "dca" not in lg.path.read_text(encoding="utf-8").replace("dca_plan", "")


@pytest.mark.parametrize("plan_id, asset, match", [("p-missing", "BTC", "غير موجودة"), (None, "ETH", "لنفس الأصل")])
def test_a_new_buy_can_only_be_tagged_with_an_existing_plan_of_its_asset(tmp_path, plan_id, asset, match):
    # Sol D40 blocker 4: an ETH buy tagged with a BTC plan was accepted and counted as BTC
    lg, plans = _setup(tmp_path, prices=lambda a: {"BTC": {"price": "100", "source": "t"}})
    p = plans.create({"asset": "BTC", "amount": "20", "day": "5", "start": "2026-04"})
    with pytest.raises(StrategyError, match=match):
        lg.add({"type": "buy", "asset": asset, "at": "2026-04-05", "qty": "2", "price": "10",
                "dca_plan": plan_id or p["id"]})
    assert lg.transactions() == [] and plans.status()["plans"][0]["qty"] == "0"


def test_quantity_and_paid_come_from_the_same_in_plan_buys(tmp_path):
    # Sol D40 blocker 5: a January buy linked to an April plan counted in quantity (1) but not in paid (0)
    lg, plans = _setup(tmp_path, prices=lambda a: {"BTC": {"price": "100", "source": "t"}})
    p = plans.create({"asset": "BTC", "amount": "100", "day": "5", "start": "2026-04"})
    jan = lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-05", "qty": "1", "price": "100"})
    with pytest.raises(StrategyError, match="خارج فترة الخطة"):
        plans.link(jan["id"], p["id"])
    with pytest.raises(StrategyError, match="خارج فترة الخطة"):
        lg.add({"type": "buy", "asset": "BTC", "at": "2026-03-31", "qty": "1", "price": "90", "dca_plan": p["id"]})
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-04-05", "qty": "0.5", "price": "200", "dca_plan": p["id"]})
    s = plans.status()["plans"][0]
    assert (s["qty"], s["paid"], s["avg_cost"], s["value"]) == ("0.5", "100", "200", "50")
    # a tag written before this rule (outside the plan) is reported, never counted
    txs = lg._load()
    next(t for t in txs if t["id"] == jan["id"])["dca_plan"] = p["id"]
    lg._save(txs)
    s = plans.status()["plans"][0]
    assert s["qty"] == "0.5" and s["paid"] == "100" and s["linked_outside_plan"] == [jan["id"]]


def test_a_ledger_without_plans_refuses_plan_tags(tmp_path):
    with pytest.raises(StrategyError, match="غير موجودة"):
        portfolio.Ledger(tmp_path).add({"type": "buy", "asset": "BTC", "at": "2026-04-05", "qty": "1", "price": "1",
                                         "dca_plan": "x"})


def test_link_and_unlink_an_existing_buy_same_asset_only(tmp_path):
    lg, plans = _setup(tmp_path)
    p = plans.create({"asset": "ETH", "amount": "50", "day": "1", "start": "2026-04"})
    eth = lg.add({"type": "buy", "asset": "ETH", "at": "2026-04-02", "qty": "0.02", "price": "2500"})
    sol = lg.add({"type": "buy", "asset": "SOL", "at": "2026-04-02", "qty": "1", "price": "100"})
    with pytest.raises(StrategyError, match="لنفس الأصل"):
        plans.link(sol["id"], p["id"])
    plans.link(eth["id"], p["id"])
    assert plans.status()["plans"][0]["months"][0]["state"] == "executed"
    plans.link(eth["id"], None)
    assert plans.status()["plans"][0]["months"][0]["state"] == "missed"     # planned 04-01, today 04-05


def test_a_stopped_plan_raises_no_reminders_and_has_no_next_purchase(tmp_path):
    lg, plans = _setup(tmp_path)
    p = plans.create({"asset": "BTC", "amount": "100", "day": "5", "start": "2026-01"})
    plans.set_active(p["id"], False)
    st = plans.status()
    assert st["reminders"] == [] and st["plans"][0]["next_purchase"] is None


def test_unpriced_plan_asset_shows_not_available_instead_of_a_value(tmp_path):
    lg, plans = _setup(tmp_path, prices=lambda a: {"XYZ": {"price": None, "reason": "no Binance USDT pair"}})
    plans.create({"asset": "XYZ", "amount": "10", "day": "3", "start": "2026-04"})
    s = plans.status()["plans"][0]
    assert s["value"] is None and s["value_minus_paid"] is None and s["price_missing"] == "no Binance USDT pair"


@pytest.mark.parametrize("body, match", [
    ({"asset": "BTC", "amount": "100", "day": "31"}, "بين 1 و28"),
    ({"asset": "BTC", "amount": "0", "day": "5"}, "أكبر من صفر"),
    ({"asset": "USDT", "amount": "10", "day": "5"}, "عملة التسعير"),
    ({"asset": "BTC", "amount": "10", "day": "5", "start": "Jan"}, "شهر البداية"),
])
def test_invalid_plans_are_refused(tmp_path, body, match):
    with pytest.raises(StrategyError, match=match):
        _setup(tmp_path)[1].create(body)


def test_dca_api_create_link_stop(tmp_path, monkeypatch):
    import http.client
    import json
    import threading

    from crypto_trader.strategies.ui import server

    srv = server.make_server(tmp_path, 0)
    lg = portfolio.Ledger(tmp_path, prices=lambda a: {})
    monkeypatch.setattr(server.Handler, "ledger", lg)
    monkeypatch.setattr(server.Handler, "dca", dca.Plans(lg, prices=lambda a: {}, today=lambda: TODAY))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def call(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {"Host": "127.0.0.1", "X-CT-UI": "1", "Content-Type": "application/json"})
        r = c.getresponse()
        out = (r.status, json.loads(r.read()))
        c.close()
        return out

    try:
        st, res = call("POST", "/api/dca/create", {"asset": "BTC", "amount": "100", "day": "5", "start": "2026-04"})
        assert st == 200
        pid = res["plan"]["id"]
        tx = lg.add({"type": "buy", "asset": "BTC", "at": "2026-04-05", "qty": "0.001", "price": "100000"})
        assert call("POST", "/api/dca/link", {"tx_id": tx["id"], "plan": pid})[0] == 200
        st, doc = call("GET", "/api/dca")
        assert st == 200 and doc["plans"][0]["months"][0]["state"] == "executed"
        assert call("POST", "/api/dca/active", {"id": pid, "active": False})[1]["plan"]["active"] is False
        assert call("POST", "/api/dca/link", {"tx_id": "nope", "plan": pid})[0] == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_status_says_tracking_only(tmp_path):
    assert "no purchase is ever placed" in _setup(tmp_path)[1].status()["note"]


def test_status_reads_stored_numbers_within_the_ledger_bounds(tmp_path):
    # Sol D40 recheck (non-blocking, phase 2 item 0): status computed from unbounded stored decimals and reported a
    # purchase the portfolio replay refuses
    lg, plans = _setup(tmp_path, prices=lambda a: {"BTC": {"price": "100", "source": "test", "at": "t"}})
    pid = plans.create({"asset": "BTC", "amount": "100", "day": "5", "start": "2026-04"})["id"]
    lg.add({"type": "buy", "asset": "BTC", "at": "2026-04-05", "qty": "1", "price": "100", "dca_plan": pid})
    txs = lg._load()
    txs[0]["qty"] = "1." + "0" * 80 + "1"
    lg._save(txs)
    with pytest.raises(StrategyError, match="خارج حدود الدقة"):
        plans.status()
