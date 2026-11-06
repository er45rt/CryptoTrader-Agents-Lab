"""Phase 2 (owner decision D42): dashboard - four separate money spaces, strategy comparison, status bar."""
from __future__ import annotations

import http.client
import re
from pathlib import Path

from crypto_trader.strategies.ui import server
from crypto_trader.tests.test_strategies_ui import live, ws  # noqa: F401  (server fixtures)

STATIC = Path(server.__file__).with_name("static")
DASH = (STATIC / "dashboard.js").read_text(encoding="utf-8")


def test_the_dashboard_is_the_home_page_and_is_served(live):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<script src="/dashboard.js"></script>' in html and "لوحة المتابعة</a>" in html
    assert "PAGES.home = async" in DASH and "PAGES.home = async" not in (STATIC / "app.js").read_text(encoding="utf-8")
    c = http.client.HTTPConnection("127.0.0.1", live, timeout=30)
    c.request("GET", "/dashboard.js", headers={"Host": "127.0.0.1"})
    r = c.getresponse()
    r.read()
    c.close()
    assert r.status == 200 and "javascript" in r.getheader("Content-Type")


def test_four_spaces_each_read_only_their_own_data():
    keys = re.findall(r'\["(portfolio|demo|paper|backtest)", "', DASH[DASH.index("const DB_SPACES"):DASH.index("const dbAt")])
    assert keys == ["portfolio", "demo", "paper", "backtest"]
    # every card / detail reads the space it renders (DB.spaces[k]); no code reaches into another space's data
    uses = re.findall(r"DB\.spaces(\[[^\]]*\]|\.\w+| = \{\})", DASH)
    assert uses and set(uses) <= {"[k]", " = {}"}, uses
    loads = re.findall(r'load\("(\w+)", api\("([^"]+)"\)\)', DASH)
    assert loads == [("portfolio", "/api/portfolio"), ("demo", "/api/demo"), ("paper", "/api/paper"),
                     ("backtest", "/api/runs")]
    src = Path(server.__file__).read_text(encoding="utf-8")
    for path in {p for _, p in loads} | set(re.findall(r'api\("(/api/[a-z/]+)', DASH)):
        assert f'"{path}"' in src, path


def test_each_space_names_its_source_and_nothing_can_trade():
    for src in ("سجل المحفظة · أسعار Binance العامة", "إعادة تشغيل محلية لأسعار تاريخية", "أسعار Binance العامة الحية · محاكاة",
                "سجل التجارب runs.jsonl"):
        assert src in DASH, src
    low = DASH.lower()
    for word in ("/order", "testnet", "mainnet", "api_key", "secret", "withdraw", "/api/paper/start", "/api/paper/new"):
        assert word not in low, word


def _run(root, rid, *, at, strategy="s", data="BTC_USDT-4h.feather", start="2024-01-01T00:00:00+00:00",
         end="2025-01-01T00:00:00+00:00", mode="fast", fee="0.001", slip="5", ret=0.1, write=True):
    import json as _json
    doc = {"id": rid, "at": at, "mode": mode, "strategy": strategy, "strategy_version": "1", "data": data if "/" in data else f"C:/data/{data}",
           "timeframe": "4h", "start": start, "end": end, "bars_count": 10, "params": {},
           "settings": {"capital_quote": "1000", "taker_fee": fee, "maker_fee": fee, "slippage_bps": slip},
           "metrics": {"net_return": ret, "max_drawdown": -0.2, "sharpe": 1.0, "round_trips": 3, "fills": 6,
                       "fees_quote": 4.5}, "benchmark": {"net_return": 0.3}, "verdict": {"verdict": "unproven"}}
    (root / "runs" / rid).mkdir(parents=True, exist_ok=True)
    if write:
        (root / "runs" / rid / "result.json").write_text(_json.dumps(doc), encoding="utf-8")
    with (root / "runs" / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(_json.dumps({"id": rid, "at": at}) + "\n")


def test_backtests_are_compared_only_with_the_same_data_period_mode_and_costs(tmp_path):
    from crypto_trader.strategies.ui import compare

    (tmp_path / "runs").mkdir()
    _run(tmp_path, "a", at="2026-01-01", strategy="sma", ret=0.1)
    _run(tmp_path, "b", at="2026-01-02", strategy="rsi", ret=0.3)
    _run(tmp_path, "c", at="2026-01-03", strategy="rsi", start="2023-01-01T00:00:00+00:00")       # other period
    _run(tmp_path, "d", at="2026-01-04", strategy="sma", mode="engine")                           # other mode
    _run(tmp_path, "e", at="2026-01-05", strategy="sma", fee="0.002")                             # other costs
    _run(tmp_path, "f", at="2026-01-06", strategy="sma", data="ETH_USDT-4h.feather")              # other data
    _run(tmp_path, "g", at="2026-01-07", write=False)                                              # no result file
    res = compare.comparison(tmp_path)
    comp = [g for g in res["groups"] if g["comparable"]]
    assert len(comp) == 1 and [r["id"] for r in comp[0]["runs"]] == ["b", "a"]      # best return first, same group
    assert sorted(r["id"] for g in res["groups"] if not g["comparable"] for r in g["runs"]) == ["c", "d", "e", "f"]
    assert all(len(g["runs"]) == 1 for g in res["groups"] if not g["comparable"])
    assert res["missing"] == ["g"] and res["runs"] == 6
    a = next(r for r in comp[0]["runs"] if r["id"] == "a")
    assert (a["net_return"], a["max_drawdown"], a["round_trips"], a["fees_quote"], a["hold_return"]) == (0.1, -0.2, 3, 4.5, 0.3)


def test_the_comparison_endpoint_and_card(live, ws):
    import json as _json

    c = http.client.HTTPConnection("127.0.0.1", live, timeout=30)
    c.request("GET", "/api/compare", headers={"Host": "127.0.0.1"})
    r = c.getresponse()
    doc = _json.loads(r.read())
    c.close()
    assert r.status == 200 and set(doc) == {"groups", "runs", "missing"}
    body = DASH[DASH.index("async function dbCompare"):]
    assert 'api("/api/compare")' in body and "comparable" in body and "dbCompare();" in DASH


class _WS:
    def __init__(self, root, sessions, collector):
        self.root, self._s, self._c = root, sessions, collector

    def paper_sessions(self):
        return {"sessions": self._s}

    def system(self):
        return {"evidence_collector": self._c}


def test_status_tells_stale_data_from_a_stopped_process(tmp_path):
    from datetime import datetime, timezone

    from crypto_trader.strategies.ui import alerts

    now = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    sess = lambda name, running, last, error=None: {"name": name, "running": running, "last_cycle": last,  # noqa: E731
                                                   "timeframe": "4h", "error": error}
    ws = _WS(tmp_path, [sess("fresh", True, "2026-03-10T08:01:00+00:00"), sess("late", True, "2026-03-09T20:00:00+00:00"),
                        sess("off", False, "2026-03-01T00:00:00+00:00"),
                        sess("broken", False, None, {"at": "2026-03-10T11:00:00+00:00", "error": "PermissionError"})],
             {"available": True, "entries": 9, "last": "2026-03-10T11:45:00+00:00"})
    (tmp_path / "ui_errors.log").write_text("[2026-03-10T09:00:00+00:00] GET /x: boom\n  traceback line\n"
                                            "[2026-03-01T09:00:00+00:00] old\n", encoding="utf-8")
    st = alerts.status(ws, now=now, demos=lambda: {"demos": [{"name": "d1", "state": "error"}, {"name": "d2", "state": "done"}]},
                       price_cache=lambda: {"BTC": {"price": "1", "at": "2026-03-10T11:58:00+00:00"},
                                            "ETH": {"price": "1", "at": "2026-03-10T10:00:00+00:00"},
                                            "XYZ": {"price": None, "reason": "no Binance USDT pair"}},
                       dca_status=lambda: {"reminders": [{"state": "missed", "asset": "BTC", "month": m,
                                                          "planned_date": m + "-05"} for m in ("2026-01", "2026-02")]})
    got = {(i["code"], i.get("name") or i.get("asset")): i["level"] for i in st["items"]}
    assert got[("paper_running", "fresh")] == "ok"
    assert got[("paper_stale", "late")] == "warn"                 # alive, but its last cycle is 16 h old on 4h bars
    assert got[("paper_stopped", "off")] == "info"                # not alive: stopped, not "stale"
    assert got[("session_error", "broken")] == "bad" and got[("paper_stopped", "broken")] == "info"
    assert got[("demo_error", "d1")] == "bad" and ("demo_error", "d2") not in got
    assert ("price_stale", "BTC") not in got and got[("price_stale", "ETH")] == "warn"
    assert got[("price_unavailable", "XYZ")] == "warn"
    assert got[("collector_ok", None)] == "ok" and got[("dca_missed", "BTC")] == "warn"
    missed = [i for i in st["items"] if i["code"] == "dca_missed"]
    assert len(missed) == 1 and missed[0]["count"] == 2 and missed[0]["month"] == "2026-02"   # one line per asset
    assert got[("live_disabled", None)] == "ok"
    err = next(i for i in st["items"] if i["code"] == "ui_errors")
    assert err["count"] == 1                                       # only the last 24 h, only timestamped lines
    late = alerts.status(_WS(tmp_path, [], {"available": True, "last": "2026-03-10T10:00:00+00:00"}), now=now)
    assert [i["code"] for i in late["items"] if i["area"] == "collector"] == ["collector_stale"]
    assert st["summary"] == {k: sum(i["level"] == k for i in st["items"]) for k in ("bad", "warn", "info", "ok")}


def test_the_status_bar_never_fetches(tmp_path):
    from crypto_trader.strategies.ui import alerts, dca, portfolio, prices

    p = prices.BinancePrices(tmp_path, fetch=lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")))
    assert p.cached() == {}
    lg = portfolio.Ledger(tmp_path)
    plans = dca.Plans(lg, prices=lambda a: (_ for _ in ()).throw(AssertionError("priced")))
    plans.create({"asset": "BTC", "amount": "10", "day": "5", "start": "2026-01"})
    assert plans.status(priced=False)["plans"][0]["price_missing"]
    src = Path(alerts.__file__).read_text(encoding="utf-8")
    assert "urllib" not in src and "fetch_public_klines" not in src and "socket" not in src
    assert "price_cache=self.prices.cached" in Path(server.__file__).read_text(encoding="utf-8")


def test_the_status_endpoint_and_bar(live, ws):
    import json as _json

    c = http.client.HTTPConnection("127.0.0.1", live, timeout=30)
    c.request("GET", "/api/status", headers={"Host": "127.0.0.1"})
    r = c.getresponse()
    doc = _json.loads(r.read())
    c.close()
    assert r.status == 200 and {i["code"] for i in doc["items"]} >= {"live_disabled"}
    body = DASH[DASH.index("const DB_ST = {"):]
    from crypto_trader.strategies.ui import alerts
    codes = set(re.findall(r'add\("\w+", "\w+", "(\w+)"', Path(alerts.__file__).read_text(encoding="utf-8")))
    codes |= {"live_enabled", "collector_stale", "dca_due", "dca_missed"}
    assert all(f"{c}:" in body for c in codes), [c for c in codes if f"{c}:" not in body]


def test_a_recorded_metric_is_never_inserted_as_markup(tmp_path):
    # Sol D42 blocker 2: metrics.round_trips = "<img ...>" from result.json reached innerHTML unescaped
    import json as _json

    from crypto_trader.strategies.ui import compare

    (tmp_path / "runs").mkdir()
    _run(tmp_path, "x", at="2026-01-01")
    doc = _json.loads((tmp_path / "runs" / "x" / "result.json").read_text(encoding="utf-8"))
    doc["metrics"].update(round_trips='<img src=x data-injected="yes">', fees_quote="1e3", net_return=True,
                          sharpe=float("nan"))
    (tmp_path / "runs" / "x" / "result.json").write_text(_json.dumps(doc), encoding="utf-8")
    r = compare.comparison(tmp_path)["groups"][0]["runs"][0]
    assert (r["round_trips"], r["fees_quote"], r["net_return"], r["sharpe"]) == (None, None, None, None)
    body = DASH[DASH.index("async function dbCompare"):]
    assert "${r.round_trips ?? " not in body and "fmtNum(r.round_trips, 0)" in body
    assert "${x.fills ?? " not in DASH


def test_same_file_name_in_another_folder_is_another_dataset(tmp_path):
    # Sol D42 blocker 3: C:/feed1/BTC_USDT-4h.feather and C:/feed2/BTC_USDT-4h.feather were grouped as comparable
    from crypto_trader.strategies.ui import compare

    (tmp_path / "runs").mkdir()
    _run(tmp_path, "a", at="2026-01-01", data="C:/feed1/BTC_USDT-4h.feather")
    _run(tmp_path, "b", at="2026-01-02", data="C:/feed2/BTC_USDT-4h.feather")
    _run(tmp_path, "c", at="2026-01-03", data="C:/feed1/./BTC_USDT-4h.feather")          # same file, other spelling
    groups = compare.comparison(tmp_path)["groups"]
    comp = [g for g in groups if g["comparable"]]
    assert [sorted(r["id"] for r in g["runs"]) for g in comp] == [["a", "c"]]
    assert [r["id"] for g in groups if not g["comparable"] for r in g["runs"]] == ["b"]
    assert all(g["key"]["data"] == "BTC_USDT-4h.feather" for g in groups)


def _files(root):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def test_the_status_route_creates_or_changes_no_file(tmp_path):
    # Sol D42 recheck, blocker 1: /api/status opened portfolio/.lock write-capable (creating it when absent), and a
    # failing read made the server append to ui_errors.log
    import json as _json
    import threading

    srv = server.make_server(tmp_path, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def status():
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request("GET", "/api/status", headers={"Host": "127.0.0.1"})
        r = c.getresponse()
        out = (r.status, _json.loads(r.read()))
        c.close()
        return out

    try:
        before = _files(tmp_path)
        assert not (tmp_path / "portfolio" / ".lock").exists()
        assert status()[0] == 200 and _files(tmp_path) == before          # fresh workspace: nothing created
        lg = server.Handler.ledger
        lg.add({"type": "buy", "asset": "BTC", "at": "2026-01-01", "qty": "1", "price": "1"})
        server.Handler.dca.create({"asset": "BTC", "amount": "10", "day": "1", "start": "2026-01"})
        before = _files(tmp_path)
        assert status()[0] == 200 and _files(tmp_path) == before          # with plans: read, never written

        def broken():
            raise PermissionError(13, "Access is denied")

        real = server.Handler.ws.paper_sessions
        server.Handler.ws.paper_sessions = broken
        try:
            st, doc = status()
        finally:
            server.Handler.ws.paper_sessions = real
        assert st == 200 and _files(tmp_path) == before                   # a failing source: no 500, no log write
        assert any(i["code"] == "check_failed" and i["area"] == "paper" for i in doc["items"])
    finally:
        srv.shutdown()
        srv.server_close()


def test_an_existing_lock_file_is_locked_through_a_read_only_handle(tmp_path):
    from crypto_trader.strategies.ui import portfolio

    lock = portfolio._LedgerLock(tmp_path / ".lock", timeout=0.3)
    with lock:                                            # first use creates it
        pass
    other = portfolio._LedgerLock(tmp_path / ".lock", timeout=0.3)
    with lock:
        assert "r" in lock._fh.mode and "+" not in lock._fh.mode and "a" not in lock._fh.mode
        import pytest as _pytest
        from crypto_trader.strategies import StrategyError
        with _pytest.raises(StrategyError):                # still exclusive against another holder
            other.__enter__()
    assert (tmp_path / ".lock").stat().st_size == 0
