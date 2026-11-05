"""D32 demo mode: an offline replay of local bars through the unchanged paper engine (no network, no orders)."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crypto_trader.strategies import StrategyError
from crypto_trader.strategies import paper_live
from crypto_trader.strategies.ui import decisions, demo, store
from crypto_trader.tests.test_strategies_plugin import synthetic_bars

STRAT = textwrap.dedent("""
    from crypto_trader.strategies import Signal, Strategy
    class Cross(Strategy):
        name = "demo-cross"; timeframe = "4h"; warmup_bars = 12
        def on_bar(self, bars):
            f, s = bars.indicator("sma", 4), bars.indicator("sma", 12)
            return Signal.long(1, "fast>slow") if f[-1] > s[-1] else Signal.flat("fast<=slow")
""")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("demo mode must never call the live price source")

    monkeypatch.setattr(paper_live, "fetch_public_klines", no_network)
    monkeypatch.setattr(paper_live, "fetch_forming_open", no_network)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.json").write_text(json.dumps({"data_dirs": [], "evidence_root": str(root / "x"), "test_summary_glob": str(root / "x*.log")}))
    ws = store.Workspace(root)
    # 220 synthetic bars that run 40 bars INTO the sealed period: those must never be read
    bars = synthetic_bars(220)
    shift = demo.SEALED_FROM - bars[180].open_time
    rows = [{"date": (b.open_time + shift).isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close,
             "volume": b.volume} for b in bars]
    data = root / "data" / "SYN_USDT-4h.csv"
    with data.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    spec = root / "strategies" / "cross.py"
    spec.write_text(STRAT)
    return ws, demo.DemoManager(root), str(spec), data


def _files(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir()) if p.is_file()}


def test_demo_replays_local_bars_through_the_paper_engine_without_the_future(env):
    ws, dm, spec, data = env
    res = dm.create(spec=spec, data=data, start=demo.SEALED_FROM - timedelta(hours=4 * 120))
    name = res["demo"]["name"]
    assert res["demo"]["state"] == "paused" and res["progress"]["done"] == 0
    last = datetime.fromisoformat(res["demo"]["replay_to"])
    assert last <= demo.SEALED_FROM                               # the sealed period is never loaded
    for _ in range(4):
        st = dm.step(name, 20)
        clock = datetime.fromisoformat(st["demo"]["clock"])
        seen = [json.loads(l) for l in (dm.base / name / "bars.jsonl").read_text().splitlines()]
        assert all(datetime.fromisoformat(r["date"]) + timedelta(hours=4) <= clock for r in seen)   # nothing after the clock
        assert all(datetime.fromisoformat(r["date"]) < demo.SEALED_FROM for r in seen)
    assert st["progress"]["done"] == 80 and st["status"]["fills"] > 0 and st["status"]["missed_bars"] == 0
    folder = dm.base / name
    n_events = sqlite3.connect(folder / "journal.db").execute("select count(*) from events").fetchone()[0]
    assert n_events > 0                                           # the engine journal, not a parallel record
    assert json.loads((folder / "session.json").read_text())["mode"].startswith("PAPER")
    eq = [json.loads(l) for l in (folder / "equity.jsonl").read_text().splitlines()]
    assert len(eq) >= 79 and float(eq[-1]["equity"]) == pytest.approx(float(st["status"]["equity_quote"]))
    # D31: the demo's decisions are in the decision center, with the trace written while deciding
    got = decisions.collect(ws.root, source=f"demo:{name}", limit=2000)
    assert got["total"] > 0 and got["counts"]["entry"] > 0
    assert all(i["ai_involved"] is False and i["signal"] is not None for i in got["items"])
    det = decisions.detail(ws.root, got["items"][0]["id"])
    assert det["bars"] and det["item"]["evidence"]["journal_decision"]


def test_demo_runs_to_the_end_and_stops_before_the_sealed_period(env):
    ws, dm, spec, data = env
    name = dm.create(spec=spec, data=data, start=demo.SEALED_FROM - timedelta(hours=4 * 15))["demo"]["name"]
    st = dm.step(name, 50)
    st = dm.step(name, 50)
    assert st["demo"]["state"] == "finished" and st["progress"]["done"] == st["progress"]["total"]
    with pytest.raises(StrategyError):
        dm.start(name)


def test_background_run_pause_speed_and_reset_keep_old_results(env):
    ws, dm, spec, data = env
    first = dm.create(spec=spec, data=data, start=demo.SEALED_FROM - timedelta(hours=4 * 100))["demo"]["name"]
    dm.start(first, "max")
    deadline = time.time() + 120
    while dm.status(first)["progress"]["done"] < 5 and time.time() < deadline:
        time.sleep(0.5)
    st = dm.pause(first)
    assert st["running"] is False and st["demo"]["state"] == "paused" and st["progress"]["done"] >= 5
    with pytest.raises(StrategyError):
        dm.set_speed(first, "warp")
    assert dm.set_speed(first, "1")["demo"]["speed"] == "1"
    before = _files(dm.base / first)
    second = dm.create(spec=spec, data=data)["demo"]["name"]        # "reset" = a NEW demo
    dm.step(second, 3)
    assert second != first and _files(dm.base / first) == before   # the old demo is untouched
    assert {d["name"] for d in dm.list()["demos"]} == {first, second}
    with pytest.raises(StrategyError):
        dm.delete(first, "yes")                                    # deleting needs the exact name
    assert dm.delete(first, first)["ok"] and not (dm.base / first).exists()
    with pytest.raises(StrategyError):
        dm.folder("../paper")


def test_demo_endpoints(env):
    import http.client
    import threading
    from crypto_trader.strategies.ui import server

    ws, _, spec, data = env
    srv = server.make_server(ws.root, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def call(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Host": "127.0.0.1", "X-CT-UI": "1", "Content-Type": "application/json"})
        r = c.getresponse()
        out = json.loads(r.read() or b"{}")
        c.close()
        return r.status, out
    try:
        s, res = call("POST", "/api/demo/new", {"spec": spec, "data": str(data), "capital": "10000"})
        assert s == 200 and res["demo"]["capital"] == "10000"
        name = res["demo"]["name"]
        s, res = call("POST", "/api/demo/step", {"name": name, "n": 30})
        assert s == 200 and res["progress"]["done"] == 30
        assert call("GET", "/api/demo")[1]["demos"][0]["name"] == name
        assert call("POST", "/api/demo/new", {"spec": spec, "data": "C:/elsewhere.csv"})[0] == 400   # listed data only
        assert call("POST", "/api/demo/delete", {"name": name, "confirm": "no"})[0] == 400
        assert call("POST", "/api/demo/warp", {"name": name})[0] == 400
        ops = [json.loads(l)["kind"] for l in (ws.root / "operations.jsonl").read_text().splitlines()]
        assert "demo.created" in ops
    finally:
        srv.shutdown()
        srv.server_close()


def test_concurrent_steps_on_one_demo_are_serialized(env, monkeypatch):
    # Sol D30-D33 r1 blocker 3: two step requests could run paper_cycle on the same session folder at once
    # (both reading the same bar-file tail, both appending the same new bars). They must run one at a time.
    import threading

    ws, dm, spec, data = env
    name = dm.create(spec=spec, data=data, start=demo.SEALED_FROM - timedelta(hours=4 * 30))["demo"]["name"]
    cursor0 = dm._doc(name)["cursor"]
    real = demo.paper_cycle
    active, peak, guard = [0], [0], threading.Lock()

    def tracked(*a, **k):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            time.sleep(0.3)                  # widen the window so overlapping calls would be seen
            return real(*a, **k)
        finally:
            with guard:
                active[0] -= 1

    monkeypatch.setattr(demo, "paper_cycle", tracked)
    gate, errors = threading.Barrier(2), []

    def one():
        gate.wait()
        try:
            dm.step(name, 1)
        except Exception as exc:            # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=one) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert not errors, errors
    assert peak[0] == 1, f"{peak[0]} paper cycles ran at once on one demo"
    assert dm._doc(name)["cursor"] == cursor0 + 2
    opens = [json.loads(l)["date"] for l in (dm.folder(name) / "bars.jsonl").read_text().splitlines() if l.strip()]
    assert len(opens) == len(set(opens))
