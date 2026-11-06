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
