"""D31 decision center: every reason shown comes from records written while the engine decided."""
from __future__ import annotations

import json
import textwrap
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_trader.strategies.paper_live import create_paper_session, paper_cycle
from crypto_trader.strategies.runtime import MarketSpec, RunSettings
from crypto_trader.strategies.ui import decisions, server, store
from crypto_trader.tests.test_strategies_plugin import synthetic_bars

STRAT = textwrap.dedent("""
    from crypto_trader.strategies import Signal, Strategy
    class Cross(Strategy):
        name = "dc-cross"; timeframe = "4h"; warmup_bars = 12
        def on_bar(self, bars):
            f, s = bars.indicator("sma", 4), bars.indicator("sma", 12)
            return Signal.long(1, "fast>slow") if f[-1] > s[-1] else Signal.flat("fast<=slow")
""")


def _paper(root: Path, name: str, settings: RunSettings, *, late_from: int | None = None):
    bars = synthetic_bars(140)
    spec = root / f"{name}.py"
    spec.write_text(STRAT)
    folder = root / "paper" / name
    create_paper_session(folder, strategy_spec=str(spec), market=MarketSpec(), settings=settings, now=bars[60].close_time)

    def fetch(symbol, tf, *, start, now, cafile=None):
        return [b for b in bars if b.open_time >= start and b.close_time <= now]

    def fetch_open(symbol, tf, *, now, cafile=None):
        forming = [b for b in bars if b.open_time <= now < b.close_time]
        return (forming[0].open_time, forming[0].open) if forming else None

    paper_cycle(folder, fetch=fetch, fetch_open=fetch_open, now=bars[60].close_time)
    for k in range(61, 110 if late_from is None else late_from):
        paper_cycle(folder, fetch=fetch, fetch_open=fetch_open, now=bars[k].close_time + timedelta(minutes=1))
    if late_from is not None:   # the process was down: these bars arrive long after their window
        paper_cycle(folder, fetch=fetch, fetch_open=fetch_open, now=bars[late_from + 20].close_time + timedelta(minutes=1))
    return folder


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("ws")
    (root / "settings.json").write_text(json.dumps({"data_dirs": [], "evidence_root": str(root / "none"),
                                                    "test_summary_glob": str(root / "none*.log")}))
    store.Workspace(root)
    _paper(root, "ok", RunSettings(capital_quote=Decimal("1000")), late_from=90)
    # a tiny order-notional limit: every buy the strategy wants is stopped by the deterministic risk engine
    _paper(root, "capped", RunSettings(capital_quote=Decimal("1000"), risk_overrides={"max_order_notional_quote": "5"}))
    return root


def test_every_listed_decision_is_backed_by_the_journal_report_and_the_trace(root):
    res = decisions.collect(root, limit=2000)
    assert res["total"] == len(res["items"]) > 0
    report = {d["market_reference"]: d for d in json.loads((root / "paper" / "ok" / "report.json").read_text())["decisions"]}
    traces = {json.loads(l)["ref"]: json.loads(l) for l in (root / "paper" / "ok" / "decision_trace.jsonl").read_text().splitlines()}
    ok = [i for i in res["items"] if i["source"]["name"] == "ok"]
    assert ok
    for i in ok:
        d = report[i["ref"]]                                  # straight from the journal-derived report
        assert (i["action"], i["rationale"], i["submission"]) == (d["action"], d["rationale"], d["submission"])
        assert i["signal"] == traces[i["ref"]]["signal"] and i["read"] == traces[i["ref"]]["read"]
        assert i["ai_involved"] is False and i["decision_source"] == "strategy-plugin"
    kinds = {i["kind"] for i in ok}
    assert {"entry", "missed"} <= kinds and kinds <= set(decisions.KINDS)
    for i in ok:
        if i["kind"] == "entry":
            assert i["action"] == "buy" and i["risk"]["decision"] == "allowed" and i["fills"]
            assert all(f["intent_id"] == report[i["ref"]]["intent_id"] for f in i["fills"])
        if i["kind"] == "missed":
            assert i["rationale"] == "missed:bar-arrived-late" and not i["fills"] and i["risk"] is None
        if i["kind"] in ("no_entry", "keep"):
            assert i["action"] == "hold" and not i["fills"]


def test_a_risk_rejection_names_the_engine_rule_that_stopped_it(root):
    res = decisions.collect(root, source="paper:capped", limit=2000)
    rejected = [i for i in res["items"] if i["kind"] == "rejected_risk"]
    assert rejected and res["counts"]["entry"] == 0
    for i in rejected:
        assert i["risk"]["decision"] == "rejected" and "max_order_notional_exceeded" in i["risk"]["codes"]
        assert i["validity"]["disposition"] == "accepted" and not i["fills"]
        assert i["signal"]["reason"] == "fast>slow"           # the strategy wanted in; the risk engine said no


def test_filters_counts_and_detail(root):
    allr = decisions.collect(root, limit=2000)
    only = decisions.collect(root, kinds=["missed"], limit=2000)
    assert only["total"] == allr["counts"]["missed"] and all(i["kind"] == "missed" for i in only["items"])
    assert only["counts"] == allr["counts"]                  # counts describe the filtered population, not the kind tab
    src = decisions.collect(root, source="paper:ok", limit=2000)
    assert {i["source"]["name"] for i in src["items"]} == {"ok"}
    assert decisions.collect(root, strategy="nope")["total"] == 0
    newest = allr["items"][0]["at"]
    assert all(i["at"] <= newest for i in allr["items"])
    pick = next(i for i in allr["items"] if i["kind"] == "entry")
    det = decisions.detail(root, pick["id"])
    assert det["item"]["evidence"]["journal_decision"]["market_reference"] == pick["ref"]
    assert det["item"]["evidence"]["trace"]["ref"] == pick["ref"] and det["bars"]
    with pytest.raises(KeyError):
        decisions.detail(root, "paper:ok:no-such-ref")
    with pytest.raises(ValueError):
        decisions.detail(root, "nonsense")


def test_decisions_without_a_trace_are_shown_without_inventing_one(root, tmp_path):
    import shutil
    copy = tmp_path / "ws2"
    shutil.copytree(root, copy)
    (copy / "paper" / "ok" / "decision_trace.jsonl").unlink()
    items = [i for i in decisions.collect(copy, source="paper:ok", limit=2000)["items"]]
    assert items and all(i["signal"] is None and i["read"] is None and i["evidence"]["trace"] is None
                         for i in [decisions.detail(copy, x["id"])["item"] for x in items[:5]])
    assert all(i["ai_involved"] is False for i in items)


def test_decision_endpoints_are_read_only_and_guarded(root):
    import http.client

    srv = server.make_server(root, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        def get(path, host="127.0.0.1"):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            c.request("GET", path, headers={"Host": host})
            r = c.getresponse()
            body = json.loads(r.read() or b"{}")
            c.close()
            return r.status, body
        status, body = get("/api/decisions?kinds=rejected_risk&limit=5")
        assert status == 200 and 0 < len(body["items"]) <= 5 and all(i["kind"] == "rejected_risk" for i in body["items"])
        assert "evidence" not in body["items"][0]             # the list stays small; evidence is in the detail
        from urllib.parse import quote
        status, det = get("/api/decision?id=" + quote(body["items"][0]["id"], safe=""))
        assert status == 200 and det["item"]["evidence"]["journal_decision"]
        assert get("/api/decision?id=paper:ok:missing")[0] == 400
        assert get("/api/decisions", host="evil.example")[0] == 403
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request("POST", "/api/decisions", body="{}", headers={"Host": "127.0.0.1", "X-CT-UI": "1", "Content-Type": "application/json"})
        assert c.getresponse().status == 404                  # nothing can be written through the decision center
    finally:
        srv.shutdown()
        srv.server_close()


def test_safety_block_is_shown_as_a_stop_not_as_not_requested():
    # Sol D30-D33 r1 blocker 5: a safety block is journaled as risk "rejected" + submission "blocked_by_safety"
    # (paper/session.py), and the safety stage was rendered only when risk was "allowed" -> "not requested".
    import re as _re
    import shutil
    import subprocess

    src = (Path(decisions.__file__).parent / "static" / "decisions.js").read_text(encoding="utf-8")
    m = _re.search(r"^function safetyStage\(i, intent\) \{.*?^\}", src, _re.S | _re.M)
    assert m, "safetyStage() must exist in decisions.js"
    # the decision view must render the safety stage through safetyStage(), not an inline condition
    assert src.count("step(...safetyStage(i, intent))") == 1
    assert not _re.search(r'step\([^)]*"الحماية"', src), "safety stage rendered outside safetyStage()"
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    cases = {
        "safety": ({"kind": "blocked_safety", "risk": {"decision": "rejected", "codes": ["trading_halted"]}}, True),
        "risk": ({"kind": "rejected_risk", "risk": {"decision": "rejected", "codes": ["max_position"]}}, True),
        "allowed": ({"kind": "entry", "risk": {"decision": "allowed", "codes": []}}, True),
        "no_order": ({"kind": "keep", "risk": None}, False),
    }
    js = m.group(0) + "\nconst C = " + json.dumps(cases) + ";\n" + \
        "console.log(JSON.stringify(Object.fromEntries(Object.entries(C).map(([k, [i, t]]) => [k, safetyStage(i, t)[0]]))));"
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == {"safety": "stop", "risk": "skip", "allowed": "pass", "no_order": "skip"}
