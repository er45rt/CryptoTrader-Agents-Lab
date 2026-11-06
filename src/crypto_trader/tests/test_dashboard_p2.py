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
    doc = {"id": rid, "at": at, "mode": mode, "strategy": strategy, "strategy_version": "1", "data": f"C:/data/{data}",
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
