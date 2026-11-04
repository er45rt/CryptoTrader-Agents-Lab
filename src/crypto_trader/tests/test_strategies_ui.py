"""D30 local GUI: request guards, workspace actions, experiment log and the sealed-period cutoff."""
from __future__ import annotations

import csv
import http.client
import json
import threading

from pathlib import Path

import pytest

from crypto_trader.strategies import StrategyError
from crypto_trader.strategies.ui import server, store
from crypto_trader.tests.test_strategies_plugin import synthetic_bars

GOOD = '''
from crypto_trader.strategies import Strategy, Signal

class Up(Strategy):
    name = "up"
    timeframe = "4h"
    warmup_bars = 5

    def on_bar(self, bars):
        return Signal.long(1) if bars.close[-1] > bars.indicator("sma", 5)[-1] else Signal.flat()
'''


@pytest.fixture()
def ws(tmp_path: Path) -> store.Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.json").write_text(json.dumps({
        "data_dirs": [], "evidence_root": str(tmp_path / "none"), "test_summary_glob": str(tmp_path / "none*.log")}))
    w = store.Workspace(root)
    rows = [{"date": b.open_time.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close,
             "volume": b.volume} for b in synthetic_bars(200)]
    with (root / "data" / "SYN_USDT-4h.csv").open("w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    return w


def _data(w: store.Workspace) -> str:
    return w.data_files()["files"][0]["path"]


# --- request guards -------------------------------------------------------------------------------------

@pytest.fixture()
def live(ws: store.Workspace):
    srv = server.make_server(ws.root, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _req(port: int, method: str, path: str, *, host: str = "127.0.0.1", body: dict | None = None,
         ui_header: bool = True) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Host": host, "Content-Type": "application/json"}
    if ui_header:
        headers["X-CT-UI"] = "1"
    conn.request(method, path, body=json.dumps(body or {}) if method == "POST" else None, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_server_refuses_foreign_host_and_headerless_posts(live):
    assert _req(live, "GET", "/api/system", host="evil.example")[0] == 403
    assert _req(live, "GET", "/api/system", host="localhost")[0] == 200
    status, _ = _req(live, "POST", "/api/risk", body={"capital": "5"}, ui_header=False)
    assert status == 403
    status, sysinfo = _req(live, "GET", "/api/system")
    assert status == 200 and sysinfo["mainnet_enabled"] is False


def test_server_reports_bad_input_as_400_and_unknown_routes_as_404(live):
    assert _req(live, "POST", "/api/risk", body={"risk_overrides": {"no_such_limit": "1"}})[0] == 400
    assert _req(live, "GET", "/api/run?id=../../etc")[0] == 400
    assert _req(live, "GET", "/api/orders")[0] == 404
    assert _req(live, "POST", "/api/order", body={})[0] == 404


# --- workspace actions ------------------------------------------------------------------------------------

def test_upload_checks_before_keeping_and_remove_is_confined(ws):
    with pytest.raises(StrategyError):
        ws.upload_strategy("broken.py", "this is not python(")
    assert not (ws.root / "strategies" / "broken.py").exists()
    with pytest.raises(StrategyError):
        ws.upload_strategy("../escape.py", GOOD)
    assert ws.upload_strategy("up.py", GOOD)["ok"]
    assert any(s["name"] == "up" and s["removable"] for s in ws.strategies()["strategies"])
    with pytest.raises(StrategyError):
        ws.remove_strategy("../settings.json")
    assert ws.remove_strategy("up.py")["ok"]
    assert not (ws.root / "strategies" / "up.py").exists()


def test_backtest_appends_to_the_experiment_log_and_raises_the_trial_count(ws):
    ws.upload_strategy("up.py", GOOD)
    spec = next(s["spec"] for s in ws.strategies()["strategies"] if s["name"] == "up")
    first = ws.backtest({"spec": spec, "data": _data(ws)})
    assert first["verdict"]["verdict"] in ("promising", "unproven")
    assert {c["key"] for c in first["verdict"]["checks"]} == {"beats_hold", "both_halves", "fees_x2", "out_of_sample", "dsr"}
    assert first["bars"] and first["equity"] and first["monthly"]
    log = ws.root / "runs" / "runs.jsonl"
    before = log.read_text(encoding="utf-8")
    second = ws.backtest({"spec": spec, "data": _data(ws)})
    after = log.read_text(encoding="utf-8")
    assert after.startswith(before) and len(after.splitlines()) == 2          # append-only
    dsr = {c["key"]: c for c in second["verdict"]["checks"]}["dsr"]
    assert "2" in dsr["label"]                                                  # n_trials grew
    assert ws.run(second["id"])["id"] == second["id"]


def test_backtest_refuses_unlisted_data_and_never_reads_the_sealed_period(ws, tmp_path):
    ws.upload_strategy("up.py", GOOD)
    spec = next(s["spec"] for s in ws.strategies()["strategies"] if s["name"] == "up")
    outside = tmp_path / "other-4h.csv"
    outside.write_text((ws.root / "data" / "SYN_USDT-4h.csv").read_text())
    with pytest.raises(StrategyError):
        ws.backtest({"spec": spec, "data": str(outside)})
    # move the synthetic series so it straddles the sealed start: nothing at or after it may be used
    bars = synthetic_bars(200)
    shift = store.SEALED_FROM - bars[100].open_time
    rows = [{"date": (b.open_time + shift).isoformat(), "open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "volume": b.volume} for b in bars]
    with (ws.root / "data" / "SYN_USDT-4h.csv").open("w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    res = ws.backtest({"spec": spec, "data": _data(ws)})
    assert res["bars_count"] <= 100
    assert all(b["t"] < store.SEALED_FROM.isoformat() for b in res["bars"])


def test_risk_save_validates_before_writing(ws):
    before = ws.settings_path.read_text(encoding="utf-8")
    with pytest.raises(StrategyError):
        ws.save_risk({"risk_overrides": {"no_such_limit": "1"}})
    with pytest.raises(StrategyError):
        ws.save_risk({"capital": "-5"})
    assert ws.settings_path.read_text(encoding="utf-8") == before
    assert ws.save_risk({"capital": "2500"})["ok"]
    assert ws.risk()["editable"]["capital"] == "2500"
    assert ws.risk()["production"]["mainnet_enabled"] is False


def test_paper_names_are_confined(ws):
    for bad in ("../x", "a/b", "", "..", "x" * 80):
        with pytest.raises(StrategyError):
            ws._paper_dir(bad)
    with pytest.raises(StrategyError):
        ws.paper_start("missing")


# --- Sol D30 round-1 regressions ------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from crypto_trader.strategies import analysis  # noqa: E402


def _equity(steps):
    eq, v = [], 1000.0
    eq.append(("2024-01-01T00:00", v))
    for i, r in enumerate(steps, 1):
        v *= 1 + r
        eq.append((f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}T00:00", v))
    return eq


def _result(eq, sharpe=2.0, hold=1.0):
    return SimpleNamespace(metrics={"sharpe": sharpe, "net_return": eq[-1][1] / eq[0][1] - 1},
                           benchmark={"sharpe": hold}, equity_curve=eq, settings={"taker_fee": "0.001"})


GOOD_FEES = lambda fee: SimpleNamespace(metrics={"net_return": 0.05})  # noqa: E731


def test_sol_d30_3_concurrent_backtests_never_share_a_trial_number(ws, monkeypatch):
    ws.upload_strategy("up.py", GOOD)
    spec = next(s["spec"] for s in ws.strategies()["strategies"] if s["name"] == "up")
    seen, gate = [], threading.Barrier(2, timeout=5)
    real = store.robustness_verdict

    def slow_verdict(result, *, rerun_with_fees, n_trials):
        seen.append(n_trials)
        try:
            gate.wait(timeout=0.5)        # the two runs meet here only if the count is not serialized
        except threading.BrokenBarrierError:
            pass
        return real(result, rerun_with_fees=rerun_with_fees, n_trials=n_trials)

    monkeypatch.setattr(store, "robustness_verdict", slow_verdict)
    threads = [threading.Thread(target=ws.backtest, args=({"spec": spec, "data": _data(ws)},)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seen) == [1, 2]
    assert len((ws.root / "runs" / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_sol_d30_4_dsr_uses_central_moment_normalization():
    mu = 1.6445 * .01 * (30 ** 0.5) / 29
    p = analysis.dsr_probability([mu + .01, mu - .01] * 15, 1)
    assert p is not None and p < 0.95                       # Sol: 0.949963518 (the old code gave 0.950093)
    assert p == pytest.approx(0.949963518, abs=1e-6)


def test_sol_d30_5_a_missing_fee_check_is_never_promising():
    eq = _equity([0.0132, -0.0068] * 15)
    v = analysis.robustness_verdict(_result(eq), rerun_with_fees=None, n_trials=1)
    assert v["verdict"] == "unproven"
    assert {c["key"]: c["ok"] for c in v["checks"]}["fees_x2"] is None
    assert analysis.robustness_verdict(_result(eq), rerun_with_fees=GOOD_FEES, n_trials=1)["verdict"] == "promising"


def test_sol_d30_6_equal_sharpe_does_not_beat_hold():
    eq = _equity([0.0132, -0.0068] * 15)
    v = analysis.robustness_verdict(_result(eq, sharpe=1.0, hold=1.0), rerun_with_fees=GOOD_FEES, n_trials=1)
    assert {c["key"]: c["ok"] for c in v["checks"]}["beats_hold"] is False
    assert v["verdict"] == "unproven"


def test_sol_d30_7_start_is_refused_while_a_stopped_loop_is_still_alive(ws, monkeypatch):
    folder = ws.root / "paper" / "s1"
    folder.mkdir()
    (folder / "session.json").write_text("{}")
    (folder / "pid.json").write_text(json.dumps({"pid": 4242}))
    (folder / "STOP").write_text("x")
    monkeypatch.setattr(store, "_pid_alive", lambda pid: True)
    spawned = []
    monkeypatch.setattr(store.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    with pytest.raises(StrategyError):
        ws.paper_start("s1")
    assert (folder / "STOP").exists() and spawned == []     # STOP kept, no second loop
    (folder / "STOP").unlink()
    assert ws.paper_start("s1") == {"ok": True, "already_running": True} and spawned == []
